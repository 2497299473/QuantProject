"""P0-B 持仓快照冻结探针（2026-09-09，Summer 拍板：把修 002112 快照拉取失败塞进第一步）。

只读模式（默认）：对 config.fund_pool 每只基金跑 holdings_history，
输出逐年×逐基金快照计数矩阵 + sha256 指纹 + 缺年告警捕获，
写 forecast_outputs/snapshot_frozen_<stamp>.json。

背景（MEMORY 08-29）：同日两次重跑代理抖动曾致 002112 静默少拉 1 年（22 vs 26 期）
→ 特征漂移 → RankIC 漂移 ±0.03~0.05。本探针的作用：
 ① 诊断：哪只基金缺哪个年（holdings_history 的 warnings 现在会点名 failed_years）；
 ② 冻结：裁决（P3）与 P1/P2 全部评估以本文件指纹为准，缺年先补拉再重冻结。

用法:
  python experiments/forecast_lab/probe_snapshot_freeze.py            # 只读探针
  python experiments/forecast_lab/probe_snapshot_freeze.py --compare a.json b.json
      # 对比两份冻结指纹的差异矩阵（纯本地，不触网）

纪律: 只读拉取（fundf10 一年一度请求 ×7年 ×4基金 ≈ 28 请求，自带 2s/5s 重试），
不写 data/、不改生产脚本；若东财 fundf10 被 IP 封锁会原样报失败年份。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import warnings
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]   # QuantV1 根
sys.path.insert(0, str(BASE_DIR))

from core import lookthrough   # noqa: E402


def _freeze_one(code: str) -> dict:
    """单基金：捕获 holdings_history 的缺年告警，产计数矩阵 + 全量 JSON sha256。"""
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        snaps = lookthrough.holdings_history(code)
        warn_msgs = [str(x.message) for x in w]
    by_year: dict[str, int] = {}
    for s in snaps:
        y = s["date"][:4]
        by_year[y] = by_year.get(y, 0) + 1
    canon = json.dumps(
        [{"d": s["date"], "h": sorted((h["code"], h["pct"]) for h in s["holdings"])}
         for s in snaps],
        ensure_ascii=False, sort_keys=True)
    return {
        "n_snapshots": len(snaps),
        "by_year": dict(sorted(by_year.items())),
        "sha256": hashlib.sha256(canon.encode("utf-8")).hexdigest(),
        "failed_year_warnings": warn_msgs,
    }


def freeze(outdir: Path) -> Path:
    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    funds = cfg["fund_pool"]
    years_expected = [str(y) for y in cfg["lookthrough"]["history_years"]]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {
        "kind": "forecast_lab_snapshot_freeze",
        "schema_version": "1",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "history_years_expected": years_expected,
        "funds": {},
    }
    print(f"== 快照冻结探针 {stamp}（{len(funds)} 基金 × {len(years_expected)} 年，"
          f"约 28 请求）==")
    for code in funds:
        try:
            r = _freeze_one(code)
        except Exception as e:  # 整只基金炸掉也要留行，防"静默消失"
            r = {"n_snapshots": 0, "by_year": {}, "sha256": None,
                 "error": f"{type(e).__name__}: {e}"}
        payload["funds"][code] = r
        yrs = r.get("by_year", {})
        row = "  ".join(f"{y}:{yrs.get(y, 0)}" for y in years_expected)
        # 区分两类 0 计数：拉取失败（有告警，必须补） vs 天然无披露（新基金，正常）。
        # 混为一谈正是 v7 静默缺年难被立刻发现的根因之一。
        has_fail = bool(r.get("failed_year_warnings")) or bool(r.get("error"))
        zero_years = [y for y in years_expected if yrs.get(y, 0) == 0]
        if has_fail:
            flag = " ⚠️拉取失败-需补拉后再冻结"
        elif zero_years:
            flag = f" （新基金无早期披露: {zero_years[0]}~{zero_years[-1]}，正常）"
        else:
            flag = ""
        print(f"  {code}  n={r['n_snapshots']:3d}  {row}  sha={str(r.get('sha256'))[:12]}{flag}")
        for m in r.get("failed_year_warnings", []):
            print(f"      ! {m[:180]}")
    out = outdir / f"snapshot_frozen_{stamp}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[written] {out}")
    return out


def compare(a: str, b: str) -> int:
    """两份冻结指纹对比：输出逐基金 sha/计数差异；全同 → 0（可复现），否则 → 1。"""
    ja = json.loads(Path(a).read_text(encoding="utf-8"))
    jb = json.loads(Path(b).read_text(encoding="utf-8"))
    diff = 0
    print(f"== compare {Path(a).name} vs {Path(b).name} ==")
    for code in sorted(set(ja["funds"]) | set(jb["funds"])):
        fa, fb = ja["funds"].get(code, {}), jb["funds"].get(code, {})
        same = fa.get("sha256") == fb.get("sha256")
        if not same:
            diff += 1
            ya, yb = fa.get("by_year", {}), fb.get("by_year", {})
            ys = sorted(set(ya) | set(yb))
            cells = "  ".join(f"{y}:{ya.get(y, 0)}→{yb.get(y, 0)}"
                              for y in ys if ya.get(y, 0) != yb.get(y, 0))
            print(f"  {code}  sha DIFF  n {fa.get('n_snapshots')}→{fb.get('n_snapshots')}  {cells}")
        else:
            print(f"  {code}  same (n={fa.get('n_snapshots')})")
    print(f"=> {'完全一致，冻结可复现' if diff == 0 else f'{diff} 只基金不一致——裁决前必须先补拉/定版本'}")
    return 0 if diff == 0 else 1


if __name__ == "__main__":
    outdir = BASE_DIR / "forecast_outputs"
    outdir.mkdir(exist_ok=True)
    if len(sys.argv) >= 2 and sys.argv[1] == "--compare":
        sys.exit(compare(sys.argv[2], sys.argv[3]))
    freeze(outdir)
