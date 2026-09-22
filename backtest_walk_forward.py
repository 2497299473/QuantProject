#!/usr/bin/env python3
"""Walk-Forward（expanding window 重训）OOS 验证（2026-09-01，GPT 五审 P0）。

回答 Rolling OOS 回答不了的问题：
  Rolling OOS = 单一冻结模型切 OOS 段，回答「这个模型何时失效」；
  Walk-Forward = 模拟真实部署：每个新测试窗前，用「当时可得的全部历史数据」
  （含 OOS 段已过去的部分）重训模型，再预测该测试窗。

口径（严格 PIT，不改动冻结切分纪律）：
- 冻结全局切分仍用 split_date_oos（train < oos_start，label-end purge）——
  frozen 基线列就用该 train_all 训一个模型（与 Rolling OOS 同一模型）。
- WF 的 K 个测试窗与 Rolling OOS 同切法（--window-days 63，窗口一一对应可比）：
  Fold k: test = 窗 k；train_k = date < cutoff_k 的全部样本，
  cutoff_k = 全局交易日序列第 (j_k - max_horizon) 个交易日（j_k = 窗 k 起点全局下标）。
  即 train_k 含 OOS 段中窗 k 之前的数据（部署模拟：那些数据当时已是历史）。
  纪律不变：train 样本 label_end < 测试窗起点（label-end purge，
  max_horizon 取 config.forecast.horizons 最大值）。
  注意：WF 的展开训练会用到 OOS 段前期数据——这是 WF 协议的本意（模拟部署时
  数据持续积累），不修改冻结切分 OOS 结论；两者是不同问题协议，都保留。
- 模型：每 horizon 一个 HGB（同 backtest_forecast / rolling_oos 超参），每折重训。
- 指标：每折 RankIC + Brier；pooled WF RankIC = 各折预测与真值拼接后的整体
  RankIC + cluster bootstrap CI（按日块）= 部署式 OOS 主判据。

主判据解读（T+5）：
  frozen 末窗 = -0.051（08-31 已知）。若 WF 末折（用数据重训到 2026-04）仍 ≤ 0
  → 衰减不是「模型过期」能救的，指向 regime 变化；若转正 → 衰减部分来自模型
  冻结过早。

预注册判定（T+5 主判据，事先写死避免事后调整）：
  ✅ wf_stable  : WF pooled RankIC 95% CI 下界 > 0 且 最近折点估计 > 0
  ⚠️ wf_partial : WF pooled CI 下界 > 0 但 最近折 ≤ 0（重训救不回最近窗）
  ❌ wf_fail    : WF pooled CI 下界 ≤ 0（部署式重训下优势消失）

输出：output/backtest_walk_forward_YYYYMMDD.md（+ 同 .log）
用法：python3 backtest_walk_forward.py [--window-days 63] [--n-boot 199]
"""
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import numpy as np

from backtest_spread import load_samples
from frozen_dataset import resolve_samples   # V4.3 P0-1：统一冻结样本入口
from backtest_forecast import (split_date_oos, build_xy, rank_ic,
                               brier_multiclass, cluster_bootstrap_ci)
from core import forecast_engine


def _window_slices(dates: list[str], window_days: int) -> list[list[str]]:
    """OOS 日期序列按交易日等分窗口（与 backtest_rolling_oos 同切法，窗口可比）。"""
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


def build_wf_folds(samples: list[dict], oos: list[dict], oos_start: str,
                   window_days: int = 63, max_horizon: int | None = None,
                   min_train_samples: int = 100) -> list[dict]:
    """构造 Walk-Forward fold 列表（纯函数，供 main 与单测共用）。

    返回按时间排序的 [{window, test_start, cutoff, train, test}]：
    - window: 该折测试窗的交易日列表（与 rolling OOS 同切分）
    - test_start: 窗起点；cutoff: date < cutoff 的样本可入训练
      （label-end purge：train 样本 label_end < test_start）
    - train: date < cutoff 的全部样本（expanding，含该折之前的 OOS 数据）
    - test: 测试窗内的样本
    历史不足（j - max_horizon < 1）或训练样本过少的折跳过。
    """
    dates_all = sorted({s["date"] for s in samples})
    idx = {d: i for i, d in enumerate(dates_all)}
    if max_horizon is None:
        _fc = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))\
            .get("forecast", {})
        max_horizon = max(_fc.get("horizons", [1, 3, 5]))
    oos_dates = sorted({s["date"] for s in oos})
    windows = _window_slices(oos_dates, window_days)
    folds = []
    for wd in windows:
        test_start = wd[0]
        j = idx[test_start]
        if j - max_horizon < 1:
            continue                      # 历史不足，无法构造合法 train
        cutoff = dates_all[j - max_horizon]
        train = [s for s in samples if s["date"] < cutoff]
        if len(train) < min_train_samples:
            continue
        test = [s for s in samples if s["date"] in set(wd)]
        folds.append({"window": wd, "test_start": test_start, "cutoff": cutoff,
                      "train": train, "test": test})
    return folds


def _fit_one_horizon(train: list[dict], h: int, flat_margin: float):
    """同 backtest_forecast / rolling_oos 超参，训一个 horizon 的 HGB。"""
    from sklearn.ensemble import HistGradientBoostingClassifier
    XY = build_xy(train, h, flat_margin)
    if XY is None or len(XY[1]) < 100:
        return None
    X, y, _ = XY
    clf = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.08,
                                         max_depth=3, early_stopping=True,
                                         random_state=42)
    clf.fit(X, y)
    return clf


def _eval(clf, test: list[dict], h: int, flat_margin: float):
    """测试窗评估 → (p_up, yret, dates, ric, brier)；样本不足返回 None。"""
    XY = build_xy(test, h, flat_margin)
    if XY is None or len(XY[1]) < 10:
        return None
    X, y, yret = XY
    po = clf.predict_proba(X)
    p_up = po[:, 2]
    ric = rank_ic(p_up.tolist(), yret.tolist())
    brier = brier_multiclass(y, po)
    return p_up, yret, XY.dates, ric, brier


def _path_eval_for_fold(f: dict) -> dict | None:
    """Path-WF（2026-09-01，P1-②）：部署式路径层校准。

    用 fold-train 重估池级 μ/σ（path_forecast v1.3：σ 窗口逐基金最近 20 交易日、
    RECENT_WINDOW 自 v1.3 冻结，每折原样复用——不允许折级调窗）→ 无条件 MC →
    对 fold-test 真 mdd5/mfe5 算命中率（期望 10%/50%/50%，与 calib 同口径）。
    返回 None = fold-train 数据不足或 fold-test 真标签缺失。
    """
    from core import path_forecast as pf
    params = pf.fit_path_params(f["train"], 5)
    if params is None:
        return None
    mu, sigma = params
    mcf = pf._mc_forecast(5, mu, sigma, 2000, 42,
                          {"bucket": "wf_fold", "n_state": None, "fallback": False})
    rows = [s for s in f["test"]
            if s.get("mdd5") is not None and s.get("mfe5") is not None]
    if len(rows) < 10:
        return None
    mdd = np.array([float(s["mdd5"]) for s in rows])
    mfe = np.array([float(s["mfe5"]) for s in rows])
    return {
        "mu": mu, "sigma": sigma, "n_test": len(rows),
        "mdd_q10": mcf.mdd_q10, "mdd_q50": mcf.mdd_q50, "mfe_q50": mcf.mfe_q50,
        "hit_mdd10": float(np.mean(mdd <= mcf.mdd_q10)),
        "hit_mdd50": float(np.mean(mdd <= mcf.mdd_q50)),
        "hit_mfe50": float(np.mean(mfe <= mcf.mfe_q50)),
        "mdd_p10": float(np.percentile(mdd, 10)),
        "mdd_p50": float(np.percentile(mdd, 50)),
        "mfe_p50": float(np.percentile(mfe, 50)),
        "mdd": mdd, "mfe": mfe,
    }


def _fold_cqr(f: dict, h: int, cal_frac: float = 0.2,
              min_cal: int = 50) -> dict | None:
    """P1-③（2026-09-01）：单折部署式 split-conformal CQR。

    calib 报告的 CQR 用 train 内 5 折日期 CV 的 qhat 平均、一次应用到整段
    OOS——校准点与使用点相距整个 OOS 跨度，不是部署时点协议。本函数在每个
    WF fold 内做标准 split conformal：
    - fold-train 按日期排序，尾部 cal_frac 切作 calib holdout（fit 段与
      cal 段时间不相交，模型只见 fit 段 → 无标签泄漏）；
    - qhat_k = cal 段非一致性得分（cqr_scores）的 (1-α) 经验分位；
    - fold-test 区间 = 折内模型 q10/q90 对称外扩 qhat_k。
    返回 None = fold-train/cal 数据不足或模型训练失败。
    """
    import math
    from backtest_quantile_calib import (cqr_scores, cqr_qhat,
                                         expand_interval, feat_row)
    from core.forecast_engine import ReturnQuantileModel
    tr = sorted((s for s in f["train"] if s.get(f"fwd{h}") is not None),
                key=lambda s: s["date"])
    n_cal = int(len(tr) * cal_frac)
    if len(tr) < 400 or len(tr) - n_cal < 300 or n_cal < min_cal:
        return None
    fit_s, cal_s = tr[:-n_cal], tr[-n_cal:]
    m = ReturnQuantileModel(h)
    if not m.fit(fit_s):
        return None
    pc = [m.predict_dist(feat_row(s)) for s in cal_s]
    sc = cqr_scores([float(s[f"fwd{h}"]) for s in cal_s],
                    [p["q10"] for p in pc], [p["q90"] for p in pc])
    qhat = cqr_qhat(sc)
    if not math.isfinite(qhat):
        return None
    te = [s for s in f["test"] if s.get(f"fwd{h}") is not None]
    if len(te) < 30:
        return None
    pt = [m.predict_dist(feat_row(s)) for s in te]
    lo_raw = np.array([p["q10"] for p in pt])
    hi_raw = np.array([p["q90"] for p in pt])
    lo, hi = expand_interval(lo_raw, hi_raw, qhat)
    y = np.array([float(s[f"fwd{h}"]) for s in te])
    return {"h": h, "qhat": qhat, "n_cal": len(cal_s), "n_test": len(te),
            "cov": float(np.mean((lo <= y) & (y <= hi))),
            "cov_raw": float(np.mean((lo_raw <= y) & (y <= hi_raw))),
            "lo": lo, "hi": hi, "lo_raw": lo_raw, "hi_raw": hi_raw, "y": y}


def _pooled_path(path_rows: list[dict]) -> dict | None:
    """Path-WF 跨折 pooled：各折预测拼接 vs 各折真值的部署式总命中率。"""
    if not path_rows:
        return None
    mdd_all = np.concatenate([r["mdd"] for r in path_rows])
    mfe_all = np.concatenate([r["mfe"] for r in path_rows])
    q10_all = np.concatenate([np.full(r["n_test"], r["mdd_q10"]) for r in path_rows])
    q50_all = np.concatenate([np.full(r["n_test"], r["mdd_q50"]) for r in path_rows])
    mfe50_all = np.concatenate([np.full(r["n_test"], r["mfe_q50"]) for r in path_rows])
    return {
        "n_folds": len(path_rows), "n_test": int(len(mdd_all)),
        "sigma_min": min(r["sigma"] for r in path_rows),
        "sigma_max": max(r["sigma"] for r in path_rows),
        "hit_mdd10": float(np.mean(mdd_all <= q10_all)),
        "hit_mdd50": float(np.mean(mdd_all <= q50_all)),
        "hit_mfe50": float(np.mean(mfe_all <= mfe50_all)),
        "mdd_p10": float(np.percentile(mdd_all, 10)),
        "mdd_p50": float(np.percentile(mdd_all, 50)),
        "mfe_p50": float(np.percentile(mfe_all, 50)),
    }


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-days", type=int, default=63)
    ap.add_argument("--n-boot", type=int, default=199)
    ap.add_argument("--snapshot", default=None,
                    help="冻结样本 jsonl（默认自动选最新 forecast_outputs/samples_frozen_*.jsonl）")
    ap.add_argument("--fresh", action="store_true",
                    help="显式活拉样本（数字与冻结基线不可比；报告标 FRESH）")
    args = ap.parse_args()

    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    fc = cfg.get("forecast", {})
    horizons = fc.get("horizons", [1, 3, 5])
    flat_margin = fc.get("prob_flat_margin", 0.003)
    max_h = max(horizons)
    t0 = time.time()

    print("== [0] 加载样本 ==")
    samples, snap_info = resolve_samples(args.snapshot, args.fresh, BASE_DIR, load_samples)
    if snap_info["mode"] in ("MISSING", "INVALID"):
        return 4
    samples_sorted = sorted(samples, key=lambda s: (s["date"], s["fund"]))
    print(f"  总样本 {len(samples_sorted)}")

    train_all, oos, oos_start = split_date_oos(samples_sorted)
    print(f"== [1] 冻结切分：train < {oos_start}（{len(train_all)}），OOS ≥ {oos_start}（{len(oos)}）==")
    if not oos:
        print("[fail] 无 OOS 样本")
        return 1

    folds = build_wf_folds(samples_sorted, oos, oos_start,
                           window_days=args.window_days, max_horizon=max_h)
    print(f"== [2] WF {len(folds)} 折（{args.window_days} 交易日/折，与 Rolling OOS 同窗；每折 expanding 重训）==")
    for i, f in enumerate(folds, 1):
        print(f"  折 {i}: test {f['window'][0]} ~ {f['window'][-1]}（train cutoff < {f['cutoff']}，n_train={len(f['train'])}）")
    if not folds:
        print("[fail] 无可用 WF 折")
        return 1

    # frozen 基线模型（train_all 训一次，全折共用 —— 与 Rolling OOS 同一模型）
    frozen = {}
    for h in horizons:
        frozen[h] = _fit_one_horizon(train_all, h, flat_margin)
        print(f"  T+{h} frozen 模型：{'就绪' if frozen[h] is not None else '样本不足跳过'}")

    # 逐折重训 + 评估
    fold_rows = []
    wf_all = {h: {"p": [], "y": [], "dates": []} for h in horizons}
    for fi, f in enumerate(folds, 1):
        print(f"\n=== 折 {fi}/{len(folds)}：{f['window'][0]} ~ {f['window'][-1]} ===")
        row = {"fold": fi, "window": f"{f['window'][0]} ~ {f['window'][-1]}",
               "n_train": len(f["train"]), "by_h": {}, "frozen": {}}
        for h in horizons:
            clf = _fit_one_horizon(f["train"], h, flat_margin)
            if clf is None:
                print(f"  T+{h} WF：训练样本不足 → 跳过")
            else:
                r = _eval(clf, f["test"], h, flat_margin)
                if r is None:
                    print(f"  T+{h} WF：测试样本不足 → 跳过")
                else:
                    p_up, yret, dts, ric, brier = r
                    print(f"  T+{h} WF RankIC={ric:+.3f} Brier={brier:.3f}（n={len(yret)}）")
                    row["by_h"][h] = {"ric": ric, "brier": brier, "n": len(yret)}
                    wf_all[h]["p"].extend(p_up.tolist())
                    wf_all[h]["y"].extend(yret.tolist())
                    wf_all[h]["dates"].extend(dts)
        # frozen 对照列（同一冻结模型在该窗的表现）
        for h in horizons:
            if frozen[h] is None:
                continue
            r = _eval(frozen[h], f["test"], h, flat_margin)
            if r is not None:
                row["frozen"][h] = r[3]
        # Path-WF（P1-②）：每折 fold-train 重估池级 μ/σ（v1.3 冻结窗口，σ 窗口逐基金）→ 路径层校准
        path_r = _path_eval_for_fold(f) if 5 in horizons else None
        if path_r is None:
            print("  Path-WF T+5：fold-train 数据不足或真标签缺失 → 跳过")
        else:
            print(f"  Path-WF T+5：μ={path_r['mu']:+.5f} σ={path_r['sigma']:.5f} "
                  f"hit(mdd10/mdd50/mfe50)={path_r['hit_mdd10']:.1%}/"
                  f"{path_r['hit_mdd50']:.1%}/{path_r['hit_mfe50']:.1%} "
                  f"(n={path_r['n_test']})")
            row["path"] = path_r
        fold_rows.append(row)

    # pooled WF（部署式 OOS 主判据）
    pooled = {}
    print("\n== [3] pooled WF OOS（各折预测拼接）==")
    for h in horizons:
        d = wf_all[h]
        if len(d["p"]) < 30:
            continue
        ric = rank_ic(d["p"], d["y"])
        ci = cluster_bootstrap_ci(
            lambda sub: rank_ic(sub["x"].tolist(), sub["y"].tolist()),
            {"x": np.array(d["p"]), "y": np.array(d["y"])}, d["dates"],
            n_boot=args.n_boot)
        pooled[h] = {"ric": ric, "ci": (float(ci[0]), float(ci[1])),
                     "n": len(d["p"]), "ndays": len(set(d["dates"]))}
        print(f"  T+{h} pooled WF RankIC={ric:+.3f} CI=[{ci[0]:+.3f}, {ci[1]:+.3f}]"
              f"（n={len(d['p'])} / {len(set(d['dates']))} 日）")

    # Path-WF pooled（部署式路径校准，观察层）
    pooled_path = _pooled_path([r.get("path") for r in fold_rows if r.get("path")])
    if pooled_path is not None:
        print(f"\n== [3b] Path-WF pooled（{pooled_path['n_folds']} 折 / {pooled_path['n_test']} 样本）==")
        print(f"  hit mdd10={pooled_path['hit_mdd10']:.1%}（期望 ~10%） "
              f"mdd50={pooled_path['hit_mdd50']:.1%}（期望 ~50%） "
              f"mfe50={pooled_path['hit_mfe50']:.1%}（期望 ~50%）")
    else:
        print("\n== [3b] Path-WF pooled：无可用折 → 跳过 ==")

    # P1-③：逐折部署式 CQR（折内 split-conformal 定标 → 折内 test 评估）
    cqr_wf = {}
    if pooled:
        print("\n== [3c] 逐折部署式 CQR（折内 split-conformal，P1-③）==")
        for h in horizons:
            per = []
            for row, f in zip(fold_rows, folds):
                r = _fold_cqr(f, h)
                if r is None:
                    continue
                per.append(r)
                row.setdefault("cqr", {})[h] = {
                    k: r[k] for k in ("qhat", "n_cal", "n_test", "cov", "cov_raw")}
            if not per:
                continue
            y_all = np.concatenate([r["y"] for r in per])
            cov_all = float(np.mean(
                (np.concatenate([r["lo"] for r in per]) <= y_all)
                & (y_all <= np.concatenate([r["hi"] for r in per]))))
            cov_raw_all = float(np.mean(
                (np.concatenate([r["lo_raw"] for r in per]) <= y_all)
                & (y_all <= np.concatenate([r["hi_raw"] for r in per]))))
            qhats = [r["qhat"] for r in per]
            cqr_wf[h] = {"n_folds": len(per), "n": int(len(y_all)),
                         "qhat_mean": float(np.mean(qhats)),
                         "qhat_min": min(qhats), "qhat_max": max(qhats),
                         "cov": cov_all, "cov_raw": cov_raw_all}
            print(f"  T+{h}: pooled coverage = {cov_all:.1%}（raw {cov_raw_all:.1%}，"
                  f"名义 80%）· qhat均值={np.mean(qhats):.4f} "
                  f"范围[{min(qhats):.4f}, {max(qhats):.4f}]（{len(per)} 折可用）")

    # 预注册判定（T+5 主判据）
    verdict_key, verdict_str = "n/a", "数据不足，无法判定"
    p5 = pooled.get(5)
    last5 = None
    for row in fold_rows:
        if 5 in row["by_h"]:
            last5 = row["by_h"][5]
    if p5 is not None and last5 is not None and p5["ci"][0] == p5["ci"][0]:
        ci_lo = p5["ci"][0]
        if ci_lo > 0 and last5["ric"] > 0:
            verdict_key = "wf_stable"
            verdict_str = ("✅ WF 稳定：部署式重训下优势保持，最近窗口仍为正"
                           "——T+5 证据接近「稳定 alpha」，可进入 T+5 专项评分卡")
        elif ci_lo > 0:
            verdict_key = "wf_partial"
            verdict_str = ("⚠️ WF 部分：pooled 仍显著，但最近折 ≤ 0——重训救不回最近窗，"
                           "衰减指向 regime 变化（市场/特征分布），而非模型冻结过期")
        else:
            verdict_key = "wf_fail"
            verdict_str = ("❌ WF 失效：部署式重训下整体优势不显著——冻结模型的 +0.080 "
                           "含「模型过期红利」成分，T+5 不值得继续向 Policy 推进")
    print(f"\n判定：{verdict_key} —— {verdict_str}")

    # ---- 报告 ----
    lines = [
        "# Walk-Forward（expanding 重训）OOS 验证（2026-09-01，GPT 五审 P0 + Path-WF P1-②）", "",
        snap_info["report_line"],
        f"> 生成：{time.strftime('%Y-%m-%d %H:%M')} · WF {len(folds)} 折（{args.window_days} 交易日/折，"
        f"与 Rolling OOS 同窗）· 每折 expanding 重训 + label-end purge（max_horizon={max_h}）"
        f" · cluster bootstrap {args.n_boot} 次", "",
        "**回答的问题**：如果按真实部署不断重训模型，T+5 优势还能不能保持？"
        "（Rolling OOS 只回答「这个冻结模型何时失效」）", "",
        "## 一、主判据：T+5 pooled WF vs 冻结基线", "",
        "| 口径 | RankIC | 95% CI | 样本 |",
        "|---|---:|---:|---:|",
        f"| 冻结模型 pooled OOS（v7 基线） | +0.080 | [+0.017, +0.142] | 905 / 305 日 |",
    ]
    if p5 is not None:
        lines.append(f"| **WF pooled（部署式）** | **{p5['ric']:+.3f}** | "
                     f"[{p5['ci'][0]:+.3f}, {p5['ci'][1]:+.3f}] | "
                     f"{p5['n']} / {p5['ndays']} 日 |")
    lines += ["", f"**预注册判定：{verdict_str}**", ""]

    lines += ["## 二、逐折对比（WF 重训 vs frozen 冻结，同窗）", "",
              "| 折 | 测试窗 | n_train | WF T+5 IC | frozen T+5 IC | Δ |",
              "|---:|---|---:|---:|---:|---:|"]
    for row in fold_rows:
        wf_ic = row["by_h"].get(5, {}).get("ric")
        fz_ic = row["frozen"].get(5)
        cells = [f"| {row['fold']} | {row['window']} | {row['n_train']} |"]
        cells.append(f" {wf_ic:+.3f} |" if wf_ic is not None else " — |")
        cells.append(f" {fz_ic:+.3f} |" if fz_ic is not None else " — |")
        if wf_ic is not None and fz_ic is not None:
            cells.append(f" {wf_ic - fz_ic:+.3f} |")
        else:
            cells.append(" — |")
        lines.append("".join(cells))
    lines.append("")

    lines += ["## 三、各周期 pooled WF RankIC", "",
              "| 周期 | WF pooled IC | 95% CI | 最近折 IC |",
              "|---|---:|---:|---:|"]
    for h in horizons:
        p = pooled.get(h)
        lasth = None
        for row in fold_rows:
            if h in row["by_h"]:
                lasth = row["by_h"][h]["ric"]
        if p is None:
            lines.append(f"| T+{h} | — | — | — |")
        else:
            ci_str = (f"[{p['ci'][0]:+.3f}, {p['ci'][1]:+.3f}]"
                      if p["ci"][0] == p["ci"][0] else "—")
            lines.append(f"| T+{h} | {p['ric']:+.3f} | {ci_str} | "
                         f"{lasth:+.3f} |" if lasth is not None
                         else f"| T+{h} | {p['ric']:+.3f} | {ci_str} | — |")
    lines.append("")

    lines += ["## 四、Path-WF：部署式逐折路径校准（T+5，观察层）", "",
              "每折用 train_k 重估池级 μ/σ（path_forecast v1.3：σ 窗口逐基金最近 20 交易日、"
              "RECENT_WINDOW 自 v1.3 冻结，逐折不允许调窗）→ 无条件 MC → 对 fold-test 真 mdd5/mfe5 命中率。", ""]
    if pooled_path is not None:
        lines += ["| 折 | 测试窗 | n_train | μ | σ | n_test | MC mdd_q10 | 真 mdd P10 | hit mdd10 | hit mdd50 | hit mfe50 |",
                  "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for row in fold_rows:
            pr = row.get("path")
            if pr is None:
                continue
            lines.append(
                f"| {row['fold']} | {row['window']} | {row['n_train']} | {pr['mu']:+.5f} | "
                f"{pr['sigma']:.5f} | {pr['n_test']} | {pr['mdd_q10']:.4f} | {pr['mdd_p10']:.4f} | "
                f"{pr['hit_mdd10']:.1%} | {pr['hit_mdd50']:.1%} | {pr['hit_mfe50']:.1%} |")
        lines += ["",
                  f"**pooled Path-WF（{pooled_path['n_folds']} 折 / {pooled_path['n_test']} 样本）**："
                  f"hit mdd10={pooled_path['hit_mdd10']:.1%}（期望 ~10%） · "
                  f"mdd50={pooled_path['hit_mdd50']:.1%}（期望 ~50%） · "
                  f"mfe50={pooled_path['hit_mfe50']:.1%}（期望 ~50%）；"
                  f"σ 范围 [{pooled_path['sigma_min']:.4f}, {pooled_path['sigma_max']:.4f}]"
                  "（折间波动 = 路径层 regime 信号）", ""]
    else:
        lines += ["_（无可用 Path-WF 折：fold-train 数据不足或真标签缺失）_", ""]

    lines += ["## 五、逐折部署式 CQR（P1-③，观察层）", "",
              "每个 WF fold 内部：fold-train 按日期尾部 20% 切 calib holdout"
              "（split-conformal，fit/cal 时间不相交）→ qhat_k → fold-test 外扩区间。"
              "与 calib 报告的「train 内 CV qhat 平均 → 整段 OOS」相比，这是部署时点"
              "协议（校准随折前滚）；qhat_k 折间变化 = 校准强度的 regime 信号。", ""]
    if cqr_wf:
        lines += ["| 周期 | pooled cov（外扩后） | raw cov | 名义 | qhat 均值 | qhat 范围 | 折数 |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        for h in horizons:
            c = cqr_wf.get(h)
            if not c:
                continue
            lines.append(f"| T+{h} | {c['cov']:.1%} | {c['cov_raw']:.1%} | 80% | "
                         f"{c['qhat_mean']:.4f} | [{c['qhat_min']:.4f}, {c['qhat_max']:.4f}] | "
                         f"{c['n_folds']} |")
        lines.append("")
    else:
        lines += ["_（无可用折内 CQR：fold-train 不足或模型训练失败）_", ""]

    lines += ["<sub>口径：每折 train = 该折测试窗起点前（含 OOS 段已过去数据，部署模拟）"
              "date &lt; cutoff 的全部样本，label-end purge 同 split_date_oos 纪律；"
              "模型超参同 backtest_forecast（HGB max_iter=200/lr=0.08/depth=3）。"
              "WF 与冻结 OOS 是不同问题协议：冻结 OOS 仍是「单一模型 OOS」的正式口径，"
              "WF 回答「部署式重训能否保持优势」，两者并列不互相替代。"
              "Path-WF（P1-②）：每折 fold-train 重估池级 μ/σ（v1.3 冻结窗口，σ 窗口逐基金）做路径层校准，"
              "观察层证据，不改变 T+5 verdict。"
              "逐折 CQR（P1-③）：折内 split-conformal（calib holdout 占 fold-train "
              "尾部 20%），部署时点协议。</sub>", ""]
    report = "\n".join(lines) + "\n"

    stamp = time.strftime("%Y%m%d")
    out_md = BASE_DIR / "output" / f"backtest_walk_forward_{stamp}.md"
    out_md.write_text(report, encoding="utf-8")
    log = BASE_DIR / "output" / f"backtest_walk_forward_{stamp}.log"
    log.write_text(report, encoding="utf-8")
    print(f"\n[ok] 报告 → {out_md}")
    print(f"[done] 耗时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
