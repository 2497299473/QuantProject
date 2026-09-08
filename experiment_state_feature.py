#!/usr/bin/env python3
"""State→Forecast 增量实验（2026-08-29，GPT 三审 P2）。

问题：「当前结构状态」特征能否提高 T+1/T+3/T+5 的 OOS 预测质量？
方法：同一 purge 切分、同一 CV/裁决口径，两组对照：
  A（基线）：现 7 特征（est_chg/est_sign/breadth/concentration/covered_pct/composite/score）
  B（+state）：A + 4 个状态数值编码（trend/pos/bucket/event）
判定（与 v5.1/v5.2 先例同哲学）：
  - B 的 OOS RankIC 三周期全部 ≥ A，且至少一个周期显著提升 → 有增量，报 Summer 决策
  - 任一周期恶化超过 0.01 → 判过拟合风险，维持 A（不动 FEATURE_KEYS）
纪律：
  - 状态特征 PIT：14:55 决策时 d 日净值未公布 → 状态用 navs[:i]（截至 i-1）前缀重放，
    与 _nav_state_at 的 T-1 口径对齐；
  - 本脚本只产证据，不改 forecast_engine.FEATURE_KEYS（改动需 Summer 拍板）。
用法：python3 experiment_state_feature.py
"""
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import numpy as np

from backtest_spread import load_samples
from backtest_forecast import split_date_oos, build_xy, rank_ic, brier_multiclass
from core import forecast_engine as fe
from core import state_engine as se

# 状态数值编码（缺失/na → 0，维度恒定）
TREND_NUM = {"up": 1, "down": -1, "consolidation": 0, "expand": 0, "na": 0}
POS_NUM = {"above": 1, "inside": 0, "below": -1, "none": 0}
BUCKET_NUM = {"fresh": 1, "stale": -1}
EVENT_NUM = {"1buy": 1, "2buy": 1, "3buy": 1,
             "1sell": -1, "2sell": -1, "3sell": -1}


def add_state_features(samples: list[dict]) -> list[dict]:
    """给样本补 4 个状态数值特征（PIT：截至 d-1 净值前缀重放）。

    口径说明：states_for_fund 内部对日 i 的状态用 navs[:i]（截至 i-1 前缀）重放，
    与 _nav_state_at 的 T-1 公布口径对齐（14:55 决策时 d 日净值未公布）。
    全序列一次算完（~1-2 分钟/大基金），避免逐样本 O(n²) 重复。
    """
    from collections import defaultdict
    by_fund: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        by_fund[s["fund"]].append(s)
    t0 = time.time()
    for code, rows in by_fund.items():
        try:
            from core import data_loader
            navs = data_loader.load_fund(code)["navs"]
        except Exception as e:
            print(f"  [warn] {code} 净值加载失败：{e}")
            for s in rows:
                s.update(state_trend=0, state_pos=0, state_bucket=0, state_event=0)
            continue
        try:
            states = se.states_for_fund(navs)
        except Exception as e:
            print(f"  [warn] {code} 状态重放失败：{e}")
            for s in rows:
                s.update(state_trend=0, state_pos=0, state_bucket=0, state_event=0)
            continue
        date_to_st = {st["date"]: st for st in states}
        for s in rows:
            st = date_to_st.get(s["date"])
            if not st:
                s.update(state_trend=0, state_pos=0, state_bucket=0, state_event=0)
                continue
            # bucket 从 state 串解析（state_at 返回 dict 不含独立 bucket 键）
            bucket = None
            if "~fresh" in st["state"]:
                bucket = "fresh"
            elif "~stale" in st["state"]:
                bucket = "stale"
            s["state_trend"] = TREND_NUM.get(st.get("trend", "na"), 0)
            s["state_pos"] = POS_NUM.get(st.get("pos", "none"), 0)
            s["state_bucket"] = BUCKET_NUM.get(bucket, 0) if bucket else 0
            s["state_event"] = EVENT_NUM.get(st.get("event"), 0) if st.get("event") else 0
        print(f"  {code}: {len(rows)} 样本状态补全（耗时 {time.time()-t0:.0f}s 累计）")
    return samples


def run_variant(name: str, samples: list[dict], extra_keys: list[str],
                flat_margin: float) -> dict[int, dict]:
    """跑一个特征变体的完整验证（同 backtest_forecast 口径）。"""
    fe.FEATURE_KEYS = list(fe.BASE_FEATURE_KEYS) + extra_keys
    from sklearn.ensemble import HistGradientBoostingClassifier
    train, oos, oos_start = split_date_oos(samples)
    out = {}
    for h in (1, 3, 5):
        XY = build_xy(train, h, flat_margin)
        XYo = build_xy(oos, h, flat_margin)
        if XY is None or XYo is None or len(XY[1]) < 100 or len(XYo[1]) < 30:
            out[h] = {"ok": False, "reason": "insufficient"}
            continue
        X, y, yret = XY
        Xo, yo, yreto = XYo
        clf = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.08,
                                             max_depth=3, early_stopping=True, random_state=42)
        clf.fit(X, y)
        po = clf.predict_proba(Xo)
        ric = rank_ic(po[:, 2].tolist(), yreto.tolist())
        brier = brier_multiclass(yo, po)
        out[h] = {"rank_ic": round(ric, 4), "brier": round(brier, 4), "n_oos": len(yo)}
        print(f"  [{name}] T+{h}: RankIC={ric:+.4f} Brier={brier:.4f}")
    return out


def main() -> int:
    print("== [1] 加载样本（PIT 口径）==")
    samples = load_samples()
    if not samples:
        print("[fail] 无样本")
        return 1
    print("== [2] 补状态特征（PIT 前缀重放，耗时较长）==")
    add_state_features(samples)

    flat_margin = 0.003
    print("\n== [3] 变体 A（基线 7 特征）==")
    res_a = run_variant("A", samples, [], flat_margin)
    print("\n== [4] 变体 B（+4 状态特征）==")
    res_b = run_variant("B", samples,
                        ["state_trend", "state_pos", "state_bucket", "state_event"],
                        flat_margin)

    # 判定
    print("\n== [5] 对照结论 ==")
    print("  T+1: A={:+.4f}  B={:+.4f}  Δ={:+.4f}".format(
        res_a[1]["rank_ic"], res_b[1]["rank_ic"],
        res_b[1]["rank_ic"] - res_a[1]["rank_ic"]))
    print("  T+3: A={:+.4f}  B={:+.4f}  Δ={:+.4f}".format(
        res_a[3]["rank_ic"], res_b[3]["rank_ic"],
        res_b[3]["rank_ic"] - res_a[3]["rank_ic"]))
    print("  T+5: A={:+.4f}  B={:+.4f}  Δ={:+.4f}".format(
        res_a[5]["rank_ic"], res_b[5]["rank_ic"],
        res_b[5]["rank_ic"] - res_a[5]["rank_ic"]))
    deltas = [res_b[h]["rank_ic"] - res_a[h]["rank_ic"] for h in (1, 3, 5)]
    any_worse = any(d < -0.01 for d in deltas)
    all_better = all(d >= 0 for d in deltas)
    if any_worse:
        print("  → 判定：B 存在周期恶化 >0.01 → 过拟合风险，维持基线 A（不改 FEATURE_KEYS）")
    elif all_better and any(d > 0.01 for d in deltas):
        print("  → 判定：B 全周期不劣且至少一周期显著提升 → 有增量，报 Summer 决策是否接入")
    else:
        print("  → 判定：B 无显著增量（变化 ≤0.01）→ 维持基线 A")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
