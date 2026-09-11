"""仓位动作评分器（Decision Engine）——观察层倾向分 + 动作层硬门禁。

背景（GPT-5.6 诊断 + 项目 2026-08-25 三轮回测裁决的融合结论）：
- GPT 建议把「偏多/偏空/中性」升级为「加仓/减仓/不动 + 置信度 + 理由 + 无效条件」，
  并给出五维加权公式（35/25/15/15/10）与四道准入门槛。
- 但项目自己的回测铁律（回测校准-20260822 + 买卖判断回测验证-20260825）已裁决：
  实时涨跌+缠论+三因子的组合历史验证不通过（B/D 失败、2025 OOS 方向翻转 -2.50%、
  过拟合比 -0.87）→ 当时结论「不输出加/减/不动」。
- 融合解法：引擎结构按 GPT 建（DecisionInput → Decision），但动作输出被
  config.decision.gates.history_validated 硬门禁锁死。当前 false → 只输出「倾向分 +
  候选动作」，action 恒为 HOLD（保持不动/观察）。只有 backtest_action.py 证明
  「动作 vs 不动」有稳定超额后，人工把 history_validated 置 true 才启用动作输出。
- 阈值 ±60 与五维权重均为框架初始值（GPT 建议，未经回测校准），同样被门禁挡住。

倾向分口径：五维加权和 → 归一化到 -100 ~ +100。
子分范围：日内趋势 ±40 / 重仓一致性 ±20 / 池内相对 ±15 / 中期趋势 ±15 / 账户 ±10。

输入各维度（可解释、可回测）：
- 日内趋势 35%：14:55 估算涨跌（tanh 映射 ±30）+ 午盘→尾盘变化（tanh 映射 ±10）
- 重仓一致性 25%：前十大同向度（±15）+ 集中度惩罚（-8）
- 池内相对 15%：本基金实时估算 vs 池内其他基金估算均值（行业指数基准留作后续接入）
- 中期趋势 15%：原三因子总分（保留，仅作解释维度，不新增信号）
- 账户约束 10%：仓位余量 / 成本保护 / 连续加仓限制
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

ACTION_CN = {"ADD": "加仓", "REDUCE": "减仓", "HOLD": "保持不动"}

# 各维度子分范围（与 _score_* 实现一致，归一化分母用）
_MAX_BY_DIM = {"intraday_trend": 40, "breadth": 20, "relative_pool": 15,
               "mid_trend": 15, "account": 10}


@dataclass
class Decision:
    action: str                        # 实际输出：history_validated=false 时恒为 HOLD
    candidate: str                     # 若动作层已启用本应输出的动作（ADD/REDUCE/HOLD）
    score: int                         # 倾向分 -100 ~ +100
    confidence: float                  # 0.0 ~ 1.0
    reasons: list[str] = field(default_factory=list)
    invalid_conditions: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    # v5 新增（GPT-5.6Luna 评审落地，2026-08-26）：多周期条件分布预测。
    # 预测是一级公民，动作是最后一层。account 仓位不进市场预测（见 forecast_engine）。
    forecast: dict = field(default_factory=dict)   # forecast_to_cn 输出（T1/T3/T5/state/path）


@dataclass
class DecisionInput:
    code: str
    name: str
    slot: str                          # mid / post
    technical_score: int = 0           # 原三因子总分 -3 ~ +3
    feat_1130: dict | None = None      # intraday_features 输出的 11:30 特征
    feat_1455: dict | None = None      # intraday_features 输出的 14:55 特征
    pool_est: dict | None = None       # {code: est_change_pct(%)} 池内所有基金实时估算
    account_state: dict | None = None  # {current_weight, max_weight, cost_nav, last_nav, consecutive_adds}
    state_ref: dict | None = None      # State Engine 只读参考（2026-08-27）：不参与评分


def _load_cfg() -> dict:
    return json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))


def _clamped(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _est_of(feat: dict | None) -> float | None:
    if not feat:
        return None
    return feat.get("est_return")


def _merged(feat_1130: dict | None, feat_1455: dict | None) -> dict:
    """合并两个时点特征：14:55 优先，缺失字段回退 11:30。"""
    out: dict = {}
    for f in (feat_1130, feat_1455):
        if f:
            out.update(f)
    return out


def _score_intraday(feat_1130: dict | None, feat_1455: dict | None) -> tuple[int, list[str]]:
    """日内趋势 ±40：估算涨跌为主（tanh(est/2)*30），午盘→尾盘变化为辅（tanh(Δ/1)*10）。"""
    est = _est_of(feat_1455)
    if est is None:
        est = _est_of(feat_1130)
    reasons: list[str] = []
    if est is None:
        return 0, ["实时估算不可用"]
    base = math.tanh(est / 2.0) * 30.0
    adj = 0.0
    e1130, e1455 = _est_of(feat_1130), _est_of(feat_1455)
    if e1130 is not None and e1455 is not None:
        phase = e1455 - e1130
        adj = math.tanh(phase / 1.0) * 10.0
        reasons.append(f"11:30 {e1130:+.2f}% → 14:55 {e1455:+.2f}%（Δ{phase:+.2f}%）")
    elif e1130 is not None:
        reasons.append(f"11:30 估算 {e1130:+.2f}%（14:55 基线未生成）")
    else:
        reasons.append(f"14:55 估算 {est:+.2f}%（无 11:30 基线）")
    return int(_clamped(base + adj, -40, 40)), reasons


def _score_breadth(feat: dict) -> tuple[int, list[str]]:
    """重仓一致性 ±15 + 集中度惩罚 -8（GPT 第十一节：7涨3跌 ≠ 5涨5跌；一票独大很危险）。"""
    if not feat or feat.get("breadth") is None:
        return 0, ["底层个股明细不可用"]
    b = feat["breadth"]
    s = b * 15.0
    conc = feat.get("concentration")
    penalty = 0
    if conc is not None and conc > 0.6:
        penalty = -8
    reasons = [f"重仓同向度 {b:+.2f}（{feat.get('n_up', '?')}涨/{feat.get('n_down', '?')}跌）"]
    if penalty:
        reasons.append(f"涨跌过度集中（最大单票贡献占比 {conc:.0%}）")
    return int(_clamped(s + penalty, -20, 20)), reasons


def _score_relative(feat: dict, pool_est: dict | None, code: str) -> tuple[int, list[str]]:
    """池内相对强度 ±15：本基金实时估算 vs 池内其他基金估算均值。

    注：GPT 建议「相对行业指数」（15%），行业指数数据源未接入；
    先用池内横截面实时估算做代理（跨基金日内相对强弱），语义接近且数据可得。
    """
    est = feat.get("est_return")
    if est is None or not pool_est or len(pool_est) < 2:
        return 0, ["池内相对基准不可用"]
    others = [v for k, v in pool_est.items() if k != code and v is not None]
    if not others:
        return 0, ["池内相对基准不可用"]
    diff = est - sum(others) / len(others)
    score = int(_clamped(math.tanh(diff / 1.5) * 15, -15, 15))
    if diff > 0.3:
        r = [f"相对池内估算强势 +{diff:.2f}%"]
    elif diff < -0.3:
        r = [f"相对池内估算弱势 {diff:+.2f}%"]
    else:
        r = [f"相对池内估算持平（{diff:+.2f}%）"]
    return score, r


def _score_technical(ts: int) -> tuple[int, list[str]]:
    """中期趋势 ±15：原三因子总分 -3~+3 线性映射（保留为解释维度，不新增信号）。"""
    s = int(_clamped(ts / 3.0 * 15, -15, 15))
    return s, [f"中期趋势三因子 {ts:+d}"]


def _score_account(acct: dict | None) -> tuple[int, list[str]]:
    """账户约束 ±10：仓位余量 / 成本保护 / 连续加仓限制（GPT 第十三节）。"""
    acct = acct or {}
    score = 0.0
    reasons: list[str] = []
    w = acct.get("current_weight", 0.0) or 0.0
    mw = acct.get("max_weight", 0.8) or 0.8
    if w > mw * 0.9:
        score -= 10
        reasons.append(f"仓位已高（{w*100:.0f}% ≥ 上限{mw*100:.0f}%×90%）")
    elif w > mw * 0.7:
        score -= 6
        reasons.append(f"仓位偏高（{w*100:.0f}%）")
    elif w < mw * 0.2:
        score += 4
        reasons.append(f"仓位较低（{w*100:.0f}%）")
    cost, nav = acct.get("cost_nav"), acct.get("last_nav")
    if cost and nav and nav < cost * 0.92:
        score -= 6
        reasons.append(f"净值低于成本 {((1 - nav / cost) * 100):.0f}%+")
    elif cost and nav and nav > cost * 1.08:
        score += 2
        reasons.append("净值高于成本 8%+")
    if (acct.get("consecutive_adds") or 0) >= 3:
        score -= 8
        reasons.append(f"已连续加仓 {acct['consecutive_adds']} 次")
    return int(_clamped(score, -10, 10)), reasons


def _check_gates(dcfg: dict, feat: dict, acct: dict | None, candidate: str) -> list[str]:
    """四道准入门槛（GPT 第十八节）→ 不满足则 invalid，动作强制 HOLD。"""
    invalid: list[str] = []
    g = dcfg.get("gates", {})
    cov = feat.get("covered_pct") or 0.0
    if cov < g.get("min_coverage", 60):
        invalid.append(f"实时覆盖率 {cov:.0f}% < {g.get('min_coverage', 60)}%")
    age = feat.get("holdings_age_days") or 0
    if age > g.get("max_snapshot_age_days", 120):
        invalid.append(f"持仓披露过旧 {age} 天 > {g.get('max_snapshot_age_days', 120)} 天")
    if candidate == "ADD":
        w = (acct or {}).get("current_weight", 0.0) or 0.0
        if w > g.get("max_position_pct", 0.8):
            invalid.append(f"当前仓位 {w*100:.0f}% ≥ 最大允许 {g.get('max_position_pct', 0.8)*100:.0f}%")
    if not g.get("history_validated", False):
        invalid.append("动作阈值未经 backtest_action.py 历史验证（history_validated=false）")
    return invalid


def evaluate(inp: DecisionInput) -> Decision:
    cfg = _load_cfg()
    dcfg = cfg.get("decision", {})
    weights = dcfg.get("weights", {
        "intraday_trend": 0.35, "breadth": 0.25, "relative_pool": 0.15,
        "mid_trend": 0.15, "account": 0.10,
    })
    feat = _merged(inp.feat_1130, inp.feat_1455)

    s1, r1 = _score_intraday(inp.feat_1130, inp.feat_1455)
    s2, r2 = _score_breadth(feat)
    s3, r3 = _score_relative(feat, inp.pool_est, inp.code)
    s4, r4 = _score_technical(inp.technical_score)
    s5, r5 = _score_account(inp.account_state)

    raw_score = (s1 * weights.get("intraday_trend", 0.35)
                 + s2 * weights.get("breadth", 0.25)
                 + s3 * weights.get("relative_pool", 0.15)
                 + s4 * weights.get("mid_trend", 0.15)
                 + s5 * weights.get("account", 0.10))

    # 归一化到 -100 ~ +100（除以理论最大加权和）
    max_possible = sum(_MAX_BY_DIM[k] * weights.get(k, 0.0) for k in _MAX_BY_DIM)
    if max_possible <= 0:
        max_possible = 1.0
    score = int(_clamped(raw_score / max_possible * 100.0, -100, 100))

    thr = dcfg.get("thresholds", {"add": 60, "reduce": -60})
    if score >= thr.get("add", 60):
        candidate = "ADD"
    elif score <= thr.get("reduce", -60):
        candidate = "REDUCE"
    else:
        candidate = "HOLD"

    invalid = _check_gates(dcfg, feat, inp.account_state, candidate)
    if invalid:
        action, confidence = "HOLD", 0.3
    elif candidate == "ADD":
        action, confidence = "ADD", _clamped((score - thr.get("add", 60)) / 40 * 0.4 + 0.6, 0.6, 1.0)
    elif candidate == "REDUCE":
        action, confidence = "REDUCE", _clamped((-score - abs(thr.get("reduce", -60))) / 40 * 0.4 + 0.6, 0.6, 1.0)
    else:
        action, confidence = "HOLD", _clamped(1.0 - abs(score) / 60, 0.4, 0.9)

    reasons = r1 + r2 + r3 + r4 + r5
    return Decision(
        action=action, candidate=candidate, score=score,
        confidence=round(confidence, 2), reasons=reasons,
        invalid_conditions=invalid,
        raw={
            "weights": weights, "s_intraday": s1, "s_breadth": s2,
            "s_relative": s3, "s_technical": s4, "s_account": s5,
            "features": feat, "est_1130": _est_of(inp.feat_1130),
            "est_1455": _est_of(inp.feat_1455),
        },
    )


def evaluate_with_forecast(inp: DecisionInput, feature_meta: dict | None = None) -> Decision:
    """v5 入口：在 evaluate() 基础上叠加多周期条件分布预测。

    - 保留旧 evaluate()（动作/倾向分）不破坏回测与既有调用
    - `inp.account_state` 不进市场预测特征（仓位不降低「上涨概率」，见 forecast_engine）
    - feature_meta: 预测器输入所需的特征字典（est_chg/breadth/composite/score...），
      回退从 inp 的 feat_* 合成
    """
    d = evaluate(inp)
    try:
        from core import forecast_engine as fe
        # 持久化闭环（2026-08-27）：进程级单例 + 自动加载 data/models 权重；
        # 无权重/版本不符 → 未训练空壳，predict 恒走占位分支。
        eng = fe.get_loaded_engine()
        features = _forecast_features(inp, feature_meta)
        freshness = (inp.feat_1455 or {}).get("holdings_freshness", 1.0)
        if inp.feat_1455 is None or not features:
            # v8（2026-08-30）：q50 回归协议恢复，占位构造同步（10 个位置参数）
            d.forecast = fe.forecast_to_cn(fe.Forecast(
                t1=fe.HorizonForecast(1, 0, 0, 0, 0, 0, 0, 0, 0, False),
                t3=fe.HorizonForecast(3, 0, 0, 0, 0, 0, 0, 0, 0, False),
                t5=fe.HorizonForecast(5, 0, 0, 0, 0, 0, 0, 0, 0, False),
                state="NO_FEATURES", path="数据不足",
                overall_confidence=0.0, meta={"model_ready": False}))
        else:
            f = eng.predict(features, freshness=freshness,
                            data_quality={"coverage": (inp.feat_1455 or {}).get("covered_pct")},
                            fund_code=inp.code)
            d.forecast = fe.forecast_to_cn(f)
    except Exception as e:  # pragma: no cover - 预测模块失败不阻断主流程
        d.forecast = {"error": str(e), "model_ready": False}
    # State Engine 只读参考（2026-08-27）：当前结构态的历史条件分布附进输出。
    # 职责边界：仅查表透出，不动任何评分/门禁/动作；失败静默不阻断主流程。
    try:
        from core import state_lookup as slk
        d.state_ref = slk.fund_state_ref(inp.code)
    except Exception:
        d.state_ref = None
    return d


def _forecast_features(inp: DecisionInput, feature_meta: dict | None) -> dict | None:
    """合成 forecast_engine.FEATURE_KEYS 所需特征。

    live 路径：feature_meta（由 run.py 传入实时特征）优先；
    否则从 inp.feat_* 字段推导（est_return→est_chg，breadth/composite/score 直接映射）。
    """
    if feature_meta:
        return feature_meta
    feat = inp.feat_1455 or {}
    if not feat:
        return None
    return {
        "est_chg": feat.get("est_return"),
        "est_sign": 1 if (feat.get("est_return") or 0) > 0 else (-1 if (feat.get("est_return") or 0) < 0 else 0),
        "breadth": feat.get("breadth"),
        "concentration": feat.get("concentration"),
        "covered_pct": feat.get("covered_pct"),
        "composite": 0,             # live 无 composite 时放 0（穿透引擎输出在别处）
        "score": inp.technical_score,
    }


def decision_to_cn(d: Decision) -> dict:
    """Decision → 中文输出字典（报告/推送用）。"""
    out = {
        "action": ACTION_CN.get(d.action, d.action),
        "candidate": ACTION_CN.get(d.candidate, d.candidate),
        "score": d.score,
        "confidence": d.confidence,
        "reasons": d.reasons,
        "invalid_conditions": d.invalid_conditions,
    }
    if d.forecast:
        out["forecast"] = d.forecast
    if getattr(d, "state_ref", None) and d.state_ref.get("horizons"):
        out["state_ref"] = d.state_ref
    return out
