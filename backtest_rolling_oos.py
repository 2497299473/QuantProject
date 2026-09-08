#!/usr/bin/env python3
"""Rolling OOS 验证：单一模型在 OOS 段的分窗 IC 轨迹（2026-08-31，GPT 四审 P0①落地）。

回答：「T+5 的 +0.080 是一直稳定，还是最近才出现？」——把 OOS 段按交易日滚动
窗口切片，逐窗算 RankIC / Brier / CI，看证据的时间结构。单一切口只能给均值，
分窗才能看出「什么时候开始失效」。

口径（与 train_forecast_model / backtest_forecast 完全同口径）：
- 训练：全池 < global OOS_START（split_date_oos 冻结切分，label-end purge）
- 模型：每 horizon 一个 HGB（同 backtest_forecast 超参），在 train 上训一次
  → 所有窗共用同一模型 = 回答「这个模型在哪个时间段还有效」
- 窗口：OOS 段按交易日等分（--window-days 控制窗宽，默认 63 ≈ 一季度）
- 指标：每窗 RankIC + cluster bootstrap 95% CI（按日块重抽样）+ Brier
- 主判据：T+5（当前唯一有证据的周期）；T+1/T+3 仅点估计参考

输出：
- output/backtest_rolling_oos_YYYYMMDD.md（人类可读）
- 同 .log（终端 + 留档）

用法：python3 backtest_rolling_oos.py [--window-days 63] [--n-boot 199]
"""
import json
import math
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import numpy as np

from backtest_spread import load_samples
from backtest_forecast import (split_date_oos, build_xy, rank_ic,
                               brier_multiclass, cluster_bootstrap_ci)
from core import forecast_engine


def _window_slices(dates: list[str], window_days: int) -> list[list[str]]:
    """OOS 日期序列按交易日等分窗口。返回每窗的日期列表。"""
    uniq = sorted(set(dates))
    n = len(uniq)
    if n == 0:
        return []
    k = max(1, n // window_days)
    out = []
    for i in range(k):
        lo = i * n // k
        hi = (i + 1) * n // k if i < k - 1 else n
        out.append(uniq[lo:hi])
    return out


def _fit_one_horizon(train, h, flat_margin):
    """在 train 上训一个 horizon 的 HGB 分类器。"""
    from sklearn.ensemble import HistGradientBoostingClassifier
    XY = build_xy(train, h, flat_margin)
    if XY is None or len(XY[1]) < 100:
        return None, None
    X, y, _ = XY
    clf = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.08,
                                         max_depth=3, early_stopping=True,
                                         random_state=42)
    clf.fit(X, y)
    return clf, y


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-days", type=int, default=63)
    ap.add_argument("--n-boot", type=int, default=199)
    args = ap.parse_args()

    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    fc = cfg.get("forecast", {})
    horizons = fc.get("horizons", [1, 3, 5])
    flat_margin = fc.get("prob_flat_margin", 0.003)
    t0 = time.time()

    print("== [0] 加载样本 ==")
    samples = load_samples()
    samples_sorted = sorted(samples, key=lambda s: (s["date"], s["fund"]))
    print(f"  总样本 {len(samples_sorted)}")

    train_all, oos, oos_start = split_date_oos(samples_sorted)
    print(f"== [1] 冻结切分：train < {oos_start}（{len(train_all)}），OOS ≥ {oos_start}（{len(oos)}）==")
    if not oos:
        print("[fail] 无 OOS 样本")
        return 1

    oos_dates = sorted({s["date"] for s in oos})
    windows = _window_slices(oos_dates, args.window_days)
    print(f"== [2] OOS 分 {len(windows)} 窗（{args.window_days} 交易日/窗）："
          f"{windows[0][0]} ~ {windows[-1][-1]} ==")
    if len(windows) < 2:
        print("[warn] 窗口数 < 2，轨迹无法判断趋势（仅单窗点估计）")

    # 每 horizon 训一次模型（全窗共用）
    clfs = {}
    for h in horizons:
        clf, _ = _fit_one_horizon(train_all, h, flat_margin)
        clfs[h] = clf
        print(f"  T+{h} 模型：{'就绪' if clf is not None else '样本不足跳过'}")

    lines = [
        "# Rolling OOS 分窗验证（2026-08-31，GPT 四审 P0①）", "",
        f"> 生成：{time.strftime('%Y-%m-%d %H:%M')} · 训练 < {oos_start}（{len(train_all)}）"
        f" · OOS 分 {len(windows)} 窗（{args.window_days} 交易日/窗）· bootstrap {args.n_boot} 次", "",
        "## 一、T+5 主判据：分窗 RankIC 轨迹", "",
        "| 窗口 | 区间 | 样本 | T+5 RankIC | 95% CI | Brier |",
        "|---|---:|---:|---:|---|---:|---:|",
    ]

    rows_by_h = {h: [] for h in horizons}
    for wi, wd in enumerate(windows, 1):
        oos_w = [s for s in oos if s["date"] in set(wd)]
        print(f"\n=== 窗 {wi}/{len(windows)}：{wd[0]} ~ {wd[-1]}（{len(oos_w)} 样本）===")
        row5 = None
        for h in horizons:
            clf = clfs[h]
            if clf is None:
                continue
            XYo = build_xy(oos_w, h, flat_margin)
            if XYo is None or len(XYo[1]) < 10:
                print(f"  T+{h} 样本不足 → 跳过")
                continue
            Xo, yo, yreto = XYo
            po = clf.predict_proba(Xo)
            p_up = po[:, 2]
            ric = rank_ic(p_up.tolist(), yreto.tolist())
            brier = brier_multiclass(yo, po)
            aligned = [s["date"] for s in oos_w
                       if s.get(f"fwd{h}") is not None
                       and all(s.get(k) is not None for k in forecast_engine.FEATURE_KEYS)]
            if h == 5:
                ric_ci = cluster_bootstrap_ci(
                    lambda sub: rank_ic(sub["x"].tolist(), sub["y"].tolist()),
                    {"x": p_up, "y": yreto}, aligned, n_boot=args.n_boot)
                ci_lo = float(ric_ci[0]) if ric_ci[0] == ric_ci[0] else None
                ci_hi = float(ric_ci[1]) if ric_ci[1] == ric_ci[1] else None
                row5 = dict(ci_lo=ci_lo, ci_hi=ci_hi, ric=ric, brier=brier, n=len(yo))
                print(f"  T+5 RankIC={ric:+.3f} CI=[{ci_lo if ci_lo is not None else 'nan':}, "
                      f"{ci_hi if ci_hi is not None else 'nan':}] Brier={brier:.3f}")
            else:
                print(f"  T+{h} RankIC={ric:+.3f} Brier={brier:.3f}")
            rows_by_h[h].append({"window": wi, "ric": ric, "brier": brier})
        if row5:
            ci_str = (f"[{row5['ci_lo']:+.3f}, {row5['ci_hi']:+.3f}]"
                      if row5["ci_lo"] is not None else "—")
            lines.append(f"| {wi} | {wd[0]} ~ {wd[-1]} | {row5['n']} | "
                         f"{row5['ric']:+.3f} | {ci_str} | {row5['brier']:.3f} |")

    # ---- 判定（T+5 主判据）----
    lines += ["", "## 二、趋势判定（T+5）", ""]
    r5 = rows_by_h.get(5) or []
    if len(r5) >= 2:
        pos = sum(1 for r in r5 if r["ric"] > 0)
        first, last = r5[0]["ric"], r5[-1]["ric"]
        last_pos = r5[-1]["ric"] > 0
        stable = pos / len(r5) >= 0.6 and last_pos
        verdict = ("✅ 证据稳定（多数窗口为正且最近窗口仍为正）" if stable
                   else "⚠️ 证据衰减或集中在早期（最近窗口转弱）")
        lines += [f"- 正窗口比例：{pos}/{len(r5)}", f"- 首窗 vs 末窗：{first:+.3f} → {last:+.3f}",
                  f"- **{verdict}**", ""]
        print(f"\n判定：正窗口 {pos}/{len(r5)}，首→末 {first:+.3f} → {last:+.3f} → {verdict}")
    else:
        lines += ["- 窗口不足 2 个，无法判定趋势（单窗点估计见上表）", ""]

    # ---- 各周期汇总表 ----
    lines += ["## 三、各周期分窗 RankIC", "",
              "| 窗 | T+1 | T+3 | T+5 |", "|---:|---:|---:|---:|"]
    for wi in range(1, len(windows) + 1):
        cell = []
        for h in horizons:
            hit = next((r for r in rows_by_h[h] if r["window"] == wi), None)
            cell.append(f"{hit['ric']:+.3f}" if hit else "—")
        lines.append(f"| {wi} | " + " | ".join(cell) + " |")
    lines.append("")

    lines += ["<sub>口径：单模型（全 train 段训练）在 OOS 段分窗评估，回答「这个模型何时失效」；"
              "每窗 CI 为按交易日块 cluster bootstrap。T+1/T+3 仅点估计。"
              "若要回答「不同训练起点下表现」，需 walk-forward 重训（后续迭代）。</sub>", ""]
    report = "\n".join(lines) + "\n"

    stamp = time.strftime("%Y%m%d")
    out_md = BASE_DIR / "output" / f"backtest_rolling_oos_{stamp}.md"
    out_md.write_text(report, encoding="utf-8")
    log = BASE_DIR / "output" / f"backtest_rolling_oos_{stamp}.log"
    log.write_text(report, encoding="utf-8")
    print(f"\n[ok] 报告 → {out_md}")
    print(f"[done] 耗时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
