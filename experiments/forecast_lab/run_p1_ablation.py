"""P1-③④ 首训 + 消融对照（2026-09-10；Summer 09-09 拍板 P1 提前至 09-10）。

问题：现有 v7（7 特征）的 T+5 RankIC 上不去，是**特征瓶颈**还是模型/目标问题？
本脚本用同一套 WF 协议、同一批冻结样本，只换特征集，给出可比的 T+5 pooled RankIC。

离线纪律（零网络）
- 样本来自 ① 冻结产物 forecast_outputs/samples_frozen_YYYYMMDD.jsonl，**不重跑 load_samples**。
- 净值来自 data/klines/{code}.json 缓存；先校验新鲜度（TTL 见 config.json），
  过期即中止并报告——绝不在本脚本内触发任何抓取。
- 只用 .venv-lab（LightGBM 4.7.0）；主 .venv 不装 lgb。

协议（与既有 WF 一致，不另立口径）
- split_date_oos(ratio=0.8, max_horizon=5) 定 OOS 段；build_wf_folds(window_days=63)
  逐折 expanding 重训，label-end purge 沿用。
- 主判据：T+5 pooled WF RankIC + cluster bootstrap 95% CI（按日块 999 次、seed 42）。
- baseline hierarchy（逐级不劣于）：瞎猜 → 多类 → est_chg 单因子 → existing_v7 → lgbm_a158。
- 消融（只换特征，模型同为 LightGBM 回归）：existing(7) / a158(50) / union(57)。

用法：
  .\\.venv-lab\\Scripts\\python.exe -X utf8 experiments\\forecast_lab\\run_p1_ablation.py
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "experiments" / "forecast_lab"))

import numpy as np                                            # noqa: E402

import lightgbm as lgb                                        # noqa: E402
from sklearn.ensemble import HistGradientBoostingClassifier    # noqa: E402

from backtest_forecast import (cluster_bootstrap_ci, rank_ic,  # noqa: E402
                               split_date_oos)
from backtest_walk_forward import build_wf_folds               # noqa: E402
from core import forecast_engine                               # noqa: E402
from expert_protocol import ExpertOutput, write_records        # noqa: E402
from features_a158lite import feature_keys                     # noqa: E402

HORIZON = 5
SEED = 42
N_BOOT = 999
FLAT_MARGIN = 0.003
A158_KEYS = feature_keys()
EXISTING_KEYS = list(forecast_engine.FEATURE_KEYS)
LGB_PARAMS = dict(objective="regression", learning_rate=0.05, num_leaves=31,
                  min_data_in_leaf=40, feature_fraction=0.8, bagging_fraction=0.8,
                  bagging_freq=1, verbosity=-1, seed=SEED, deterministic=True,
                  num_threads=2, force_row_wise=True)
N_ROUNDS = 300


# ---------------------------------------------------------------- 数据装载
def load_nav_cache(code: str, ttl_hours: float) -> list[tuple[str, float]]:
    """只读缓存读净值（升序）。过期/缺失即抛错——本脚本绝不联网。"""
    p = BASE / "data" / "klines" / f"{code}.json"
    if not p.exists():
        raise FileNotFoundError(f"净值缓存缺失：{p}")
    age_h = (datetime.now().timestamp() - p.stat().st_mtime) / 3600.0
    if age_h > ttl_hours:
        raise RuntimeError(f"{code} 净值缓存已过期（{age_h:.1f}h > {ttl_hours}h）；"
                           f"请先手动刷新，禁止在本脚本内联网抓取")
    data = json.loads(p.read_text(encoding="utf-8"))
    navs = [(str(d), float(v)) for d, v in data["navs"]]
    navs.sort(key=lambda t: t[0])
    return navs


def load_frozen_samples(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


# ---------------------------------------------------------------- 特征矩阵
def _mask_row(values) -> list[float]:
    """B1 协议：每特征 (值, 缺失掩码) 双列；缺失值填 0、掩码置 1。"""
    row = []
    for v in values:
        ok = v is not None and isinstance(v, (int, float)) and math.isfinite(float(v))
        row.append(float(v) if ok else 0.0)
        row.append(0.0 if ok else 1.0)
    return row


def matrix_existing(samples) -> np.ndarray:
    return np.array([_mask_row([s.get(k) for k in EXISTING_KEYS]) for s in samples],
                    dtype=float)


def matrix_a158(samples, a158_map) -> np.ndarray:
    rows = []
    for s in samples:
        f = a158_map.get((s["fund"], s["date"]), {})
        rows.append(_mask_row([f.get(k) for k in A158_KEYS]))
    return np.array(rows, dtype=float)


def matrix_union(samples, a158_map) -> np.ndarray:
    return np.hstack([matrix_existing(samples), matrix_a158(samples, a158_map)])


def targets(samples) -> np.ndarray:
    return np.array([float(s[f"fwd{HORIZON}"]) for s in samples], dtype=float)


def with_label(samples) -> list[dict]:
    return [s for s in samples if s.get(f"fwd{HORIZON}") is not None]


# ---------------------------------------------------------------- 模型
def lgb_reg_fit_predict(Xtr, ytr, Xte, params=None, rounds=N_ROUNDS) -> np.ndarray:
    ds = lgb.Dataset(Xtr, label=ytr)
    booster = lgb.train(params or LGB_PARAMS, ds, num_boost_round=rounds)
    return np.asarray(booster.predict(Xte), dtype=float)


def hgb_v7_fit_predict(Xtr, ytr, Xte) -> np.ndarray:
    """existing_v7 口径：三分类 HGB（flat_margin 分箱），以 P(up) 排序。"""
    y = np.where(ytr > FLAT_MARGIN, 2, np.where(ytr < -FLAT_MARGIN, 0, 1))
    clf = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.08,
                                         max_depth=3, early_stopping=True,
                                         random_state=SEED)
    clf.fit(Xtr, y)
    proba = clf.predict_proba(Xte)
    col = list(clf.classes_).index(2) if 2 in clf.classes_ else -1
    return np.asarray(proba[:, col], dtype=float)


# ---------------------------------------------------------------- WF 评估
def run_wf(folds, matrix_fn, fit_fn) -> dict:
    """逐折 expanding 重训；返回 pooled 预测/真值/日期 + 每折 RankIC。"""
    preds, trues, dates, per_fold = [], [], [], []
    for fd in folds:
        train, test = with_label(fd["train"]), with_label(fd["test"])
        if len(train) < 100 or len(test) < 2:
            continue
        Xtr, Xte = matrix_fn(train), matrix_fn(test)
        ytr = targets(train)
        p = fit_fn(Xtr, ytr, Xte)
        yte = targets(test)
        ic = rank_ic(list(p), list(yte))
        per_fold.append({"test_start": fd["test_start"], "cutoff": fd["cutoff"],
                         "n_test": len(test), "rank_ic": round(ic, 4)})
        preds.extend(list(p))
        trues.extend(list(yte))
        dates.extend([s["date"] for s in test])
    if not preds:
        return {"n": 0, "rank_ic": float("nan"), "ci": (float("nan"), float("nan")),
                "per_fold": []}
    pooled = rank_ic(preds, trues)
    lo, hi = cluster_bootstrap_ci(lambda sub: rank_ic(list(sub["p"]), list(sub["y"])),
                                  {"p": np.array(preds), "y": np.array(trues)},
                                  dates, n_boot=N_BOOT, seed=SEED)
    return {"n": len(preds), "rank_ic": round(float(pooled), 4),
            "ci": (None if math.isnan(lo) else round(lo, 4),
                   None if math.isnan(hi) else round(hi, 4)),
            "per_fold": per_fold, "dates": dates, "preds": preds, "trues": trues}


def main() -> int:
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    ttl = float(cfg["data"]["cache_ttl_hours"])
    stamp = datetime.now().strftime("%Y%m%d")
    jsonl = BASE / "forecast_outputs" / f"samples_frozen_{stamp}.jsonl"
    meta_p = BASE / "forecast_outputs" / f"samples_frozen_{stamp}.meta.json"
    if not jsonl.exists():
        print(f"[ABORT] 冻结样本不存在：{jsonl}（先跑 ① freeze_samples.py）")
        return 1
    meta = json.loads(meta_p.read_text(encoding="utf-8"))

    print("== [0] 离线装载 ==")
    samples = load_frozen_samples(jsonl)
    raw = jsonl.read_text(encoding="utf-8")
    sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    print(f"  样本 {len(samples)} 行；sha256 {sha[:16]}… "
          f"（meta 记录 {meta['sha256'][:16]}…）")
    if sha != meta["sha256"]:
        print("  [ABORT] 冻结样本 sha256 与 meta 不一致——样本已被改动，停止。")
        return 1

    funds = cfg["fund_pool"]
    nav_by_fund = {}
    for code in funds:
        nav_by_fund[code] = load_nav_cache(code, ttl)
        print(f"  {code}: 净值 {len(nav_by_fund[code])} 条（缓存命中，零网络）")

    from features_a158lite import build_feature_rows
    print("== [1] A158-lite 特征（PIT：样本日只用 ≤T-1 净值）==")
    a158_map = build_feature_rows(samples, nav_by_fund)
    n_ok = sum(1 for v in a158_map.values() if not all(math.isnan(x) for x in v.values()))
    print(f"  特征数/样本 {len(A158_KEYS)}；可用样本 {n_ok}/{len(a158_map)}"
          f"（其余历史不足 61 点，全 NaN）")

    print("== [2] 冻结切分 + WF 折点 ==")
    train_all, oos, oos_start = split_date_oos(samples, ratio=0.8, max_horizon=HORIZON)
    folds = build_wf_folds(samples, oos, oos_start, window_days=63,
                           max_horizon=HORIZON)
    print(f"  oos_start={oos_start}  OOS 样本 {len(oos)}  折数 {len(folds)}")

    print("== [3] 消融：只换特征集，模型同为 LightGBM 回归 ==")
    results = {}
    for name, fn in (("existing(7)", matrix_existing),
                     ("a158(50)", lambda ss: matrix_a158(ss, a158_map)),
                     ("union(57)", lambda ss: matrix_union(ss, a158_map))):
        r = run_wf(folds, fn, lgb_reg_fit_predict)
        results[name] = r
        print(f"  {name:12s} pooled RankIC={r['rank_ic']:+.4f}  CI={r['ci']}  n={r['n']}")

    print("== [4] baseline hierarchy（T+5）==")
    # L1 瞎猜 / L2 多类：理论 RankIC = 0（常数预测无排序信息）
    hierarchy = [{"level": "1 瞎猜", "rank_ic": 0.0, "ci": (None, None)},
                 {"level": "2 多类", "rank_ic": 0.0, "ci": (None, None)}]
    # L3 est_chg 单因子（同一 pooled 测试窗）
    oos_lbl = with_label(oos)
    est = np.array([float(s["est_chg"]) for s in oos_lbl])
    est_y = targets(oos_lbl)
    est_d = [s["date"] for s in oos_lbl]
    lo, hi = cluster_bootstrap_ci(lambda sub: rank_ic(list(sub["p"]), list(sub["y"])),
                                  {"p": est, "y": est_y}, est_d,
                                  n_boot=N_BOOT, seed=SEED)
    hierarchy.append({"level": "3 est_chg 单因子", "rank_ic": round(rank_ic(list(est), list(est_y)), 4),
                      "ci": (None if math.isnan(lo) else round(lo, 4),
                             None if math.isnan(hi) else round(hi, 4))})
    r_v7 = run_wf(folds, matrix_existing, hgb_v7_fit_predict)
    hierarchy.append({"level": "4 existing_v7（HGB 7 特征）",
                      "rank_ic": r_v7["rank_ic"], "ci": r_v7["ci"]})
    hierarchy.append({"level": "5 lgbm_a158（LGB 50 特征）",
                      "rank_ic": results["a158(50)"]["rank_ic"],
                      "ci": results["a158(50)"]["ci"]})
    for h in hierarchy:
        print(f"  {h['level']:26s} RankIC={h['rank_ic']:+.4f}  CI={h['ci']}")
    mono = all(hierarchy[i + 1]["rank_ic"] >= hierarchy[i]["rank_ic"] - 1e-9
               for i in range(len(hierarchy) - 1))
    print(f"  逐级不劣于检查：{'通过' if mono else '未通过（见报告）'}")

    print("== [5] 输出 ExpertOutput 契约记录（末折测试窗）==")
    recs, n_cross = [], 0
    last = folds[-1] if folds else None
    if last:
        train, test = with_label(last["train"]), with_label(last["test"])
        Xtr, Xte = matrix_a158(train, a158_map), matrix_a158(test, a158_map)
        ytr = targets(train)
        mu = lgb_reg_fit_predict(Xtr, ytr, Xte)
        qs = {}
        for alpha in (0.1, 0.5, 0.9):
            qs[alpha] = lgb_reg_fit_predict(
                Xtr, ytr, Xte,
                params={**LGB_PARAMS, "objective": "quantile", "alpha": alpha})
        ybin = (ytr > 0).astype(int)
        clf = lgb.train({**LGB_PARAMS, "objective": "binary",
                         "metric": "binary_logloss"}, lgb.Dataset(Xtr, label=ybin),
                        num_boost_round=N_ROUNDS)
        pu = np.asarray(clf.predict(Xte), dtype=float)
        for i, s in enumerate(test):
            q = sorted([float(qs[0.1][i]), float(qs[0.5][i]), float(qs[0.9][i])])
            if q != [float(qs[0.1][i]), float(qs[0.5][i]), float(qs[0.9][i])]:
                n_cross += 1
            recs.append(ExpertOutput(
                schema_version="1", expert="lgbm_a158", fund=s["fund"], asof=s["date"],
                horizon=HORIZON, expected_return=float(mu[i]),
                prob_up=float(min(1.0, max(0.0, pu[i]))), q10=q[0], q50=q[1], q90=q[2],
                q_source="empirical", confidence=float("nan"),
                meta={"features": len(A158_KEYS), "model": "lightgbm",
                      "fold_test_start": last["test_start"], "q_sorted": True}))
        out_rec = BASE / "forecast_outputs" / f"expert_lgbm_a158_{stamp}.jsonl"
        write_records(out_rec, recs, append=False)
        print(f"  {len(recs)} 条 → {out_rec.name}；分位交叉矫正 {n_cross} 条（排序兜底）")

    print("== [6] 写报告 ==")
    rep = BASE / "output" / f"forecast_lab_p1_{stamp}.md"
    rep.write_text(render_report(samples, meta, sha, a158_map, n_ok, oos_start,
                                 len(folds), results, hierarchy, mono, r_v7,
                                 len(recs), n_cross, len(A158_KEYS)), encoding="utf-8")
    print(f"  已写 {rep}")
    print(f"  JSON 摘要: {json.dumps({'ablation': {k: {'ic': v['rank_ic'], 'ci': v['ci']} for k, v in results.items()}, 'hierarchy': hierarchy, 'monotone': mono}, ensure_ascii=False)}")
    return 0


def render_report(samples, meta, sha, a158_map, n_ok, oos_start, n_folds, results,
                  hierarchy, mono, r_v7, n_rec, n_cross, n_feat) -> str:
    L = [f"# Forecast Lab P1 首训 + 消融对照（{datetime.now():%Y-%m-%d}）", "",
         "> paper-only 研究层。输出不构成实盘建议。", "",
         "## 〇、结论", ""]
    a, u = results["a158(50)"], results["union(57)"]
    e = results["existing(7)"]
    L.append(f"- **A158-lite（{n_feat} 特征）T+5 pooled WF RankIC = {a['rank_ic']:+.4f}**"
             f"（CI {a['ci']}）；现有 7 特征同模型 = {e['rank_ic']:+.4f}；并集 = {u['rank_ic']:+.4f}")
    L.append(f"- baseline hierarchy 逐级不劣于：**{'通过' if mono else '未通过'}**")
    L.append("")
    L.append("## 一、口径与纪律")
    L.append(f"- 冻结样本：`{meta['jsonl']}`，{len(samples)} 行，sha256 `{sha[:16]}…`（与 meta 一致）")
    L.append(f"- 特征：A158-lite {n_feat} 个（10 族 × 5 窗），PIT 只用 ≤T-1 净值；"
             f"可用样本 {n_ok}/{len(a158_map)}")
    L.append("- CORR 口径替代：Alpha158 原为 corr(close, log(volume))；基金无成交量 → "
             "用**收益滞后 1 阶自相关**替代（已在 ② 模块显式标注）")
    L.append(f"- WF：`build_wf_folds(window_days=63)`，oos_start={oos_start}，折数 {n_folds}，"
             "label-end purge 沿用既有协议")
    L.append(f"- 主判据：T+5 pooled WF RankIC + cluster bootstrap（按日块 {N_BOOT} 次，seed {SEED}）")
    L.append("")
    L.append("## 二、消融（只换特征集，模型同为 LightGBM 回归）")
    L.append("")
    L.append("| 特征集 | pooled RankIC | 95% CI | n |")
    L.append("|---|---|---|---|")
    for k in ("existing(7)", "a158(50)", "union(57)"):
        r = results[k]
        L.append(f"| {k} | {r['rank_ic']:+.4f} | {r['ci']} | {r['n']} |")
    L.append("")
    L.append("## 三、baseline hierarchy（T+5，逐级不劣于）")
    L.append("")
    L.append("| 级别 | pooled RankIC | 95% CI |")
    L.append("|---|---|---|")
    for h in hierarchy:
        L.append(f"| {h['level']} | {h['rank_ic']:+.4f} | {h['ci']} |")
    L.append("")
    L.append("## 四、逐折明细")
    L.append("")
    for k in ("existing(7)", "a158(50)", "union(57)"):
        folds_txt = ", ".join(f"{f['test_start']}:{f['rank_ic']:+.3f}"
                              for f in results[k]["per_fold"])
        L.append(f"- {k}: {folds_txt}")
    L.append(f"- existing_v7(HGB): " + ", ".join(
        f"{f['test_start']}:{f['rank_ic']:+.3f}" for f in r_v7["per_fold"]))
    L.append("")
    L.append("## 五、专家输出契约")
    L.append(f"- `expert_lgbm_a158_{datetime.now():%Y%m%d}.jsonl`：{n_rec} 条 ExpertOutput"
             f"（expert=`lgbm_a158`，q_source=`empirical`，分位交叉排序兜底 {n_cross} 条）")
    L.append("- confidence 一律 NaN（尚无校准层，不填 0.5 假装中性）")
    L.append("")
    L.append("## 六、待决")
    L.append("- 见日汇总「待拍板」板块（本报告只陈述事实，不自行推进闸门）")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main())
