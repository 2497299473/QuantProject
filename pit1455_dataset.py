"""14:55 PIT Forecast 数据集构建器（V4.4 步 1，2026-09-22）。

作用：把「label 分母 = T 日最终 NAV」的现有冻结样本，按 pit1455_contract 的口径
重算为「label 分母 = 14:55 可观测 NAV 估计」的新样本集。**只读、离线、可回滚**：
不改动现有 samples_frozen_*.jsonl，产出一个带独立后缀的新 JSONL + meta 侧车。

为什么是「重算」而非「原位替换」：
- 现有 est_chg（fraction 量纲）正是 14:55 对 T 日的最优估算，已在样本里；
- 但 navs[T]（最终 NAV）藏在 data/klines 缓存里、没进样本行；
- 新 label = navs[T+h] / (navs[T-1]*(1+est_chg)) - 1，
  分子 navs[T+h]、分母所需的 navs[T-1] 都要从缓存补齐。
  所以本构建器 = 现有特征行 ⊕ 缓存里的 NAV 序列 → 逐行套契约式。

口径边界（诚实标注，勿当成品）：
- 这是「离线研究口径」：navs[T-1] 取缓存里 T 的前一交易日已公布净值。
  真实 14:55 决策时 navs[T-1] 一定可见（前一日净值），成立。
- est_chg 用现有样本值（日线收盘近似 14:55，缺尾盘漂移）——沿用既有约定，
  本步不引入新近似，只换 label 分母。
- 跳过条件：缓存无该基金 / T 非缓存内交易日 / 无前一交易日 / est_chg 缺失 /
  未来第 h 日不在缓存内 → 该行该 label 记 None（不硬造）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import pit1455_contract as C  # noqa: E402

FWD_KEYS = ("fwd1", "fwd2", "fwd3", "fwd5", "fwd10", "fwd20")
FWD_H = {k: int(k[3:]) for k in FWD_KEYS}


def load_nav_series(code: str) -> list[tuple[str, float]] | None:
    """读本地 NAV 缓存（零网络）→ [(date, nav), ...] 升序；缺失返回 None。"""
    p = BASE_DIR / "data" / "klines" / f"{code}.json"
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    navs = raw.get("navs")
    if not isinstance(navs, list) or not navs:
        return None
    out: list[tuple[str, float]] = []
    for row in navs:
        try:
            out.append((str(row[0])[:10], float(row[1])))
        except (IndexError, TypeError, ValueError):
            continue
    return out or None


def recompute_row(sample: dict, nav_rows: list[tuple[str, float]],
                  date_index: dict[str, int]) -> dict | None:
    """按契约重算单行的 14:55 label。返回带 fwd*_1455 字段的新行；不可算 → None。"""
    date = sample.get("date")
    est_chg = sample.get("est_chg")          # fraction 量纲（契约一致）
    if date is None or est_chg is None:
        return None
    i = date_index.get(date)
    if i is None or i < 1:                    # 无前一交易日 → navs[T-1] 不可得
        return None
    prev_nav = nav_rows[i - 1][1]
    nav_hat = C.estimate_nav_at_1455(prev_nav, est_chg)
    if nav_hat is None:
        return None
    out = dict(sample)                        # 保留原特征行（含原 label，供对照）
    out["_nav_hat_1455"] = round(nav_hat, 6)
    for k, h in FWD_H.items():
        j = i + h
        if j < len(nav_rows):
            out[f"{k}_1455"] = _r4(C.fwd_from_1455(nav_hat, nav_rows[j][1]))
        else:
            out[f"{k}_1455"] = None
    return out


def _r4(v):
    return None if v is None else round(v, 6)


def build(snapshot_path: Path, out_path: Path) -> dict:
    """读冻结样本 JSONL → 逐行重算 → 写新 JSONL。返回统计摘要。"""
    cache: dict[str, list[tuple[str, float]]] = {}
    index: dict[str, dict[str, int]] = {}
    stats = {"total": 0, "kept": 0, "skipped_fund_no_cache": 0,
             "skipped_date": 0, "skipped_est_chg": 0,
             "label_diff": {k: [] for k in FWD_KEYS}}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(snapshot_path, encoding="utf-8") as fh, \
            open(out_path, "w", encoding="utf-8") as wf:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            stats["total"] += 1
            s = json.loads(line)
            code = s.get("fund")
            if code not in cache:
                cache[code] = load_nav_series(code)
                if cache[code]:
                    index[code] = {d: n for n, (d, _) in enumerate(cache[code])}
            nav_rows = cache.get(code)
            if not nav_rows:
                stats["skipped_fund_no_cache"] += 1
                continue
            if s.get("est_chg") is None:
                stats["skipped_est_chg"] += 1
                continue
            new = recompute_row(s, nav_rows, index[code])
            if new is None:
                stats["skipped_date"] += 1
                continue
            stats["kept"] += 1
            for k in FWD_KEYS:
                a, b = s.get(k), new.get(f"{k}_1455")
                if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                    stats["label_diff"][k].append(abs(b - a))
            wf.write(json.dumps(new, ensure_ascii=False) + "\n")
    return stats


def _summarize(stats: dict) -> dict:
    import statistics as st
    diff = {}
    for k, v in stats["label_diff"].items():
        diff[k] = {"n": len(v),
                   "mean_abs": round(st.fmean(v), 6) if v else None,
                   "max_abs": round(max(v), 6) if v else None}
    return {k: v for k, v in stats.items() if k != "label_diff"} | \
        {"label_delta_vs_old": diff}


def main() -> int:
    ap = argparse.ArgumentParser(description="14:55 PIT 数据集构建器（零网络）")
    ap.add_argument("--snapshot", default=str(
        BASE_DIR / "forecast_outputs" / "samples_frozen_20260910.jsonl"))
    ap.add_argument("--out", default=None,
                    help="输出 JSONL 路径（缺省 forecast_outputs/samples_pit1455_<stamp>.jsonl）")
    ap.add_argument("--dry-run", action="store_true", help="只算统计不写文件")
    args = ap.parse_args()

    snap = Path(args.snapshot)
    if not snap.is_file():
        print(f"[pit1455] FAIL 冻结样本不存在：{snap}", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else \
        BASE_DIR / "forecast_outputs" / f"samples_pit1455_20260922.jsonl"

    if args.dry_run:
        stats = build(snap, Path(__file__).with_suffix(".tmpout.jsonl"))
        Path(__file__).with_suffix(".tmpout.jsonl").unlink(missing_ok=True)
    else:
        stats = build(snap, out)

    summ = _summarize(stats)
    print("[pit1455] " + json.dumps(summ, ensure_ascii=False, indent=2))
    meta = {"contract": C.contract_provenance(), "source_snapshot": snap.name,
            "output": out.name, "stats": summ}
    if not args.dry_run:
        out.with_suffix(".meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[pit1455] 写出 {out} 与 {out.with_suffix('.meta.json').name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
