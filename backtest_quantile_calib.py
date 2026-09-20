#!/usr/bin/env python3
"""分位数校准验证（2026-09-01，GPT 五审 P1-C 落地）。

回答（GPT 五审 §十一/§十二）：v8 起模型输出真 Q10/Q50/Q90（quantile loss HGBR），
但全项目此前 **0 处 coverage/pinball 统计**——分位数回归没做校准检验等于只验了
中位数方向、没验区间可信度。

口径（与 backtest_forecast / train_forecast_model 完全同协议）：
- 训练：split_date_oos 冻结切分（train < oos_start，label-end purge），B1 双列特征
- 模型：ReturnQuantileModel（同 forecast_engine，每 horizon 独立 fit）
- OOS：全段 907 样本，逐样本预测 q10/q50/q90

指标（预注册，写死在代码里）：
1. Coverage80 = P(q10 <= y <= q90)，名义 80%；判据：0.70~0.90 为过，
   <0.70 区间过窄（风险被低估），>0.90 过宽（信息量低）。cluster bootstrap 95% CI。
2. Pinball loss（q10/q50/q90 各自）vs 无条件基线（train 经验分位数常数预测）：
   model < baseline = 有技能（区间随特征状态变化带来增益）。
3. Q50 RankIC（vs fwd_h）+ cluster CI：中位数也该有排序能力（与 e_return 同向）。
4. 区间宽度 mean(q90-q10)：sharpness 参考（校准前提下越窄信息越多）。
5. h=5 附加：路径层无条件粗校准——train 全局 μ/σ 单次 MC（n=2000）的
   mdd_q10/mdd_q50/mfe_q50 vs OOS 真 mdd5/mfe5 命中率（期望 10%/50%/50%）。
   注意：这是分布级粗校准（路径层为观察层、非逐日条件化），逐日 MC 校准留待实验。
6. cross-validated conformal-style 再校准（2026-09-01 backlog；CV 平均 qhat 为工程近似，无严格 split-conformal 覆盖保证）：train 内按日期块
   5 折 expanding CV 定标 nonconformity 分位 qhat（fold 间 embargo=h 防
   label 泄漏），OOS 区间两侧外扩 qhat 后复测 coverage——OOS 只评估不参与定标。

输出：output/backtest_quantile_calib_YYYYMMDD.md + 同 .log
用法：python3 backtest_quantile_calib.py [--n-boot 199]
"""
import json
import math
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import numpy as np

from backtest_forecast import (split_date_oos, rank_ic, cluster_bootstrap_ci, date_group_cv_masks)
from backtest_spread import load_samples
from frozen_dataset import resolve_samples   # V4.3 P0-1：统一冻结样本入口
from core import forecast_engine
from core.forecast_engine import FEATURE_KEYS, ReturnQuantileModel

COV_LO, COV_HI = 0.70, 0.90        # coverage 80% 的可接受带宽（预注册 ±10pp）
NOMINAL = 0.80
CQR_ALPHA = 0.10                   # conformal 目标失覆盖率（80% 区间，预注册）
CQR_N_FOLDS = 5                    # train 内日期块折数（expanding）


def feat_row(s: dict) -> list[float]:
    """样本 → 14 维 B1 双列特征（值+missing_mask），与 build_xy 同口径。"""
    row = []
    for k in FEATURE_KEYS:
        v = s.get(k)
        row.append(0.0 if v is None else float(v))
        row.append(1.0 if v is None else 0.0)
    return row


def pinball(y: float, yhat: float, q: float) -> float:
    """Pinball（分位数）损失：q*(y-yhat) if y>=yhat else (1-q)*(yhat-y)。"""
    return q * (y - yhat) if y >= yhat else (1.0 - q) * (yhat - y)


def mean_pinball(y_true: np.ndarray, yhat: np.ndarray, q: float) -> float:
    return float(np.mean([pinball(float(y), float(p), q)
                          for y, p in zip(y_true, yhat)]))


def coverage_mask(q10: np.ndarray, q90: np.ndarray, y: np.ndarray) -> np.ndarray:
    return ((q10 <= y) & (y <= q90)).astype(float)


# ---------- CQR 再校准（2026-09-01，Romano et al. 2019 Conformalized QR）----------
def cqr_scores(y, lo, hi) -> np.ndarray:
    """CQR nonconformity：区间内取浅侧深度（负值），区间外取到边距离（正值）。

    s = max(lo - y, y - hi)；退化区间（lo > hi 交叉）记 inf（不可用作定标）。
    """
    y = np.asarray(y, dtype=float)
    lo = np.asarray(lo, dtype=float)
    hi = np.asarray(hi, dtype=float)
    out = np.maximum(lo - y, y - hi)
    return np.where((hi >= lo) & np.isfinite(out), out, np.inf)


def cqr_qhat(scores, alpha: float = CQR_ALPHA) -> float:
    """finite-sample 分位：第 ceil((n+1)(1-alpha))/n 阶统计量（CQR 论文 eq.2）。

    分子超过 n（定标集太小）→ inf（诚实不可用）；非有限分数过滤后计数。
    """
    s = np.sort(np.asarray([v for v in scores if np.isfinite(v)], dtype=float))
    n = len(s)
    if n == 0:
        return float("inf")
    k = math.ceil((n + 1) * (1.0 - alpha))
    if k > n:
        return float("inf")
    return float(s[k - 1])


def expand_interval(lo, hi, qhat: float):
    """区间两侧对称外扩 qhat：lo-qhat / hi+qhat。"""
    return np.asarray(lo, dtype=float) - qhat, np.asarray(hi, dtype=float) + qhat


def cov_metric_factory(q10: np.ndarray, q90: np.ndarray):
    def metric(sub):
        y = sub["y"]
        return float(np.mean((sub["q10"] <= y) & (y <= sub["q90"])))
    return metric


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
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
    t0 = time.time()

    print("== [0] 加载样本 ==")
    samples, snap_info = resolve_samples(args.snapshot, args.fresh, BASE_DIR, load_samples)
    if snap_info["mode"] in ("MISSING", "INVALID"):
        return 4
    samples_sorted = sorted(samples, key=lambda s: (s["date"], s["fund"]))
    print(f"  总样本 {len(samples_sorted)}")

    train, oos, oos_start = split_date_oos(samples_sorted)
    print(f"== [1] 冻结切分：train < {oos_start}（{len(train)}），OOS ≥ {oos_start}（{len(oos)}）==")
    if not oos or len(train) < 200:
        print("[fail] 样本不足")
        return 1

    train_dates = [s["date"] for s in train]

    # OOS 预测特征一次备好
    X_oos = np.array([feat_row(s) for s in oos], dtype=float)
    oos_dates = [s["date"] for s in oos]
    has_mdd = all(s.get("mdd5") is not None for s in oos)

    results = {}
    for h in horizons:
        y = np.array([float(s[f"fwd{h}"]) for s in oos if s.get(f"fwd{h}") is not None])
        mask = np.array([s.get(f"fwd{h}") is not None for s in oos])
        Xh = X_oos[mask]
        yh = y
        dates_h = [d for d, m in zip(oos_dates, mask) if m]
        if len(yh) < 100:
            print(f"[warn] T+{h} OOS 样本不足（{len(yh)}），跳过")
            continue

        model = ReturnQuantileModel(h)
        if not model.fit([s for s in train if s.get(f"fwd{h}") is not None]):
            print(f"[warn] T+{h} 模型训练失败，跳过")
            continue
        preds = [model.predict_dist(list(r)) for r in Xh]
        q10 = np.array([p["q10"] for p in preds])
        q50 = np.array([p["q50"] for p in preds])
        q90 = np.array([p["q90"] for p in preds])

        cov = float(np.mean(coverage_mask(q10, q90, yh)))
        ci = cluster_bootstrap_ci(cov_metric_factory(q10, q90),
                                  {"q10": q10, "q90": q90, "y": yh},
                                  dates_h, n_boot=args.n_boot)
        width = float(np.mean(q90 - q10))

        # pinball：模型 vs 无条件基线（train 经验分位数常数）
        y_train = np.array([float(s[f"fwd{h}"]) for s in train
                            if s.get(f"fwd{h}") is not None])
        base_q = {q: float(np.percentile(y_train, q * 100)) for q in (0.10, 0.50, 0.90)}
        pb_model = {q: mean_pinball(yh, {0.10: q10, 0.50: q50, 0.90: q90}[q], q)
                    for q in (0.10, 0.50, 0.90)}
        pb_base = {q: mean_pinball(yh, np.full_like(yh, base_q[q]), q)
                   for q in (0.10, 0.50, 0.90)}
        skill = {q: pb_base[q] - pb_model[q] for q in (0.10, 0.50, 0.90)}

        ric = rank_ic(list(q50), list(yh))
        ric_ci = cluster_bootstrap_ci(
            lambda sub: rank_ic(list(sub["q50"]), list(sub["y"])),
            {"q50": q50, "y": yh}, dates_h, n_boot=args.n_boot)

        cov_ok = COV_LO <= cov <= COV_HI
        skill_ok = all(v >= 0 for v in skill.values())
        verdict = "✅ 校准可用" if (cov_ok and skill_ok) else (
            "⚠️ 区间过窄" if cov < COV_LO else
            "⚠️ 区间过宽" if cov > COV_HI else "⚠️ pinball 无技能")
        results[h] = dict(n=len(yh), cov=cov, cov_ci=ci, width=width,
                          pb_model=pb_model, pb_base=pb_base, skill=skill,
                          ric=ric, ric_ci=ric_ci, base_q=base_q,
                          cov_ok=cov_ok, skill_ok=skill_ok, verdict=verdict)

        print(f"\n=== T+{h}（OOS n={len(yh)}）===")
        print(f"  Coverage80 = {cov:.1%}  CI[{ci[0]:.1%}, {ci[1]:.1%}]  "
              f"（名义 {NOMINAL:.0%}，带宽 {COV_LO:.0%}~{COV_HI:.0%}）→ {'过' if cov_ok else '未过'}")
        print(f"  区间宽度 mean(q90-q10) = {width:.4f}")
        for q in (0.10, 0.50, 0.90):
            print(f"  Pinball q{int(q*100)}: 模型 {pb_model[q]:.5f} vs 基线 {pb_base[q]:.5f}"
                  f"（train 经验分位 {base_q[q]:+.4f}）→ 技能 {skill[q]:+.5f}")
        print(f"  Q50 RankIC = {ric:+.3f}  CI[{ric_ci[0]:+.3f}, {ric_ci[1]:+.3f}]")
        print(f"  → {verdict}")

        # ---- CQR 再校准（train 内 CV 定标，OOS 只评估不参与定标）----
        fold_qhats = []
        for cv in date_group_cv_masks(train_dates, n_splits=CQR_N_FOLDS, embargo=h):
            tr_s = [s for s, m in zip(train, cv["tr_mask"])
                    if m and s.get(f"fwd{h}") is not None]
            va_s = [s for s, m in zip(train, cv["va_mask"])
                    if m and s.get(f"fwd{h}") is not None]
            if len(tr_s) < 100 or not va_s:
                continue
            mf = ReturnQuantileModel(h)
            if not mf.fit(tr_s):
                continue
            vp = [mf.predict_dist(feat_row(s)) for s in va_s]
            sc = cqr_scores([float(s[f"fwd{h}"]) for s in va_s],
                            [p["q10"] for p in vp], [p["q90"] for p in vp])
            q = cqr_qhat(sc, CQR_ALPHA)
            if math.isfinite(q):
                fold_qhats.append(q)
        cqr = {"available": False}
        if fold_qhats:
            qhat = float(np.mean(fold_qhats))
            lo_c, hi_c = expand_interval(q10, q90, qhat)
            cov_c = float(np.mean(coverage_mask(lo_c, hi_c, yh)))
            cqr = {"available": True, "qhat": qhat, "n_folds": len(fold_qhats),
                   "cov": cov_c,
                   "improved": abs(cov_c - NOMINAL) < abs(cov - NOMINAL),
                   "in_band": COV_LO <= cov_c <= COV_HI}
            print(f"  CQR 再校准：qhat={qhat:.4f}（{len(fold_qhats)} 折可用）"
                  f" OOS Coverage = {cov_c:.1%}（原始 {cov:.1%}）→ "
                  f"{'更接近名义' if cqr['improved'] else '未更接近名义'}"
                  f"{'，入带宽' if cqr['in_band'] else '，仍在带宽外'}")
        else:
            print("  CQR 再校准：折定标全部不可用（样本/模型不足），跳过")
        results[h]["cqr"] = cqr

    # h=5 附加：路径层无条件粗校准
    path_block = ["", "## 路径层无条件粗校准（仅 T+5，观察层）", ""]
    if has_mdd and 5 in horizons:
        from core.path_forecast import _mc_forecast, fit_path_params
        params = fit_path_params(train, 5)
        if params is None:
            path_block.append("- train 日收益不足，跳过")
        else:
            mu, sigma = params
            pf = _mc_forecast(5, mu, sigma, 2000, 42,
                              {"bucket": "global", "n_state": None, "fallback": False})
            mdd_true = np.array([float(s["mdd5"]) for s in oos])
            mfe_true = np.array([float(s["mfe5"]) for s in oos])
            hit_mdd10 = float(np.mean(mdd_true <= pf.mdd_q10))
            hit_mdd50 = float(np.mean(mdd_true <= pf.mdd_q50))
            hit_mfe50 = float(np.mean(mfe_true <= pf.mfe_q50))
            path_block += [
                f"- MC(train μ={mu:.5f}, σ={sigma:.5f}, n=2000)："
                f"mdd_q10={pf.mdd_q10:.4f} mdd_q50={pf.mdd_q50:.4f} mfe_q50={pf.mfe_q50:.4f}",
                f"- 真 mdd5 ≤ mdd_q10 命中率 = {hit_mdd10:.1%}（期望 ~10%）",
                f"- 真 mdd5 ≤ mdd_q50 命中率 = {hit_mdd50:.1%}（期望 ~50%）",
                f"- 真 mfe5 ≤ mfe_q50 命中率 = {hit_mfe50:.1%}（期望 ~50%）",
                "- 注：分布级粗校准（单一无条件预测 vs 全体真值），逐日条件化校准留待实验",
                ""]
            print("\n== 路径层无条件粗校准（T+5）==")
            for ln in path_block[3:6]:
                print("  " + ln.strip("- "))
    else:
        path_block.append("- OOS 缺 mdd5/mfe5 真标签，跳过")

    ok_all = all(r["cov_ok"] and r["skill_ok"] for r in results.values())
    print("\n========================================")
    print("分位数校准验证 · 汇总")
    print("========================================")
    for h, r in results.items():
        print(f"  T+{h}: {r['verdict']}  cov={r['cov']:.1%} "
              f"skill(Σq)={sum(r['skill'].values()):+.5f} q50IC={r['ric']:+.3f}")
    for h, r in results.items():
        c = r.get("cqr", {})
        if c.get("available"):
            print(f"  T+{h} CQR: cov {r['cov']:.1%} → {c['cov']:.1%} "
                  f"(qhat={c['qhat']:.4f}, {c['n_folds']} 折)")
    print(f"\n  总判定：{'✅ 分位数输出可校准使用（观察/证据层）' if ok_all else '❌ 存在 horizon 校准未过——区间预测暂只能当参考'}")
    print("  （本报告为证据层诊断，不绑定 registry/promotion，不改 model_ready）")

    # 留档 md
    lines = ["# 分位数校准验证（2026-09-01，GPT 五审 P1-C）", "",
             snap_info["report_line"],
             f"> 生成：{time.strftime('%Y-%m-%d %H:%M')} · train < {oos_start}（{len(train)}）"
             f" · OOS ≥ {oos_start} · bootstrap {args.n_boot} 次 · 预注册带宽 {COV_LO:.0%}~{COV_HI:.0%}", "",
             "| horizon | n | Coverage80 | 95% CI | 宽度 | Pinball 技能(Σ) | Q50 RankIC | 判定 |",
             "|---|---:|---:|---|---:|---:|---|---|"]
    for h, r in results.items():
        lines.append(
            f"| T+{h} | {r['n']} | {r['cov']:.1%} | [{r['cov_ci'][0]:.1%}, {r['cov_ci'][1]:.1%}]"
            f" | {r['width']:.4f} | {sum(r['skill'].values()):+.5f}"
            f" | {r['ric']:+.3f} [{r['ric_ci'][0]:+.3f},{r['ric_ci'][1]:+.3f}] | {r['verdict']} |")
    cqr_lines = ["", "## cross-validated conformal-style 再校准（train 内 CV 定标 → OOS 对照，诊断）", "",
                 "| horizon | qhat | 可用折 | OOS Coverage(原始→CQR) | 判定 |",
                 "|---|---:|---:|---|---|"]
    for h, r in results.items():
        c = r.get("cqr", {})
        if c.get("available"):
            cqr_lines.append(
                f"| T+{h} | {c['qhat']:.4f} | {c['n_folds']} | "
                f"{r['cov']:.1%} → {c['cov']:.1%} | "
                f"{'更接近名义80%' if c['improved'] else '未更接近名义'}"
                f"{'，入带宽' if c['in_band'] else '，带宽外'} |")
        else:
            cqr_lines.append(f"| T+{h} | — | 0 | {r['cov']:.1%} → — | 定标不可用 |")
    cqr_lines += [
        f"- 方法：Romano et al. 2019 CQR；nonconformity=区间外深度，finite-sample 分位"
        f" ceil((n+1)(1-{CQR_ALPHA}))/n，fold 间 embargo=h 防 label 泄漏，qhat 取可用折均值（CV 平均为工程近似，不宣称严格 finite-sample 覆盖保证）",
        "- OOS 只评估不参与定标；qhat>0 = 区间需外扩（模型区间过窄的 conformal 修正量）；区间为常数外扩，不随状态变化",
        ""]
    lines += path_block + cqr_lines + [
        "", "## 结论",
        f"- 总判定：{'✅ 全部 horizon 校准可用' if ok_all else '❌ 存在未过 horizon'}",
        "- Coverage 带宽预注册 ±10pp（0.70~0.90）；<0.70=过窄（风险低估），>0.90=过宽",
        "- Pinball 技能 = 无条件基线损失 − 模型损失（>0 = 特征条件化带来区间增益）",
        "- cross-validated conformal-style：train 内 CV 定标 + OOS 复测（qhat 外扩，80% 目标）；诊断用，不宣称严格覆盖保证",
        "- 本报告为证据层诊断：不绑定 registry/promotion，model_ready 不受影响", ""]
    out = BASE_DIR / "output" / f"backtest_quantile_calib_{time.strftime('%Y%m%d')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[ok] 报告 → {out}")
    print(f"[done] 耗时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
