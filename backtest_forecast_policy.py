#!/usr/bin/env python3
"""Forecast→Policy 联合回测（2026-08-29，GPT 三审 P2）。

问题：若按预测概率出动作（p_up≥θ 加 / p_down≥θ 减 / 否则不动），
     OOS 上相对「恒不动」有超额吗？ML 概率相对「est_chg 单因子同规则」有增量吗？
纪律（与项目铁律对齐）：
- **只产证据，不接下单**：输出报告落盘 output/，config.decision 门禁一律不动；
- 冻结切分 + label-end purge（与 backtest_forecast 同口径），OOS 段只评不训；
- 政策规则固定 θ=0.6（与 v4 动作阈值 ±60 同量级，避免「为过线调参」）；
- 次日实现收益 fwd1（T+1 决策 → 次日净值结算，与场外基金 T+1 口径一致）。

评估内容：
1. 政策超额：OOS 上「加日 fwd1 均值 − 不动日 fwd1 均值」（加 vs 减分列），
   cluster bootstrap（按日）95% CI；
2. ML vs 单因子：同一政策规则，p_up 换成 est_chg>0 的信号，超额差 = ML 增量；
3. 尾部标签对照：Q10 预测 vs 真 mdd5 的 RankIC（Q10 排序能否预示窗口内
   真实最大回撤——v7 移除 max_dd 后的替代证据）。

用法：python3 backtest_forecast_policy.py [--out 输出文件名]
"""
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import numpy as np

from backtest_spread import load_samples
from backtest_forecast import (split_date_oos, build_xy, rank_ic,
                               cluster_bootstrap_ci)
from core import forecast_engine as fe
from core import path_forecast as pf
from backtest_quantile_calib import feat_row as b1_feat_row   # B1 14 维双列特征

POLICY_THR = 0.6          # p_up ≥ θ 加 / p_down ≥ θ 减（固定，不调参）


def policy_actions(p_up: np.ndarray, p_down: np.ndarray) -> np.ndarray:
    """政策动作：2=加 1=不动 0=减（θ 固定）。"""
    act = np.ones(len(p_up), dtype=int)
    act[p_up >= POLICY_THR] = 2
    act[p_down >= POLICY_THR] = 0
    return act


def eval_policy(name: str, act: np.ndarray, fwd1: np.ndarray,
                dates: list[str]) -> dict:
    """政策回测：各动作组 fwd1 均值 + 加−不动 / 减−不动 超额 + 按日 CI。"""
    out = {"name": name}
    for label, code in (("add", 2), ("hold", 1), ("reduce", 0)):
        m = act == code
        out[f"n_{label}"] = int(m.sum())
        out[f"mean_{label}"] = float(fwd1[m].mean()) if m.sum() else 0.0
    if out["n_add"]:
        out["excess_add"] = out["mean_add"] - out["mean_hold"]
    if out["n_reduce"]:
        out["excess_reduce"] = out["mean_reduce"] - out["mean_hold"]
    # 按日 cluster bootstrap：加日超额 CI（把每日「加日均值−不动日均值」当块）
    uniq = sorted(set(dates))
    if len(uniq) >= 10 and out["n_add"]:
        daily = {}
        for d, a, r in zip(dates, act, fwd1):
            v = daily.setdefault(d, {"a": [], "h": []})
            if a == 2:
                v["a"].append(r)
            elif a == 1:
                v["h"].append(r)
        blocks = []
        for d in uniq:
            v = daily.get(d)
            if v and v["a"] and v["h"]:
                blocks.append(np.mean(v["a"]) - np.mean(v["h"]))
        if len(blocks) >= 10:
            arr = np.array(blocks)
            rng = np.random.default_rng(42)
            boots = []
            for _ in range(999):
                samp = rng.choice(blocks, size=len(blocks), replace=True)
                boots.append(float(np.mean(samp)))
            out["excess_add_ci"] = [round(float(np.percentile(boots, 2.5)), 5),
                                    round(float(np.percentile(boots, 97.5)), 5)]
    return out


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None,
                    help="输出文件名（默认 backtest_forecast_policy_YYYYMMDD.md）")
    args = ap.parse_args()

    t0 = time.time()
    print("== [1] 加载样本（PIT 口径）==")
    samples = load_samples()
    if not samples:
        print("[fail] 无样本")
        return 1
    samples.sort(key=lambda s: (s["date"], s["fund"]))

    train, oos, oos_start = split_date_oos(samples)
    print(f"== [2] 冻结切分（purge）：train < {oos_start}（{len(train)}），"
          f"OOS ≥ {oos_start}（{len(oos)}）==")

    # 训练（7 基线特征，T+1 方向三分类；与线上同口径）
    from sklearn.ensemble import HistGradientBoostingClassifier
    h = 1
    flat_margin = 0.003
    XY = build_xy(train, h, flat_margin)
    if XY is None:
        print("[fail] 训练样本不足")
        return 1
    X, y, _ = XY
    clf = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.08,
                                         max_depth=3, early_stopping=True,
                                         random_state=42)
    clf.fit(X, y)

    # OOS 推理
    XYo = build_xy(oos, h, flat_margin)
    if XYo is None:
        print("[fail] OOS 样本不足")
        return 1
    Xo, yo, yreto = XYo
    proba = clf.predict_proba(Xo)
    p_up, p_down = proba[:, 2], proba[:, 0]
    fwd1 = yreto
    dates = XYo.dates

    act_ml = policy_actions(p_up, p_down)
    act_base = policy_actions((Xo[:, fe.FEATURE_KEYS.index("est_chg")] > 0).astype(float),
                              (Xo[:, fe.FEATURE_KEYS.index("est_chg")] < 0).astype(float))

    print(f"\n== [3] 政策回测（θ={POLICY_THR}，次日实现收益 fwd1，OOS {len(dates)} 样本）==")
    r_ml = eval_policy("ML概率", act_ml, fwd1, dates)
    r_base = eval_policy("est_chg单因子", act_base, fwd1, dates)

    # 尾部标签对照：Q10 预测 vs 真 mdd5/mfe5
    # （方向三分类无 Q10，复用 ReturnQuantileModel 同口径回归器）
    qm = fe.ReturnQuantileModel(h)
    ok = qm.fit(train)
    ric_mdd = ric_mfe = None
    if ok:
        q10_list, mdd_list, mfe_list = [], [], []
        for s in oos:
            if s.get("fwd1") is None or any(s.get(k) is None for k in fe.FEATURE_KEYS):
                continue
            if s.get("mdd5") is None:
                continue
            # B1 后 ReturnQuantileModel 为 14 维双列特征（值+missing_mask），
            # 旧 7 维 vec 会 predict 崩溃（2026-09-01 实跑发现，B1 落地后此脚本首次重跑）
            dist = qm.predict_dist(b1_feat_row(s))
            q10_list.append(dist["q10"])
            mdd_list.append(float(s["mdd5"]))
            if s.get("mfe5") is not None:
                mfe_list.append(float(s["mfe5"]))
        if len(q10_list) >= 10:
            ric_mdd = rank_ic(q10_list, mdd_list)
        if len(mfe_list) >= 10:
            ric_mfe = rank_ic(q10_list, mfe_list)

    # path forecast 校准对照（train 段 μ/近期 σ 蒙特卡洛 vs OOS 经验 mdd5）
    # 2026-09-01 六审修：v1.2 近期 σ 若用 oos 会前视（OOS 尾部数据估 σ 再评 OOS 全段）。
    # 固定模型 OOS 评估一律用 train 段参数；WF 化部署口径挂 09-26。
    path = pf.forecast_path(train, horizon=5)
    emp_mdd = [s["mdd5"] for s in oos if s.get("mdd5") is not None]

    # ---- 报告 ----
    lines = [
        "# Forecast→Policy 联合回测（v7 证据留档）", "",
        f"> 生成：{time.strftime('%Y-%m-%d %H:%M')} · OOS ≥ {oos_start} · "
        f"样本 {len(dates)}（{len(set(dates))} 日）· 政策 θ={POLICY_THR} · 只产证据不接下单", "",
        "**纪律**：本报告不改变 config.decision 任何门禁；动作层启用仍需"
        " backtest_action.py 历史验证 + Summer 人工拍板。", "",
        "## 一、政策超额（OOS，次日实现收益 fwd1）", "",
        "| 政策 | 加n | 加日均值 | 不动日均值 | 减n | 减日均值 | 加−不动 | 减−不动 | 加超额95%CI |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in (r_ml, r_base):
        ci = r.get("excess_add_ci")
        ci_txt = f"[{ci[0]*100:+.3f}%, {ci[1]*100:+.3f}%]" if ci else "—"
        lines.append(
            f"| {r['name']} | {r['n_add']} | {r['mean_add']*100:+.3f}% | "
            f"{r['mean_hold']*100:+.3f}% | {r['n_reduce']} | {r['mean_reduce']*100:+.3f}% | "
            f"{r.get('excess_add', 0)*100:+.3f}% | {r.get('excess_reduce', 0)*100:+.3f}% | "
            f"{ci_txt} |")
    ml_ex = r_ml.get("excess_add", 0)
    base_ex = r_base.get("excess_add", 0)
    verdict = ("✅ ML 政策有超额且优于单因子" if ml_ex > 0 and ml_ex > base_ex
               else "⚠️ ML 政策无稳定超额或不如单因子（证据不支持接下单）")
    lines += [
        "", f"**ML 相对单因子增量**：{ml_ex*100:+.3f}% vs {base_ex*100:+.3f}% → **{verdict}**", "",
        "## 二、尾部标签对照（Q10 预测 vs 真 MDD/MFE）", "",
        f"- Q10 vs 真 mdd5 RankIC = {f'{ric_mdd:+.4f}' if ric_mdd is not None else '样本不足'}"
        f"（正=Q10 更差时窗口内真实回撤更深，Q10 有尾部排序力）",
        f"- Q10 vs 真 mfe5 RankIC = {f'{ric_mfe:+.4f}' if ric_mfe is not None else '样本不足'}", "",
        "## 三、Path Forecast 校准对照（T+5，全局 μ/σ 蒙特卡洛）", "",
    ]
    if path.model_ready and emp_mdd:
        lines += [
            f"| 口径 | MDD Q10 | MDD Q50 | MFE Q50 | 终点 Q10 | 终点 Q90 |",
            f"|---|---:|---:|---:|---:|---:|",
            f"| 模拟（{pf.DEFAULT_N_PATHS}路径） | {path.mdd_q10*100:+.2f}% | "
            f"{path.mdd_q50*100:+.2f}% | {path.mfe_q50*100:+.2f}% | "
            f"{path.q10*100:+.2f}% | {path.q90*100:+.2f}% |",
            f"| OOS 经验（n={len(emp_mdd)}） | "
            f"{np.percentile(emp_mdd, 10)*100:+.2f}% | {np.percentile(emp_mdd, 50)*100:+.2f}% | "
            f"— | {np.percentile(yreto, 10)*100:+.2f}% | {np.percentile(yreto, 90)*100:+.2f}% |",
            "", "<sub>train 段 μ/基金级近期 σ 蒙特卡洛（2026-09-01 修：禁用 OOS 估 σ，防前视；"
            "v1.3 σ 窗口 = 每基金最近 20 个交易日等权拼接，修「20 行≠20 交易日」）；"
            "不区分状态，仅作量级校准对照；条件化/逐基金报告为后续迭代项。</sub>",
        ]
    else:
        lines += ["_（path forecast 未就绪：numpy 缺失或日收益样本不足）_"]
    lines += ["", f"---", f"*生成耗时 {time.time()-t0:.0f}s · 观察层证据，不构成投资建议*", ""]

    report = "\n".join(lines)
    out = BASE_DIR / "output" / (args.out or f"backtest_forecast_policy_{time.strftime('%Y%m%d')}.md")
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"[done] 报告 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
