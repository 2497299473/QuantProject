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
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import numpy as np

from backtest_spread import load_samples, FWD_LIST, EXTRA_FWD, HISTORICAL_FEATURE_MODE
from frozen_dataset import resolve_samples   # V4.3 P0-1：统一冻结样本入口
import freeze_verify_tool                    # B++-2：git_commit 复用三件套口径，不新写取数
from core import forecast_contract, forecast_engine, validation_schema

# 确定性种子
RNG = random.Random(42)


# ---------- 指标 ----------
# B++-5（2026-09-23）：指标「不足/不可算」与真实 0 分离——
# status 核心（*_status）返回 (value|None, status)（core.validation_schema 四态），
# 证据层只吃 status 核心；遗留函数变薄外壳，哨兵行为逐位保留（rank_ic <10 → 0.0、
# NaN → 0.0；brier 空输入 → 0.0；calib 空输入 → ace=1.0），十余个既有消费方
# （lofo / walk_forward / rolling_oos / experiments…）零改动兼容，既有统计行为
# 不一次性大改。
def brier_multiclass_status(y_true, proba) -> tuple[float | None, str]:
    """多类 Brier status 核心：空输入/形状不良/标签越界 → (None, NOT_COMPUTABLE)。

    与遗留外壳的唯一分叉：n==0 时历史哨兵 0.0 在这里是 (None, NOT_COMPUTABLE)
    ——空样本没有 Brier 可言，0.0 是「完美」的语义，不能被空样本冒领。
    """
    proba = np.asarray(proba, dtype=float)
    if proba.ndim != 2 or proba.shape[0] == 0:
        return None, validation_schema.STATUS_NOT_COMPUTABLE
    n, k = proba.shape
    yt = np.asarray(y_true)
    if (len(yt) != n or np.any(yt.astype(int) != yt)
            or yt.min() < 0 or yt.max() >= k):
        return None, validation_schema.STATUS_NOT_COMPUTABLE
    onehot = np.zeros((n, k))
    onehot[np.arange(n), yt.astype(int)] = 1.0
    return (float(np.mean(np.sum((proba - onehot) ** 2, axis=1))),
            validation_schema.STATUS_OK)


def brier_multiclass(y_true: np.ndarray, proba: np.ndarray) -> float:
    """多类 Brier Score（越低越好，0=完美，0.667=三分类瞎猜）。

    B++-5：brier_multiclass_status 的遗留外壳——空输入沿用历史哨兵 0.0
    （这不是测量值），既有消费方行为逐位不变；证据层一律走 status 核心。
    """
    val, _ = brier_multiclass_status(y_true, proba)
    return 0.0 if val is None else float(val)


def calibration_curve(y_bool: np.ndarray, p: np.ndarray, nbins: int = 5) -> dict:
    """二值校准曲线：每桶预测概率 vs 实际频率（B++-5 返回新增 status 键）。

    bins/ace 数值与历史逐位一致（纯增量键，既有消费方只读 'ace' 不受影响）：
    空输入或无样本落入 [0,1] 域（无有效桶）→ status=NOT_COMPUTABLE，此时
    ace=1.0 是哨兵不是测量值；正常路径 → status=OK，ace 为真测量值（含真实 0.0）。
    """
    p = np.asarray(p, dtype=float)
    if len(p) == 0:
        return {"bins": [], "ace": 1.0,
                "status": validation_schema.STATUS_NOT_COMPUTABLE}
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
    if not bins:
        return {"bins": [], "ace": 1.0,
                "status": validation_schema.STATUS_NOT_COMPUTABLE}
    return {"bins": bins, "ace": ace / len(p), "status": validation_schema.STATUS_OK}


def rank_ic_status(xs, ys) -> tuple[float | None, str]:
    """Rank IC status 核心：<10 样本或 spearman NaN（恒值序列等）→ (None, NOT_COMPUTABLE)。

    真实 0 分（如对称构造 rho 恰为 0）→ (0.0, OK)——与「不可算」正式分家。
    """
    from scipy.stats import spearmanr
    if len(xs) != len(ys) or len(xs) < 10:
        return None, validation_schema.STATUS_NOT_COMPUTABLE
    r, _ = spearmanr(xs, ys)
    if r != r:                                   # NaN（恒值输入等）
        return None, validation_schema.STATUS_NOT_COMPUTABLE
    return float(r), validation_schema.STATUS_OK


def rank_ic(xs: list[float], ys: list[float]) -> float:
    """Spearman 秩相关（Rank IC）。

    B++-5：rank_ic_status 的遗留外壳——<10 与 NaN 沿用历史哨兵 0.0（不是
    测量值），既有消费方逐位不变；证据层一律走 status 核心。
    """
    val, _ = rank_ic_status(xs, ys)
    return 0.0 if val is None else float(val)


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


def decision_edge_metrics(score, est, yret, dates,
                          n_boot: int = 999, seed: int = 42) -> dict:
    """P0-1 两轨（2026-09-23）：decision_edge 审计指标。

    口径：decision_edge = RankIC(模型得分) − RankIC(est_chg 单因子)。
    同一 OOS 样本、同一 fwd 标签（NAV[T] 口径：前向收益基于净值，不改成
    估算基线标签）。回答「相对于 14:55 估值本身提供的机械基线，模型
    额外贡献多少排序信息？」（D2 prereg 的核心问题之一）。

    纪律：**只审计，不参与 ok 裁决门禁**（两轨——forecast 验证与决策边际
    分开记账，避免审计指标反噬裁决标准）。CI 复用 cluster_bootstrap_ci
    （按日块重抽样），不新造 bootstrap。样本不足时 CI 为 None、如实标注。
    """
    s = np.asarray(score, dtype=float)
    e = np.asarray(est, dtype=float)
    y = np.asarray(yret, dtype=float)
    edge = rank_ic(s.tolist(), y.tolist()) - rank_ic(e.tolist(), y.tolist())
    ci = cluster_bootstrap_ci(
        lambda sub: (rank_ic(sub["x"].tolist(), sub["y"].tolist())
                     - rank_ic(sub["est"].tolist(), sub["y"].tolist())),
        {"x": s, "est": e, "y": y}, list(dates), n_boot=n_boot, seed=seed)
    return {"edge": round(float(edge), 4),
            "edge_ci": [round(ci[0], 4), round(ci[1], 4)] if ci[0] == ci[0] else None}


# ---------- B++-2（2026-09-23）：validation evidence schema v2 产出 ----------
# 契约 B §12/§13 落地第一批：验证器直接产出 schema v2 证据（pooled + fund×horizon
# + provenance），fund 证据落盘而非只打 stdout。纯函数组表 + fail-closed 自检
# （core.validation_schema.validate_evidence），不 bind、不碰 registry。
def _ev_nc() -> dict:
    """NOT_COMPUTABLE 槽位简写（value 恒 null，不造 0）。"""
    return validation_schema.ev_na(validation_schema.STATUS_NOT_COMPUTABLE)


def _ev_ci(ci) -> dict:
    """cluster_bootstrap_ci 的 (lo, hi) → CI 槽位；nan（含交易日不足）→ NOT_COMPUTABLE。"""
    if ci is None or ci[0] != ci[0]:
        return _ev_nc()
    return validation_schema.ev_ok([round(float(ci[0]), 3), round(float(ci[1]), 3)])


def insufficient_power_node(n: int) -> dict:
    """未产出任何指标的 horizon 节点（样本不足/验证器跳过分支）。

    n 真实计数永远 OK；n==0 → 全指标 NOT_COMPUTABLE（无行可算，定义上
    算不出，不是“样本还不够”）；0<n<30 → INSUFFICIENT_POWER（临时闸门：
    对齐验证器既有 OOS n>=30 下限；正式 N_POWER_FUND 待功效预注册冻结）；
    n>=30 但未产出指标（如训练集不足未拟合）→ NOT_COMPUTABLE。
    """
    node = validation_schema.metric_node()
    node["n"] = validation_schema.ev_ok(int(n))
    st = (validation_schema.STATUS_NOT_COMPUTABLE if n <= 0
          else validation_schema.STATUS_INSUFFICIENT_POWER if n < 30
          else validation_schema.STATUS_NOT_COMPUTABLE)
    for k in node:
        if k != "n":
            node[k] = validation_schema.ev_na(st)
    return node


def evidence_node_from_rows(score_up, base_vec, yret, dates, p_train,
                            po=None, yo=None) -> dict:
    """schema v2 单 horizon 证据节点（pooled 与 fund 共用同一构建器）。

    产出端诚实化（B++-5 前置）：n==0 → NOT_COMPUTABLE；0<n<30 →
    INSUFFICIENT_POWER；秩相关族输入恒值（spearman 即 NaN）→ NOT_COMPUTABLE，
    不得伪装 0.0；CI 复用 cluster_bootstrap_ci（不新造 bootstrap），交易日
    不足其自带返回 nan → 如实 NOT_COMPUTABLE。po/yo 提供时补 Brier /
    b_majority（用传入 p_train，与 stdout 同源）/ midpoint_calibration_error。
    """
    yr = np.asarray(yret, dtype=float)
    if len(yr) < 30:
        return insufficient_power_node(len(yr))
    s = np.asarray(score_up, dtype=float)
    b = np.asarray(base_vec, dtype=float)
    dl = list(dates)

    def _ric_st(x, y):
        """B++-5：status 核心直取——恒值/样本不足 → (None, NOT_COMPUTABLE)，真实 0 保留。"""
        return rank_ic_status(x.tolist(), y.tolist())

    node = validation_schema.metric_node()
    node["n"] = validation_schema.ev_ok(int(len(yr)))
    ric_v, ric_st = _ric_st(s, yr)
    base_v, base_st = _ric_st(b, yr)
    node["rank_ic"] = (validation_schema.ev_ok(round(ric_v, 3))
                       if ric_st == validation_schema.STATUS_OK
                       else validation_schema.ev_na(ric_st))
    node["base_ic"] = (validation_schema.ev_ok(round(base_v, 3))
                       if base_st == validation_schema.STATUS_OK
                       else validation_schema.ev_na(base_st))
    if ric_st == validation_schema.STATUS_OK:
        node["rank_ic_ci"] = _ev_ci(cluster_bootstrap_ci(
            lambda sub: rank_ic(sub["x"].tolist(), sub["y"].tolist()),
            {"x": s, "y": yr}, dl))
    else:
        # 退化输入（恒值序列）不进 bootstrap——遗留 rank_ic 的 0.0 哨兵会把
        # 不可算伪装成 [0.0, 0.0] 假 CI（正是三态纪律要禁的混装）。
        node["rank_ic_ci"] = _ev_nc()
    if ric_st == validation_schema.STATUS_OK and base_st == validation_schema.STATUS_OK:
        node["decision_edge"] = validation_schema.ev_ok(round(ric_v - base_v, 3))
        node["decision_edge_ci"] = _ev_ci(cluster_bootstrap_ci(
            lambda sub: rank_ic(sub["x"].tolist(), sub["y"].tolist())
            - rank_ic(sub["e"].tolist(), sub["y"].tolist()),
            {"x": s, "e": b, "y": yr}, dl))
    else:
        node["decision_edge"] = _ev_nc()
        node["decision_edge_ci"] = _ev_nc()
    if po is not None and yo is not None and len(po) == len(yr):
        b_v, b_st = brier_multiclass_status(yo, po)
        node["brier"] = (validation_schema.ev_ok(round(b_v, 3))
                         if b_st == validation_schema.STATUS_OK
                         else validation_schema.ev_na(b_st))
        if b_st == validation_schema.STATUS_OK:
            node["brier_ci"] = _ev_ci(cluster_bootstrap_ci(
                lambda sub: brier_multiclass(sub["y"], sub["p"]),
                {"y": yo, "p": po}, dl))
        else:
            node["brier_ci"] = _ev_nc()
        onehot = np.zeros((len(yr), 3))
        onehot[np.arange(len(yr)), yo] = 1.0
        pt = np.asarray(p_train, dtype=float)
        node["b_majority"] = validation_schema.ev_ok(round(
            float(np.mean(np.sum((onehot - pt) ** 2, axis=1))), 3))
        y_up = (yo == 2).astype(int)
        calib = calibration_curve(y_up, po[:, 2])
        node[validation_schema.MIDPOINT_CALIBRATION_ERROR_KEY] = (
            validation_schema.ev_ok(round(calib["ace"], 3))
            if calib["status"] == validation_schema.STATUS_OK
            else validation_schema.ev_na(calib["status"]))
    return node


def pooled_node_from_results(r: dict) -> dict:
    """results[h]（stdout 报告字典，已 round）→ schema v2 pooled 节点。

    不重算、不转义——与当轮报告逐位同源；CI 为 None（nan）或缺字段如实
    NOT_COMPUTABLE，不造 0；跳过分支（reason）委托 insufficient_power_node。
    """
    if "reason" in r:
        return insufficient_power_node(int(r.get("n_oos", 0)))
    if not r:
        # 周期本轮未运行（results 无条目）：全 UNKNOWN（「还没跑」），
        # 不是 NOT_COMPUTABLE（那是「算了但定义上产不出结果」）——两态不混
        # （契约 B §13）。
        return validation_schema.metric_node()
    node = validation_schema.metric_node()
    node["n"] = validation_schema.ev_ok(int(r.get("n_oos", 0)))
    edge = r.get("decision_edge") or {}
    node["rank_ic"] = (validation_schema.ev_ok(r["rank_ic"])
                       if "rank_ic" in r else _ev_nc())
    node["rank_ic_ci"] = (validation_schema.ev_ok(list(r["ric_ci"]))
                          if r.get("ric_ci") is not None else _ev_nc())
    node["base_ic"] = validation_schema.ev_ok(r["base_ic"]) if "base_ic" in r else _ev_nc()
    node["decision_edge"] = (validation_schema.ev_ok(edge["edge"])
                             if "edge" in edge else _ev_nc())
    node["decision_edge_ci"] = (validation_schema.ev_ok(list(edge["edge_ci"]))
                                if edge.get("edge_ci") else _ev_nc())
    node["brier"] = validation_schema.ev_ok(r["oos_brier"]) if "oos_brier" in r else _ev_nc()
    node["brier_ci"] = (validation_schema.ev_ok(list(r["brier_ci"]))
                        if r.get("brier_ci") is not None else _ev_nc())
    node["b_majority"] = (validation_schema.ev_ok(r["b_majority"])
                          if "b_majority" in r else _ev_nc())
    node[validation_schema.MIDPOINT_CALIBRATION_ERROR_KEY] = (
        validation_schema.ev_ok(r["ace"]) if "ace" in r else _ev_nc())
    return node


def assemble_validation_evidence(results: dict, pooled_nodes: dict,
                                 fund_evidence: dict, horizons, snap_info: dict,
                                 *, overall_ok: bool, git_head: str | None,
                                 produced_at: str,
                                 model_sha256: str | None = None) -> dict:
    """本轮验证产出 → validation evidence schema v2（组表纯函数）。

    pooled：常规分支取 results[h]（与 stdout 逐位同源），跳过分支取
    pooled_nodes[h]；funds：fund_evidence[h][code]（同一 OOS 预测按行身份
    切片）；protocol 取 current_feature_protocol()（registry 同源，不另造字段）；
    power.frozen=OK(False)——功效阈值未预注册是可计算事实，其余 power 键如实
    UNKNOWN；quantile calibration 本验证器不产出 → 全 UNKNOWN；provenance
    透传冻结件三元组 + git head + 模型 artifact sha（本验证器不持久化模型 →
    诚实 UNKNOWN）+ EOD_PROXY 口径诚实标注。decision 只映射
    验证器既有 overall_ok，不引入第二套裁决。
    """
    proto = forecast_engine.current_feature_protocol()
    prov_snap = snap_info.get("snapshot_provenance") or {}
    ev = validation_schema.blank_evidence()
    ev["decision"] = "approved" if overall_ok else "rejected"
    ev["protocol"] = {"version": int(proto["protocol_version"]),
                      "feature_dim": int(proto["feature_dim"]),
                      "feature_keys": list(proto["feature_keys"]),
                      "masking": json.dumps(proto["masking"], ensure_ascii=False)}
    for h in horizons:
        c = validation_schema.canonical_horizon(h)
        if c is None:
            continue
        # 键型归一：调用方传入的 results/pooled_nodes/fund_evidence 可能以
        # int 或 str 作周期键（config horizons 历史上两型都有）——双路查找，
        # 查不到才落 UNKNOWN，杜绝键型不匹配静默降级。
        node = pooled_nodes.get(h, pooled_nodes.get(c))
        if node is not None:
            ev["pooled"][c] = node
        else:
            r = results.get(h, results.get(c))
            ev["pooled"][c] = pooled_node_from_results(r or {})
        fe = fund_evidence.get(h) or fund_evidence.get(c) or {}
        for code, node in fe.items():
            if code in ev["funds"]:
                ev["funds"][code][c] = node
    ev["power"]["frozen"] = validation_schema.ev_ok(False)
    sha = prov_snap.get("samples_sha256_lf")
    ev["provenance"] = {
        "artifact_sha256": (validation_schema.ev_ok(str(model_sha256)) if model_sha256
                            else validation_schema.ev_na(validation_schema.STATUS_UNKNOWN)),
        "frozen_dataset": validation_schema.ev_ok(
            str(prov_snap.get("snapshot_file") or snap_info.get("file")
                or snap_info.get("mode") or "unknown")),
        "dataset_sha256": (validation_schema.ev_ok(str(sha)) if sha
                           else validation_schema.ev_na(validation_schema.STATUS_UNKNOWN)),
        "git_commit": (validation_schema.ev_ok(str(git_head)) if git_head
                       else validation_schema.ev_na(validation_schema.STATUS_UNKNOWN)),
        "feature_protocol": validation_schema.ev_ok(
            f"protocol_version={proto['protocol_version']};"
            f"feature_dim={proto['feature_dim']};masking={ev['protocol']['masking']}"),
        "contract_version": validation_schema.ev_ok(forecast_contract.CONTRACT_VERSION),
        "historical_feature_mode": validation_schema.ev_ok(HISTORICAL_FEATURE_MODE),
        "produced_by": validation_schema.ev_ok("backtest_forecast.py"),
        "produced_at": validation_schema.ev_ok(str(produced_at)),
    }
    return ev


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
    # B 契约 §15-B3（2026-09-23）：Forecast 短周期不再被 fwd20 连坐——入口保留
    # 全部特征行（require_fwds=()），各 horizon 的标签可用性由 build_xy 按
    # fwd{h} 逐行过滤（既有逻辑），fwd1/3/5 各自拥有独立可用样本集合。
    # 冻结件路径不受影响（jsonl 行按落盘内容消费）；freeze_samples/spread 等
    # 消费方维持默认 FWD_LIST 口径，本批不改。
    samples, snap_info = resolve_samples(args.snapshot, args.fresh, BASE_DIR,
                                         lambda: load_samples(require_fwds=()))
    if snap_info["mode"] in ("MISSING", "INVALID"):
        return 4
    # V4.3.1 ④：报告首行区必须原样记录样本快照（DRIFTED/INCOMPLETE/UNKNOWN/FRESH
    # 均在此可见；stdout 即本报告主体，与其他 9 个入口的 report_line 纪律同款）
    print(snap_info["report_line"])
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
    pooled_nodes = {}    # B++-2：样本不足跳过分支的 pooled schema v2 节点
    fund_evidence = {}   # B++-2：fund×horizon schema v2 节点（同一 OOS 预测按行身份切片）
    overall_ok = True
    for h in horizons:
        print(f"\n=== Horizon T+{h} ===")
        XY = build_xy(train_all, h, flat_margin)
        XYo = build_xy(oos, h, flat_margin)
        if XY is None or len(XY[1]) < 100 or XYo is None or len(XYo[1]) < 30:
            print(f"  ⚠️ 样本不足（train={len(XY[1]) if XY else 0}, oos={len(XYo[1]) if XYo else 0}）→ 跳过")
            results[h] = {"ok": False, "reason": "insufficient_samples"}
            overall_ok = False
            # B++-2：跳过分支证据诚实化——n 真实计数，指标按三态标注
            n_oos_h = len(XYo[1]) if XYo is not None else 0
            pooled_nodes[h] = insufficient_power_node(n_oos_h)
            fund_evidence[h] = {
                code: insufficient_power_node(
                    sum(1 for f in XYo.funds if f == code) if XYo is not None else 0)
                for code in validation_schema.PRODUCTION_FUNDS}
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

        # P0-1 两轨（2026-09-23）：decision_edge 审计指标——模型相对 est_chg
        # 单因子机械基线的额外排序信息（OOS、同 fwd 标签、同样本）。只审计，
        # 不进下方 ok 裁决；数值进 results → stdout 报告（registry 绑定时留档）。
        edge_audit = decision_edge_metrics(score_up, base_vec, yreto,
                                           aligned_oos_dates)
        ci_txt = (f"[{edge_audit['edge_ci'][0]:+.3f}, {edge_audit['edge_ci'][1]:+.3f}]"
                  if edge_audit["edge_ci"] else "n/a（样本不足）")
        print(f"  decision_edge（审计，不进裁决）= {edge_audit['edge']:+.3f} "
              f"95% CI = {ci_txt}")

        # B++-2：四基金证据——同一份 OOS 预测按行身份切片（契约 B §6/F3：
        # fund×horizon 必须逐基金落盘，不得只打 stdout）。不重训、不新增模型。
        base_arr = np.asarray(base_vec, dtype=float)
        fund_evidence[h] = {}
        for code in validation_schema.PRODUCTION_FUNDS:
            m = np.array([f == code for f in XYo.funds], dtype=bool)
            fund_evidence[h][code] = evidence_node_from_rows(
                score_up[m], base_arr[m], yreto[m],
                [d for d, keep in zip(XYo.dates, m) if keep],
                p_train, po[m], yo[m])

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
                      "decision_edge": edge_audit,
                      "b_majority": round(b_majority, 3),
                      "ace": round(calib["ace"], 3), "n_train": int(len(y)), "n_oos": int(len(yo))}
        overall_ok = overall_ok and ok
        print(f"  → 裁决：{'✅ 通过' if ok else '❌ 不通过'}（需全部周期通过）")

    # ---- B++-2：validation evidence schema v2 组表 + fail-closed 自检 + 落盘 ----
    # 纪律：仅留档（不 bind_validation、不碰 registry/prereg）；自检不过拒落盘；
    # 证据产物异常只降级记录，绝不反噬验证裁决退出码。
    try:
        ev = assemble_validation_evidence(
            results, pooled_nodes, fund_evidence, horizons, snap_info,
            overall_ok=overall_ok,
            git_head=freeze_verify_tool.git_commit(),
            produced_at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
        ok_ev, errs_ev = validation_schema.validate_evidence(ev)
        if not ok_ev:
            print(f"\n[evidence] schema v2 自检未过，拒绝落盘（fail-closed）：{errs_ev[:3]}")
        else:
            outdir = BASE_DIR / "output" / "validation_evidence"
            outdir.mkdir(parents=True, exist_ok=True)
            ev_path = outdir / (
                f"validation_evidence_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
            ev_path.write_text(json.dumps(ev, ensure_ascii=False, indent=2),
                               encoding="utf-8")
            # stdout 纪律：不含运行时间戳（文件名才含）——本验证器的可复现性
            # 判据是「同命令连跑两次 stdout 逐字节一致」，变量只许落盘不进 stdout。
            print("\n[evidence] schema v2 证据已落盘 → output/validation_evidence/"
                  "validation_evidence_<运行时间戳>.json（仅留档，不参与授权）")
    except Exception as e:
        print(f"\n[evidence] 证据组表/落盘失败（不影响裁决）：{type(e).__name__}: {e}")

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
