#!/usr/bin/env python3
"""v5 多周期预测验证器：TimeSeriesSplit + Brier/Calibration/RankIC + OOS 裁决。

回答 GPT-5.6Luna 评审的核心问题：
    「这个特征有没有提高 T+1/T+3/T+5 的样本外预测质量？」
    「概率预测靠不靠谱（不只是方向命中率）？」

纪律（对齐项目铁律「有证据才上线」，2026-08-26 定）：
1. 样本 = backtest_spread.load_samples()（防前视：t 日持仓 = 披露生效日 ≤ t 最近季报快照）
2. 防前视训练：TimeSeriesSplit（按时间序切分，用过去预测未来，不随机洗牌）
3. 最终段（OOS，取时间上最后 20%，2025 附近起）单独留证，不参与调参
4. 指标（GPT 第二十节）：
   - Brier Score      概率校准（三分类多类 Brier：越低越好，0.67=瞎猜）
   - Calibration      预测 P(up)=x 的桶，实际方向率是否 ≈ x（70% 桶应 ≈70% 上涨）
   - Rank IC          rank(feature 或预测分) 与未来收益的秩相关（排序能力）
   - Hit Rate         分桶标签方向命中
5. 裁决：OOS 上 Rank IC > 0 且 Calibration 斜率接近 1（或 MAE 不差于基线）
   → 模型预测有样本外价值，可把 config.forecast.model_ready 置 true（人工复核）
   否则 → 维持占位预测，本报告留档为证据（与 v4 history_validated 同哲学）。

用法：python3 backtest_forecast.py
"""
import json
import math
import random
import sys
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import numpy as np

from backtest_spread import load_samples, FWD_LIST, EXTRA_FWD
from core import forecast_engine

# 确定性种子
RNG = random.Random(42)


# ---------- 指标 ----------
def brier_multiclass(y_true: np.ndarray, proba: np.ndarray) -> float:
    """多类 Brier Score（越低越好，0=完美，0.667=三分类瞎猜）。"""
    n, k = proba.shape
    if n == 0:
        return 0.0
    onehot = np.zeros((n, k))
    onehot[np.arange(n), y_true] = 1.0
    return float(np.mean(np.sum((proba - onehot) ** 2, axis=1)))


def calibration_curve(y_bool: np.ndarray, p: np.ndarray, nbins: int = 5) -> dict:
    """二值校准曲线：每桶预测概率 vs 实际频率。返回平均绝对校准误差(ACE)。"""
    if len(p) == 0:
        return {"bins": [], "ace": 1.0}
    edges = np.linspace(0, 1, nbins + 1)
    ace = 0.0
    bins = []
    for i in range(nbins):
        lo, hi = edges[i], edges[i + 1]
        if i == nbins - 1:
            mask = p >= lo
        else:
            mask = (p >= lo) & (p < hi)
        if mask.sum() == 0:
            continue
        freq = float(y_bool[mask].mean())
        mid = (lo + hi) / 2
        ace += abs(mid - freq) * mask.sum()
        bins.append({"bin": f"{lo:.2f}~{hi:.2f}", "n": int(mask.sum()),
                     "avg_p": float(p[mask].mean()), "freq": freq})
    return {"bins": bins, "ace": ace / len(p)}


def rank_ic(xs: list[float], ys: list[float]) -> float:
    """Spearman 秩相关（Rank IC）。"""
    from scipy.stats import spearmanr
    if len(xs) < 10:
        return 0.0
    r, _ = spearmanr(xs, ys)
    return float(r) if r == r else 0.0


def split_date_oos(samples: list[dict], ratio: float = 0.8,
                   max_horizon: int | None = None) -> tuple[list, list, str]:
    """按交易日 80% 分位切分 train/OOS + label-end purge（冻结纪律 2026-08-28，GPT P0①/P0②）。

    返回 (train, oos, oos_start)：train 只含 oos_start 之前的日期，OOS 段永不参与训练。
    train_forecast_model.py 与 backtest 同口径，单一事实来源。

    P0-2（2026-08-29，GPT 三审）：train 尾部额外剔除 oos_start 前 max_horizon 个交易日——
    这些样本的 fwd<=max_horizon 标签窗口伸进 OOS 段（label_end >= oos_start），按
    「train 样本 label_end < oos_start」纪律不得入训。purge 数量随池子加长自动增减。
    max_horizon 默认取 config.forecast.horizons 的最大值（配置唯一事实来源，
    2026-08-31 改：不再硬编码 5，horizons 扩到 [1,3,5,10,20] 时无需再手动同步）。
    """
    dates = sorted({s["date"] for s in samples})
    if not dates:
        return [], [], None                  # 空样本优雅降级（2026-08-28 加固）
    split_i = min(max(int(len(dates) * ratio), 1), len(dates) - 1)   # 防越界
    oos_start = dates[split_i]
    if max_horizon is None:
        import json as _json
        _fc = _json.loads((Path(__file__).resolve().parent / "config.json")
                          .read_text(encoding="utf-8")).get("forecast", {})
        max_horizon = max(_fc.get("horizons", [1, 3, 5]))
    # label-end purge（P0-2）：train 尾部 max_horizon 个交易日剔除（标签窗口与 OOS 重叠）
    cutoff = dates[max(0, split_i - max_horizon)]
    train = [s for s in samples if s["date"] < cutoff]
    oos = [s for s in samples if s["date"] >= oos_start]
    return train, oos, oos_start


def date_group_cv_masks(dates: list[str], n_splits: int = 5, embargo: int = 1) -> list[dict]:
    """按交易日分块的滚动前视 CV 掩码（2026-08-28 修，GPT P1③）。

    规则：一个交易日只属于一个 fold；每 fold 的训练集 = 该块**之前**的日期，
    且剔除与本块紧邻的 embargo 个交易日（T+embargo 标签与验证段重叠 → purge）。
    返回 [{fold, tr_mask, va_mask, n_train, n_va}]，掩码与输入 dates 等长。
    首个块（无历史）训练掩码为空 → 调用方应跳过。
    """
    uniq = sorted(set(dates))
    idx = {d: i for i, d in enumerate(uniq)}
    row = np.array([idx[d] for d in dates])
    blocks = np.array_split(np.arange(len(uniq)), n_splits)
    out = []
    for b in blocks:
        if len(b) == 0:
            continue
        start_i = int(b[0])
        va_dates = {uniq[j] for j in b}
        emb_dates = {uniq[j] for j in range(max(0, start_i - embargo), start_i)}
        tr_mask = np.array([i < start_i and uniq[i] not in emb_dates for i in row])
        va_mask = np.array([d in va_dates for d in dates])
        out.append({"fold": start_i, "tr_mask": tr_mask, "va_mask": va_mask,
                    "n_train": int(tr_mask.sum()), "n_va": int(va_mask.sum())})
    return out


# ---------- 主流程 ----------
def cluster_bootstrap_ci(metric, arrays: dict[str, np.ndarray], dates: list[str],
                         n_boot: int = 999, seed: int = 42) -> tuple[float, float]:
    """Cluster bootstrap（按交易日块重抽样）指标 95% CI（2026-08-29，GPT 三审 P1）。

    同日的多基金样本是同一横截面，独立抽样会高估有效样本量 → 按日整块
    重抽样（有放回抽 len(uniq) 个日，拼回样本）才是正确口径。
    metric(sub_arrays_dict) -> float；返回 (lo, hi)，不可用时 (nan, nan)。
    """
    uniq = sorted(set(dates))
    if len(uniq) < 10:
        return float("nan"), float("nan")
    groups = {d: np.array([i for i, dd in enumerate(dates) if dd == d], dtype=int)
              for d in uniq}
    rng = np.random.default_rng(seed)
    stats = []
    for _ in range(n_boot):
        chosen = rng.choice(uniq, size=len(uniq), replace=True)
        idx = np.concatenate([groups[d] for d in chosen])
        sub = {k: v[idx] for k, v in arrays.items()}
        try:
            val = float(metric(sub))
        except Exception:
            continue
        if val == val:                       # 非 NaN
            stats.append(val)
    if len(stats) < 50:
        return float("nan"), float("nan")
    return float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5))


@dataclass
class XYBatch:
    """同一筛选循环生成的矩阵、标签与行身份，禁止下游自行重建对齐。"""
    X: np.ndarray
    y: np.ndarray
    yret: np.ndarray
    dates: list[str]
    funds: list[str]

    def __iter__(self):
        # 向后兼容 ``X, y, yret = build_xy(...)``。
        return iter((self.X, self.y, self.yret))

    def __getitem__(self, index):
        return (self.X, self.y, self.yret)[index]


def build_xy(samples: list[dict], horizon: int, flat_margin: float):
    """样本 → 特征、标签及严格同序的 date/fund 行身份。"""
    X, y, yret, dates, funds = [], [], [], [], []
    for s in samples:
        fwd = s.get(f"fwd{horizon}")
        if fwd is None:
            continue
        # B1（2026-08-31）：缺失不整行丢弃——值填 0 + missing_mask 双列（与引擎同口径）
        row = []
        for k in forecast_engine.FEATURE_KEYS:
            v = s.get(k)
            row.append(0.0 if v is None else float(v))
            row.append(1.0 if v is None else 0.0)
        X.append(row)
        yret.append(float(fwd))
        dates.append(s["date"])
        funds.append(s.get("fund", ""))
        if fwd > flat_margin:
            y.append(2)
        elif fwd < -flat_margin:
            y.append(0)
        else:
            y.append(1)
    if not X:
        return None
    return XYBatch(np.array(X, dtype=float), np.array(y, dtype=int),
                   np.array(yret, dtype=float), dates, funds)


def main() -> int:
    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    fc = cfg.get("forecast", {})
    horizons = fc.get("horizons", [1, 3, 5])
    flat_margin = fc.get("prob_flat_margin", 0.003)

    print("== [0] 加载样本 ==")
    samples = load_samples()
    print(f"  总样本 {len(samples)}")
    # 样本需按时间排序（load_samples 已是按日期循环构建，这里再显式排序）
    samples_sorted = sorted(samples, key=lambda s: (s["date"], s["fund"]))

    # 按基金样本量（诊断）
    from collections import defaultdict
    by_fund = defaultdict(int)
    for s in samples_sorted:
        by_fund[s["fund"]] += 1
    print("  按基金样本量:", dict(by_fund))

    print(f"\n== [1] 各周期标签可用样本（全池）==")
    for h in sorted(set(list(horizons) + list(FWD_LIST) + list(EXTRA_FWD))):
        n = sum(1 for s in samples_sorted if s.get(f"fwd{h}") is not None)
        print(f"  fwd{h}: {n}")

    # OOS 切分：时间上最后 20% 留作最终验证（冻结：OOS 段永不参与训练，train_forecast_model 同口径）
    train_all, oos, oos_start = split_date_oos(samples_sorted)
    if not train_all and not oos:
        print("[fail] 无样本（网络/持仓拉取故障？）——总样本 0，无法验证")
        return 1
    print(f"\n== [2] 时间切分：train < {oos_start}（{len(train_all)}），OOS ≥ {oos_start}（{len(oos)}）==")

    from sklearn.ensemble import HistGradientBoostingClassifier

    results = {}
    overall_ok = True
    for h in horizons:
        print(f"\n=== Horizon T+{h} ===")
        XY = build_xy(train_all, h, flat_margin)
        XYo = build_xy(oos, h, flat_margin)
        if XY is None or len(XY[1]) < 100 or XYo is None or len(XYo[1]) < 30:
            print(f"  ⚠️ 样本不足（train={len(XY[1]) if XY else 0}, oos={len(XYo[1]) if XYo else 0}）→ 跳过")
            results[h] = {"ok": False, "reason": "insufficient_samples"}
            overall_ok = False
            continue

        X, y, yret = XY
        Xo, yo, yreto = XYo

        # 类别平衡检查
        counts = np.bincount(y, minlength=3)
        print(f"  类别分布 up/flat/down = {counts.tolist()}")

        # 按日分组 + purged/embargoed CV（2026-08-28 修，GPT P1③）：
        # 一个交易日只属于一个 fold；train 只含验证块**之前**的日期，且剔除紧邻
        # embargo=h 个交易日的样本（T+h 标签与验证段重叠 → purge），杜绝同日跨折。
        clf = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.08,
                                             max_depth=3, early_stopping=True, random_state=42)
        # 日期身份由 build_xy 与 X/y 在同一循环生成，严禁下游重建筛选口径。
        cv_results = []
        for fm in date_group_cv_masks(XY.dates, n_splits=5, embargo=h):
            if fm["n_train"] < 50 or fm["n_va"] == 0:
                print(f"  fold@{fm['fold']}: 样本不足(tr={fm['n_train']},va={fm['n_va']}) → 跳过")
                continue
            clf_cv = HistGradientBoostingClassifier(max_iter=100, learning_rate=0.08,
                                                    max_depth=3, early_stopping=True, random_state=42)
            clf_cv.fit(X[fm["tr_mask"]], y[fm["tr_mask"]])
            pv = clf_cv.predict_proba(X[fm["va_mask"]])
            cv_results.append(brier_multiclass(y[fm["va_mask"]], pv))
        cv_brier = float(np.mean(cv_results)) if cv_results else 0.0
        print(f"  CV Brier(均) = {cv_brier:.3f}（瞎猜基准 0.667；越低越好，可用 fold={len(cv_results)}）")

        # 在全部 train 上拟合 OOS 用
        clf.fit(X, y)
        po = clf.predict_proba(Xo)
        oo_brier = brier_multiclass(yo, po)
        print(f"  OOS Brier = {oo_brier:.3f}")

        # Calibration（对 up 二值）
        p_up = po[:, 2]
        y_up = (yo == 2).astype(int)
        calib = calibration_curve(y_up, p_up)
        print(f"  OOS 校准(up) ACE = {calib['ace']:.3f}")
        for b in calib["bins"]:
            print(f"    {b['bin']} n={b['n']} 预测均={b['avg_p']:.2f} 实际={b['freq']:.2f}")

        # Rank IC：模型对 up 的倾向分 vs 未来收益
        score_up = po[:, 2]
        ric = rank_ic(score_up.tolist(), yreto.tolist())
        print(f"  OOS Rank IC(up倾向 vs fwd{h}) = {ric:+.3f}")

        # v7 P1：cluster bootstrap 95% CI（按日块重抽样，同日样本不独立）
        aligned_oos_dates = XYo.dates
        ric_ci = cluster_bootstrap_ci(
            lambda sub: rank_ic(sub["x"].tolist(), sub["y"].tolist()),
            {"x": score_up, "y": yreto}, aligned_oos_dates)
        brier_ci = cluster_bootstrap_ci(
            lambda sub: brier_multiclass(sub["y"], sub["p"]),
            {"y": yo, "p": po}, aligned_oos_dates)
        print(f"  OOS Rank IC 95% CI (cluster bootstrap, n={len(aligned_oos_dates)}样本/{len(set(aligned_oos_dates))}日) = [{ric_ci[0]:+.3f}, {ric_ci[1]:+.3f}]")
        print(f"  OOS Brier 95% CI = [{brier_ci[0]:.3f}, {brier_ci[1]:.3f}]")

        # 基线梯队（v7 P1：瞎猜 → 多数类 → est_chg 单因子 → ML，逐级不劣于才过裁决）
        # ① 瞎猜：三分类 Brier=2/3，Rank IC=0（无排序信息）
        b_guess = 2.0 / 3.0
        ic_guess = 0.0
        # ② 多数类：用 TRAIN 段类别分布（防泄漏）作常数概率；Brier 为常数，Rank IC=0
        p_train = np.bincount(y, minlength=3) / max(1, len(y))
        onehot_oos = np.zeros((len(yo), 3))
        onehot_oos[np.arange(len(yo)), yo] = 1.0
        b_majority = float(np.mean(np.sum((onehot_oos - p_train) ** 2, axis=1)))
        ic_majority = 0.0
        # ③ est_chg 单因子
        base_vec = [Xo[i][forecast_engine.FEATURE_KEYS.index("est_chg")] for i in range(Xo.shape[0])]
        base_ic = rank_ic(base_vec, yreto.tolist())
        print(f"  基线梯队（OOS）：瞎猜 Brier={b_guess:.3f} IC=0.000 │ "
              f"多数类 Brier={b_majority:.3f} IC=0.000 │ est_chg IC={base_ic:+.3f}")

        # 裁决（v7 P1：
        #   - Rank IC 95% CI 下界 > 0（cluster bootstrap）才算「显著不为零」
        #   - ML Brier 逐级不劣于梯队（Brier 越低越好；IC 高于 est_chg 单因子））
        ric_significant = not math.isnan(ric_ci[0]) and ric_ci[0] > 0
        beats_hierarchy = (oo_brier <= max(b_majority, b_guess) and ric > base_ic)
        ok = ric_significant and oo_brier < 0.62 and calib["ace"] < 0.25 and beats_hierarchy
        results[h] = {"ok": ok, "cv_brier": round(cv_brier, 3), "oos_brier": round(oo_brier, 3),
                      "rank_ic": round(ric, 3), "ric_ci": [round(ric_ci[0], 3), round(ric_ci[1], 3)]
                      if not math.isnan(ric_ci[0]) else None,
                      "brier_ci": [round(brier_ci[0], 3), round(brier_ci[1], 3)]
                      if not math.isnan(brier_ci[0]) else None,
                      "base_ic": round(base_ic, 3),
                      "b_majority": round(b_majority, 3),
                      "ace": round(calib["ace"], 3), "n_train": int(len(y)), "n_oos": int(len(yo))}
        overall_ok = overall_ok and ok
        print(f"  → 裁决：{'✅ 通过' if ok else '❌ 不通过'}（需全部周期通过）")

    # 特征重要性（排列重要性，HistGradientBoosting 无原生 feature_importances_）
    try:
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.inspection import permutation_importance
        Xall, yall = None, None
        for h in horizons:
            XY = build_xy(samples_sorted, h, flat_margin)
            if XY is None:
                continue
            Xh, yh, _ = XY
            Xall = Xh if Xall is None else np.vstack([Xall, Xh])
            yall = np.concatenate([yall, yh]) if yall is not None else yh
        clf_all = HistGradientBoostingClassifier(max_iter=200, random_state=42)
        clf_all.fit(Xall, yall)
        pi = permutation_importance(clf_all, Xall, yall, n_repeats=10,
                                    random_state=42, scoring="accuracy")
        imp = dict(zip(forecast_engine.FEATURE_KEYS, [float(x) / 10 for x in pi.importances_mean]))
        print(f"\n== [3] 特征重要性（全量，排列重要性，×10^2）==\n  " +
              ", ".join(f"{k}={v:.3f}" for k, v in sorted(imp.items(), key=lambda x: -x[1])))
    except Exception as e:
        print(f"\n== [3] 特征重要性不可用：{e}")

    # 汇总
    print("\n========================================")
    print("v5 多周期预测验证 · 汇总")
    print("========================================")
    for h, r in results.items():
        status = "✅ 通过" if r.get("ok") else ("⚠️ 样本不足" if not r.get("ok") and "reason" in r else "❌ 不通过")
        print(f"  T+{h:2d}: {status}  {r}")
    if overall_ok:
        print("\n✅ 三周期全部通过 → 可人工评估把 config.forecast.model_ready 置 true（仍需复核措辞红线）")
    else:
        print("\n❌ 存在周期未通过/样本不足 → 维持占位预测（model_ready=false），本报告留档为证据。")
        print("   改进方向：① 补更多历史净值/持仓快照；② 调 prob_flat_margin；③ 增加特征；④ 更长 OOS 观测。")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
