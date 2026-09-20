#!/usr/bin/env python3
"""Leave-One-Fund-Out 跨基金泛化验证（2026-08-30，GPT 四审 P1-⑬ 落地）。

问题：pooled 模型主要学到 002112/002207 的行为（各 1522 样本，占全池 91%），
025687 仅 27 样本。模型究竟在学习「普遍规律」还是「某两只基金的脾气」？
LOFO 逐基金留出验证回答这个问题。

方法（与 backtest_forecast 同口径）：
1. 样本/切分：load_samples() → split_date_oos（label-end purge 冻结纪律），
   train 段 = 全池 oos_start 之前样本（含 purge），OOS 段 = oos_start 之后样本。
2. 每折：留出基金 F，训练集 = 其余基金 **train 段**样本（OOS 永不入训），
   测试集 = F 的 **OOS 段**样本。
3. 每基金 × 每周期：HGBClassifier（超参与 backtest_forecast 相同：
   max_iter=200, lr=0.08, max_depth=3, early_stopping, rs=42）→ OOS Rank IC
   + cluster bootstrap 95% CI（按日块）+ Brier + Calibration ACE。
4. 功效标注：OOS 样本 < 100 的基金（025687: 27）CI 宽，仅作「警示信号」，
   不作「判决」；大样本互测（002112↔002207，跨行业）是主证据。

判定规则（跑前冻结，2026-08-30，不因结果回头改）：
- 以 T+5 为该基金稳定性映射的主判据（档案状态：2026-09-01 冻结时点为全池唯一显著周期；
  09-18 C2 WF 部署式复算 ≈ -0.079，当前结论 UNRESOLVED，V4.3 P0-4）：
    n_oos >= 100 且 CI 下界 > 0 → 「泛化成立」    → 稳定性 0.85
    n_oos >= 100 且 CI 上界 < 0 → 「泛化不成立」  → 稳定性封顶 0.2
    n_oos >= 100 且 CI 跨零     → 「泛化证据不足」→ 稳定性 0.6
    n_oos < 100                → 「证据不足（小样本）」→ 稳定性 0.5
- 无 LOFO 条目的基金 → 稳定性 0.5（诚实中性，不再默认 0.85）。
- 映射函数 stability_from_evidence 是唯一事实来源，confidence 消费它，
  不因某次运行结果临时改判。

时序口径：train_all = 全池 < global OOS_START；留出基金 F 的 OOS 段（≥ OOS_START）
不入训练。其他基金与测试基金共享同一市场时序——对绝大多数测试基金，训练集并未用到
其 OOS 之后的市场日期，因此这不是「未来日期泄漏」，而是「跨基金泛化证据，非独立
市场环境下的因果泛化证据」：市场共同因子可能经其他基金样本被模型学到。结论措辞
一律用「泛化证据」而非「因果证据」。

输出：data/model_registry/lofo_evidence.json（供 confidence 消费）
      + output/backtest_lofo_20260830.md（人类可读报告）

用法：python3 backtest_lofo.py
"""
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

from backtest_spread import load_samples
from frozen_dataset import resolve_samples   # V4.3 P0-1：统一冻结样本入口
from backtest_forecast import (split_date_oos, build_xy, rank_ic,
                               brier_multiclass, calibration_curve,
                               cluster_bootstrap_ci)
from core import forecast_engine

RNG_SEED = 42
MIN_N_OOS_POWER = 100          # 功效门槛：低于此只当警示信号


def stability_from_evidence(n_oos: int | None, ci_lo: float | None,
                            ci_hi: float | None) -> float:
    """冻结映射（2026-08-30）：T+5 证据 → 稳定性。唯一事实来源。"""
    if n_oos is None or ci_lo is None or ci_hi is None:
        return 0.5
    if n_oos < MIN_N_OOS_POWER:
        return 0.5
    if ci_lo > 0:
        return 0.85
    if ci_hi < 0:
        return 0.2
    return 0.6


def verdict_from_evidence(n_oos: int | None, ci_lo: float | None,
                          ci_hi: float | None) -> str:
    if n_oos is None or ci_lo is None or ci_hi is None:
        return "无 T+5 证据"
    if n_oos < MIN_N_OOS_POWER:
        return "证据不足（小样本，仅警示）"
    if ci_lo > 0:
        return "跨基金泛化成立"
    if ci_hi < 0:
        return "跨基金泛化不成立"
    return "跨基金泛化证据不足（CI 跨零）"


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=None,
                    help="冻结样本 jsonl（默认自动选最新 forecast_outputs/samples_frozen_*.jsonl）")
    ap.add_argument("--fresh", action="store_true",
                    help="显式活拉样本（数字与冻结基线不可比；报告标 FRESH）")
    args = ap.parse_args()

    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    fc = cfg.get("forecast", {})
    horizons = fc.get("horizons", [1, 3, 5])
    flat_margin = fc.get("prob_flat_margin", 0.003)

    print("== [0] 加载样本 ==")
    samples, snap_info = resolve_samples(args.snapshot, args.fresh, BASE_DIR, load_samples)
    if snap_info["mode"] in ("MISSING", "INVALID"):
        return 4
    samples_sorted = sorted(samples, key=lambda s: (s["date"], s["fund"]))
    by_fund = defaultdict(int)
    for s in samples_sorted:
        by_fund[s["fund"]] += 1
    print("  按基金样本量:", dict(by_fund))
    funds = sorted(by_fund)
    if not funds:
        print("[fail] 无基金样本")
        return 1

    train_all, oos, oos_start = split_date_oos(samples_sorted)
    print(f"== [1] 冻结切分：train < {oos_start}（{len(train_all)}），OOS ≥ {oos_start}（{len(oos)}）==")

    t0 = time.time()
    evidence: dict[str, dict] = {}
    lines = [
        "# LOFO 跨基金泛化验证（v8 证据留档）", "",
        snap_info["report_line"],
        f"> 生成：{time.strftime('%Y-%m-%d %H:%M')} · 切分 OOS ≥ {oos_start} · "
        f"pooled 全池样本 {len(samples_sorted)} · 判定规则跑前冻结（docstring）", "",
        "| 留出基金 | OOS样本 | T+5 RankIC | T+5 CI(95%) | 判词 | 稳定性映射 |",
        "|---|---:|---:|---|---:|---:|",
    ]

    for F in funds:
        train_F = [s for s in train_all if s["fund"] != F]
        oos_F = [s for s in oos if s["fund"] == F]
        print(f"\n=== 留出 {F}（train={len(train_F)}，OOS={len(oos_F)}）===")
        if len(oos_F) < 10:
            print("  OOS 样本 < 10 → 跳过")
            continue
        fund_ev: dict[str, dict] = {}
        for h in horizons:
            XY = build_xy(train_F, h, flat_margin)
            XYo = build_xy(oos_F, h, flat_margin)
            if XY is None or len(XY[1]) < 100 or XYo is None or len(XYo[1]) < 10:
                print(f"  T+{h} 样本不足（train={len(XY[1]) if XY else 0}, "
                      f"oos={len(XYo[1]) if XYo else 0}）→ 跳过")
                continue
            X, y, yret = XY
            Xo, yo, yreto = XYo
            clf = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.08,
                                                 max_depth=3, early_stopping=True,
                                                 random_state=RNG_SEED)
            clf.fit(X, y)
            po = clf.predict_proba(Xo)
            p_up = po[:, 2]
            y_up = (yo == 2).astype(int)
            calib = calibration_curve(y_up, p_up)
            ric = rank_ic(p_up.tolist(), yreto.tolist())
            oo_brier = brier_multiclass(yo, po)
            aligned_dates = XYo.dates
            ric_ci = cluster_bootstrap_ci(
                lambda sub: rank_ic(sub["x"].tolist(), sub["y"].tolist()),
                {"x": p_up, "y": yreto}, aligned_dates)
            ci_lo = float(ric_ci[0]) if ric_ci[0] == ric_ci[0] else None
            ci_hi = float(ric_ci[1]) if ric_ci[1] == ric_ci[1] else None
            print(f"  T+{h} RankIC={ric:+.3f} CI=[{ci_lo if ci_lo is not None else 'nan'}, "
                  f"{ci_hi if ci_hi is not None else 'nan'}] Brier={oo_brier:.3f} "
                  f"ACE={calib['ace']:.3f} (n={len(oos_F)})")
            fund_ev[str(h)] = {
                "n_oos": len(oos_F),
                "rank_ic": round(float(ric), 4),
                "ci_lo": round(ci_lo, 4) if ci_lo is not None else None,
                "ci_hi": round(ci_hi, 4) if ci_hi is not None else None,
                "oos_brier": round(float(oo_brier), 4),
                "ace": round(float(calib["ace"]), 4),
            }

        # 冻结映射：以 T+5 为主判据
        ev5 = fund_ev.get("5")
        n5 = ev5.get("n_oos") if ev5 else None
        lo5 = ev5.get("ci_lo") if ev5 else None
        hi5 = ev5.get("ci_hi") if ev5 else None
        stab = stability_from_evidence(n5, lo5, hi5)
        verdict = verdict_from_evidence(n5, lo5, hi5)
        evidence[F] = {"stability": stab, "verdict": verdict, "horizons": fund_ev}
        ci_str = f"[{lo5:+.3f}, {hi5:+.3f}]" if lo5 is not None else "—"
        lines.append(f"| {F} | {n5 if n5 is not None else '—'} | "
                     f"{ev5['rank_ic'] if ev5 else '—':+} | {ci_str} | {verdict} | {stab} |")
        print(f"  → {F} 稳定性映射 {stab}（{verdict}）")

    # 写证据文件（confidence 消费）
    ev_path = BASE_DIR / "data" / "model_registry" / "lofo_evidence.json"
    ev_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "oos_start": oos_start, "rule": "frozen 2026-08-30, T+5 主判据",
               "funds": evidence}
    ev_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[ok] 证据 → {ev_path}")

    # 人类可读报告
    lines += ["", "<sub>功效说明：OOS 样本 < 100 的基金（025687: 27）CI 宽，仅作警示信号；"
              "大样本互测（002112↔002207）是主证据。结论措辞为「泛化证据」非「因果证据」"
              "（跨基金泛化证据，非独立市场环境因果证据；其他基金与测试基金共享市场时序）。</sub>", ""]
    report = "\n".join(lines) + "\n"
    out_md = BASE_DIR / "output" / "backtest_lofo_20260830.md"
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(report, encoding="utf-8")
    print(f"[ok] 报告 → {out_md}")
    print(f"[done] 耗时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
