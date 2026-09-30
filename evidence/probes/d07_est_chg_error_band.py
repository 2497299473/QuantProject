#!/usr/bin/env python3
"""D-07（2026-09-30 面 4 审计）：est_chg 对 T 日真实涨跌 a_T 的经验误差分布。

背景：1455 口径 label 分母 nav_hat = navs[T-1]*(1+est_chg)，est_chg 用日线
收盘近似 14:55（backtest_spread 既有声明「仅差尾盘漂移」）。分母误差
= navs[T-1]*(a_T - est_chg)（契约 docstring 精确式的一阶项），故
|est_chg - a_T| 的经验分布 = 1455 口径的量化误差带。本探针把它算出来。

口径（写死，可复现）：
- a_T = navs[T]/navs[T-1] - 1，navs 取 data/klines/<fund>.json 缓存
  （官方净值序列，(date, nav) 升序；T-1 = 缓存内 T 的前一行）。
- err = est_chg - a_T（带符号）；报 |err| 的 mean / p50 / p90 / max。
- 轨 1（冻结池，主轨）：canonical 冻结件 samples_frozen_20260910.jsonl
  全部 3371 行；est_chg 为 backtest_spread 的收盘近似（fraction）。
  样本跨 ~1500 交易日 ⇒ 满足验收「≥30 个交易日」。
  注意语义：此轨误差含「尾盘漂移 + 持仓覆盖/跟踪误差」两部分
  （est_chg 只按已披露前十大持仓加权，covered_pct ~75-80%）。
- 轨 2（live，临时轨）：data/intraday_features.jsonl 中 date ≥ 2026-09-23
  （契约切换日，EST_CHG_LIVE_FRACTION_SINCE）的 post 槽行，est_chg 已是
  fraction（腾讯行情加权，过 est_chg_from_pct 桥后落盘）。
  截至 2026-09-30 只有 5 个交易日 ⇒ **不满足 ≥30 日验收，标 PROVISIONAL**，
  满 30 日后重跑本脚本刷新（零网络，可随时重跑）。
- 行配对：(fund, date) 双侧都有才计入；a_T 不可算（缓存无 T 或 T-1）→ 跳过并计数。

零网络：只读 forecast_outputs/、data/klines/、data/intraday_features.jsonl。
输出：evidence/probes/d07_est_chg_error_band_<stamp>.json（机读）+ stdout（人读）。
用法：.\\.venv\\Scripts\\python.exe -X utf8 evidence\\probes\\d07_est_chg_error_band.py
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

FROZEN = BASE / "forecast_outputs" / "samples_frozen_20260910.jsonl"
KLINES = BASE / "data" / "klines"
STORE = BASE / "data" / "intraday_features.jsonl"
LIVE_SINCE = "2026-09-23"          # == pit1455_contract.EST_CHG_LIVE_FRACTION_SINCE
OUT = Path(__file__).with_name("d07_est_chg_error_band_20260930.json")


def load_navs(code: str) -> dict[str, tuple[int, float]] | None:
    """缓存 navs → {date: (index, nav)}；缺失/损坏 → None。"""
    p = KLINES / f"{code}.json"
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    out: dict[str, tuple[int, float]] = {}
    for i, row in enumerate(raw.get("navs") or []):
        try:
            out[str(row[0])[:10]] = (i, float(row[1]))
        except (IndexError, TypeError, ValueError):
            continue
    return out or None


def a_t(navs: dict[str, tuple[int, float]], code: str, date: str,
        seq: list[tuple[str, float]] | None = None):
    """a_T = navs[T]/navs[T-1] - 1；不可算 → None。seq 为升序 (date, nav) 列表。"""
    hit = navs.get(date)
    if hit is None:
        return None
    i, _ = hit
    if i < 1:
        return None
    prev_date, prev_nav = seq[i - 1]
    cur_nav = seq[i][1]
    if prev_nav <= 0:
        return None
    return cur_nav / prev_nav - 1.0


def dist(errs: list[float]) -> dict:
    """|err| 分布统计（errs 为带符号误差列表）。"""
    if not errs:
        return {"n": 0}
    abs_e = sorted(abs(x) for x in errs)
    n = len(abs_e)

    def pct(p: float) -> float:
        k = min(n - 1, max(0, int(round(p * (n - 1)))))
        return round(abs_e[k], 6)

    return {"n": n,
            "mean_abs": round(statistics.fmean(abs_e), 6),
            "p50_abs": pct(0.50), "p90_abs": pct(0.90),
            "max_abs": round(abs_e[-1], 6),
            "mean_signed": round(statistics.fmean(errs), 6)}


def run_track(pairs: list[tuple[str, str, float]], label: str) -> dict:
    """pairs = [(fund, date, est_chg_fraction)] → 误差分布 + 逐基金拆分。"""
    cache: dict[str, object] = {}
    errs: list[float] = []
    per_fund: dict[str, list[float]] = {}
    skipped = {"no_cache": 0, "no_a_t": 0}
    for fund, date, est in pairs:
        if fund not in cache:
            navs = load_navs(fund)
            seq = sorted(((d, v) for d, (_i, v) in navs.items())) if navs else None
            cache[fund] = (navs, seq)
        navs, seq = cache[fund]
        if navs is None:
            skipped["no_cache"] += 1
            continue
        at = a_t(navs, fund, date, seq)
        if at is None:
            skipped["no_a_t"] += 1
            continue
        e = est - at
        errs.append(e)
        per_fund.setdefault(fund, []).append(e)
    out = {"label": label, "overall": dist(errs), "skipped": skipped,
           "n_dates": len({d for _f, d, _e in pairs}),
           "per_fund": {f: dist(v) for f, v in sorted(per_fund.items())}}
    return out


def main() -> int:
    # 轨 1：冻结池（canonical 冻结件，G-A 锁定内容）
    frozen_pairs = []
    for line in FROZEN.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        s = json.loads(line)
        if s.get("est_chg") is not None and s.get("date") and s.get("fund"):
            frozen_pairs.append((s["fund"], s["date"], float(s["est_chg"])))
    t1 = run_track(frozen_pairs, "frozen_pool(close-approx est_chg)")

    # 轨 2：live fraction 行（切换日后 post 槽；PROVISIONAL，n_dates < 30）
    live_pairs = []
    if STORE.is_file():
        for line in STORE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if (r.get("date", "") >= LIVE_SINCE and r.get("slot") == "post"
                    and (r.get("features") or {}).get("est_chg") is not None):
                live_pairs.append((r["fund"], r["date"],
                                   float(r["features"]["est_chg"])))
    t2 = run_track(live_pairs, "live_post(fraction, PROVISIONAL)")
    t2["provisional_reason"] = ("切换日 2026-09-23 起 live fraction 行仅 "
                                f"{t2['n_dates']} 个交易日 < 验收要求 30；"
                                "满 30 日后重跑本脚本刷新（零网络）")

    result = {"kind": "d07_est_chg_error_band", "produced_at": "2026-09-30",
              "spec": "err = est_chg - a_T; a_T = navs[T]/navs[T-1]-1（缓存官方净值）",
              "tracks": [t1, t2]}
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=1))
    print(f"\n[d07] 机读件 → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
