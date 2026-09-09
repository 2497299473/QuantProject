"""V3 Forecast Lab 专家统一输出接口 · schema v1 —— 2026-09-09 冻结（P0-A）。

纪律（Summer 拍板 2026-09-09，写入即冻结）:
- schema_version="1" 冻结后**不得改字段语义**；扩展只能加带默认值的字段，或 bump
  版本号并同步更新本文件底部 SELFTEST 与裁决脚本。
- 每个专家 = 纯函数 (samples, ctx) -> list[ExpertOutput]。专家**禁止**读取标注日
  之后的数据；ctx.asof 用于一切外部数据截断（PIT）。
- 单位约定：expected_return/q10/q50/q90 一律为**收益率小数**（0.01 = +1%），
  与 V3 净值 fwd 标签一致；prob_up ∈ [0,1]。
- q_source 必填（v6 假分位数教训，v7 P0-5 遗留妥协的显式化）:
    normal_1.282sigma  由 E±1.282σ 正态近似合成（现有 v7 引擎口径）
    empirical          真条件分位/经验分位（v8 quantile 回归、Chronos-2 分位网格取 10/50/90）
    conformal          校准层产物（未来窗口，本阶段不产生）
- confidence ∈ [0,1]，专家自评把握度；缺失/不明口径一律 NaN，**不许填 0.5 假装中性**。
- expert 命名: existing_v7 / lgbm_a158 / chronos2 / chanlun_struct（候补，未过闸门不得注册）。

适配层：
- from_horizon_forecast(hf, *, q_source, asof)   现有 core.forecast_engine.HorizonForecast
- from_backtest_row(row, *, q_source, asof)      现有 backtest_forecast / walk-forward 预测行
  （两函数不 import 生产模块，鸭子类型接收，防环依赖；字段缺失抛 KeyError 而非静默 NaN）
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Iterable

SCHEMA_VERSION = "1"

Q_SOURCES = frozenset({"normal_1.282sigma", "empirical", "conformal"})

HORIZONS = (1, 3, 5)  # V3 现行三周期；扩展需 bump schema

_NAN = float("nan")


@dataclass(frozen=True)
class ExpertOutput:
    """单专家 × 单 horizon × 单标的 × 单决策日 的统一输出（契约见模块 docstring）。"""

    schema_version: str
    expert: str          # 注册专家名，白名单见 registry.advice.json 扩展后
    fund: str            # 六位代码
    asof: str            # 决策日 YYYY-MM-DD（PIT：一切信息 ≤ 当日 14:55 可得口径）
    horizon: int         # ∈ HORIZONS
    expected_return: float | None   # E[r]，小数；无值=None（不是 0）
    prob_up: float | None
    q10: float | None
    q50: float | None
    q90: float | None
    q_source: str
    confidence: float
    meta: dict = field(default_factory=dict)  # 模型版本/特征协议/耗时等自由键（进 registry 留档）

    def __post_init__(self):
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"schema_version 必须为 {SCHEMA_VERSION!r}，收到 {self.schema_version!r}")
        if not self.expert:
            raise ValueError("expert 不得为空")
        if self.horizon not in HORIZONS:
            raise ValueError(f"horizon={self.horizon} ∉ {HORIZONS}")
        if self.q_source not in Q_SOURCES:
            raise ValueError(f"q_source={self.q_source!r} ∉ {sorted(Q_SOURCES)}")
        if self.asof is None or len(str(self.asof)) != 10:
            raise ValueError(f"asof 需为 YYYY-MM-DD，收到 {self.asof!r}")
        p = self.prob_up
        if p is not None and not (0.0 <= p <= 1.0):
            raise ValueError(f"prob_up={p} 越界 [0,1]")
        c = self.confidence
        if not (math.isnan(c) or 0.0 <= c <= 1.0):
            raise ValueError(f"confidence={c} 越界 [0,1]（缺失用 NaN）")
        # 分位数单调（对非 None 值校验；与 v8 ReturnQuantileModel 的排序兜底同向）
        qs = [q for q in (self.q10, self.q50, self.q90)
              if q is not None and math.isfinite(q)]   # NaN=某位不可用，按缺失处理（NaN 会让 sorted 比较恒不等）
        if len(qs) >= 2 and qs != sorted(qs):
            raise ValueError(f"分位数交叉 q10={self.q10} q50={self.q50} q90={self.q90}")

    def to_dict(self) -> dict:
        return asdict(self)


def from_horizon_forecast(hf, *, expert: str, fund: str, asof: str,
                          q_source: str = "normal_1.282sigma") -> ExpertOutput:
    """适配现有 Forecast.horizons 元素（HorizonForecast dataclass）。

    映射：p_up→prob_up、e_return→expected_return、confidence 原样。
    注意：v7 引擎 q10/q90 是 ±1.282σ 正态近似、q50=E[r]，默认 q_source 如实标。
    """
    return ExpertOutput(
        schema_version=SCHEMA_VERSION, expert=expert, fund=fund, asof=asof,
        horizon=int(hf.horizon),
        expected_return=None if hf.e_return is None else float(hf.e_return),
        prob_up=None if hf.p_up is None else float(hf.p_up),
        q10=None if hf.q10 is None else float(hf.q10),
        q50=None if hf.q50 is None else float(hf.q50),
        q90=None if hf.q90 is None else float(hf.q90),
        q_source=q_source,
        confidence=float(_NAN) if hf.confidence is None else float(hf.confidence),
        meta={"model_ready": getattr(hf, "model_ready", None)},
    )


def from_backtest_row(row, *, expert: str, fund: str, asof: str, horizon: int,
                      q_source: str = "empirical") -> ExpertOutput:
    """适配 backtest_forecast / walk-forward 的单行预测（p_up/yhat 口径）。

    row 需含 "p_up"；可选 "e_return"（E[r]）。回测行通常无真分位输出 →
    q10/q50/q90 = None（缺失 ≠ 0），裁决只用 prob_up/expected_return。
    """
    return ExpertOutput(
        schema_version=SCHEMA_VERSION, expert=expert, fund=fund, asof=asof,
        horizon=int(horizon),
        expected_return=None if row.get("e_return") is None else float(row["e_return"]),
        prob_up=None if row.get("p_up") is None else float(row["p_up"]),
        q10=None, q50=None, q90=None,
        q_source=q_source,
        confidence=_NAN,
        meta={"from": "backtest_row"},
    )


# ------------------------------------------------------------------ 自检
def _selftest() -> int:
    ok = 0
    fail = 0

    def check(name: str, cond: bool):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  [FAIL] {name}")

    good = dict(expert="selftest", fund="002112", asof="2026-09-09", horizon=5,
                expected_return=0.002, prob_up=0.55, q10=-0.01, q50=0.002, q90=0.015,
                q_source="empirical", confidence=0.7)
    check("构造合法", True if ExpertOutput(SCHEMA_VERSION, **good) else False)
    check("horizon 越界拒绝", _raises(ValueError, lambda: ExpertOutput(
        SCHEMA_VERSION, **{**good, "horizon": 10})))
    check("q_source 非法拒绝", _raises(ValueError, lambda: ExpertOutput(
        SCHEMA_VERSION, **{**good, "q_source": "guess"})))
    check("prob_up 越界拒绝", _raises(ValueError, lambda: ExpertOutput(
        SCHEMA_VERSION, **{**good, "prob_up": 1.2})))
    check("分位交叉拒绝", _raises(ValueError, lambda: ExpertOutput(
        SCHEMA_VERSION, **{**good, "q90": -0.02})))
    check("confidence 可用 NaN（原样保留不拒绝）", math.isnan(ExpertOutput(
        SCHEMA_VERSION, **{**good, "confidence": _NAN}).confidence))
    check("confidence 越界拒绝", _raises(ValueError, lambda: ExpertOutput(
        SCHEMA_VERSION, **{**good, "confidence": 1.5})))
    check("q 含 NaN 不误判交叉", not _raises(ValueError, lambda: ExpertOutput(
        SCHEMA_VERSION, **{**good, "q10": _NAN})))
    check("None 期望收益合法", ExpertOutput(
        SCHEMA_VERSION, **{**good, "expected_return": None}).expected_return is None)

    class _HF:  # 鸭子类型 HorizonForecast
        horizon, p_up, e_return, q10, q50, q90, confidence, model_ready = (
            5, 0.6, 0.001, -0.01, 0.001, 0.02, 0.8, False)

    o = from_horizon_forecast(_HF(), expert="existing_v7", fund="002112", asof="2026-09-09")
    check("v7 适配映射", o.prob_up == 0.6 and o.expected_return == 0.001
          and o.q_source == "normal_1.282sigma")

    o2 = from_backtest_row({"p_up": 0.51}, expert="existing_v7", fund="002112",
                           asof="2026-09-09", horizon=5)
    check("回测行适配 q=None", o2.q10 is None and o2.q50 is None and o2.q90 is None)
    # to_dict 往返不能用 dataclass `==`：confidence=NaN 在场时，py3.12 的元组比较走
    # 同对象身份短路（nan==nan 判真），py3.14 改为逐字段严格 ==（nan!=nan 判假）。
    # 2026-09-09 实测两 venv（3.12.13 / 3.14.6）该检查结论随解释器版本翻转——测试
    # 语义不得依赖解释器版本，故改 NaN 容差逐字段比较（schema 冻结不受影响，仅动自检）。
    from dataclasses import fields as _fields
    o3 = ExpertOutput(**o2.to_dict())

    def _nan_eq(a, b):
        return a == b or (isinstance(a, float) and isinstance(b, float)
                          and math.isnan(a) and math.isnan(b))
    check("to_dict 往返", o3.__class__ is o2.__class__
          and all(_nan_eq(getattr(o2, f.name), getattr(o3, f.name))
                  for f in _fields(ExpertOutput)))

    print(f"[expert_protocol SELFTEST] {ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


def _raises(exc, fn):
    try:
        fn()
        return False
    except exc:
        return True
    except Exception:
        return False


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())


# ==================================================================
# P0 合并扩展（2026-09-09 11:05，并入自并行实现，见报告 §七；只增不改上面已冻结的 schema）
# 背景：同一分钟内另一会话独立写了 core/expert_protocol.py（六值 q_source + 自带 ensemble）。
# 冲突裁决：本文件为权威 schema（Summer 拍板「写入即冻结」在前）；以下只合并其独有增量：
#   from_forecast_triple  一次转 t1/t3/t5 三条（适配 core.forecast_engine.Forecast）
#   ensemble              V1 等权/显式加权融合（分歧只降 confidence，绝不上调）
#   write_records/load_records  jsonl 追加入库往返
# 被废弃的重复实现已删除（core/expert_protocol.py 等，见 §七）。
# ==================================================================
import json as _json
from pathlib import Path as _Path


def from_forecast_triple(fc, *, expert: str, fund: str, asof: str,
                         q_source: str = "empirical",
                         meta: dict | None = None) -> list[ExpertOutput]:
    """把 v8 引擎 Forecast（t1/t3/t5）一次转成三条协议记录。

    字段映射（forecast_engine.py L74-97 已核实）：p_up→prob_up、
    e_return→expected_return、q10/q50/q90 直取、confidence 原样、
    model_ready 进 meta。q_source 默认 "empirical"（v8 起为真 quantile loss
    HGBR；若未来回退正态近似必须显式改 "normal_1.282sigma"，不许沉默）。
    """
    rows = []
    for h, hf in ((1, fc.t1), (3, fc.t3), (5, fc.t5)):
        m = dict(meta or {})
        m.update({"model_ready": getattr(hf, "model_ready", None),
                  "horizon_attr": getattr(hf, "horizon", None)})
        rows.append(ExpertOutput(
            schema_version=SCHEMA_VERSION, expert=expert, fund=fund,
            asof=asof, horizon=h,
            expected_return=None if hf.e_return is None else float(hf.e_return),
            prob_up=None if hf.p_up is None else float(hf.p_up),
            q10=None if hf.q10 is None else float(hf.q10),
            q50=None if hf.q50 is None else float(hf.q50),
            q90=None if hf.q90 is None else float(hf.q90),
            q_source=q_source,
            confidence=_NAN if hf.confidence is None else float(hf.confidence),
            meta=m))
    return rows


def _avg_finite(outputs, attr, ws):
    """跳过 None/NaN 的加权均值（该字段的权重重新归一）；全缺 → None。"""
    items = []
    for o, w in zip(outputs, ws):
        v = getattr(o, attr)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        items.append((float(v), w))
    if not items:
        return None
    tot = sum(w for _, w in items)
    return sum(v * w for v, w in items) / tot


def ensemble(outputs, weights=None, dispersion_penalty: float = 0.5) -> dict:
    """V1 等权融合（评估报告 §二步骤4）。返回 dict（可 json 入库，不强制是 ExpertOutput）。

    规则：
    - weights=None → 等权；显式权重必须和为 1（±1e-9），不隐式归一。
    - 成员必须同 (fund, asof, horizon) 且专家名唯一（同专家先自行聚合）。
    - expected_return/prob_up/q10/q50/q90：对可用值（跳 None/NaN）加权均值。
    - confidence = 加权均值 × 分歧惩罚；惩罚 ≤1，**只降不升**。
      dispersion = q50 极差 / 平均预测宽度(q90-q10)，尺度无关；
      penalty = 1/(1 + dispersion_penalty × dispersion)。宽度全缺时 dispersion=0。
    """
    if not outputs:
        raise ValueError("ensemble: 至少 1 条记录")
    n = len(outputs)
    names = [o.expert for o in outputs]
    if len(set(names)) != n:
        raise ValueError(f"ensemble: 专家名重复 {names}（同专家须先自行聚合）")
    if len({(o.fund, o.asof, o.horizon) for o in outputs}) != 1:
        raise ValueError("ensemble: 成员必须同 fund / asof / horizon")
    if weights is None:
        ws = [1.0 / n] * n
    else:
        if len(weights) != n:
            raise ValueError("ensemble: weights 数量不匹配")
        if abs(sum(weights) - 1.0) > 1e-9:
            raise ValueError(f"ensemble: weights 须显式归一（当前和 {sum(weights)}）")
        ws = [float(w) for w in weights]

    q50s = [o.q50 for o in outputs
            if o.q50 is not None and math.isfinite(o.q50)]
    widths = [(o.q90 - o.q10) for o in outputs
              if o.q10 is not None and o.q90 is not None
              and math.isfinite(o.q10) and math.isfinite(o.q90)]
    mean_width = (sum(widths) / len(widths)) if widths else 0.0
    dispersion = 0.0
    if len(q50s) >= 2 and mean_width > 1e-12:
        dispersion = (max(q50s) - min(q50s)) / mean_width
    penalty = 1.0 / (1.0 + max(0.0, dispersion_penalty) * dispersion)
    conf = _avg_finite(outputs, "confidence", ws)
    if conf is not None:
        conf = conf * penalty  # 只降不升（penalty≤1）
    return {
        "kind": "ensemble",
        "schema_version": SCHEMA_VERSION,
        "fund": outputs[0].fund,
        "asof": outputs[0].asof,
        "horizon": outputs[0].horizon,
        "expected_return": _avg_finite(outputs, "expected_return", ws),
        "prob_up": _avg_finite(outputs, "prob_up", ws),
        "q10": _avg_finite(outputs, "q10", ws),
        "q50": _avg_finite(outputs, "q50", ws),
        "q90": _avg_finite(outputs, "q90", ws),
        "confidence": conf,
        "q_source_set": sorted({o.q_source for o in outputs}),
        "dispersion": dispersion,
        "confidence_penalty": penalty,
        "weights": ws,
        "members": names,
    }


def write_records(path, outputs, append: bool = True) -> int:
    """jsonl 追加/覆写；元素可是 ExpertOutput 或 dict（如 ensemble 结果）。"""
    p = _Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    cnt = 0
    with open(p, "a" if append else "w", encoding="utf-8") as fh:
        for o in outputs:
            d = o.to_dict() if isinstance(o, ExpertOutput) else dict(o)
            fh.write(_json.dumps(d, ensure_ascii=False, sort_keys=True) + "\n")
            cnt += 1
    return cnt


def load_records(path):
    """jsonl 读回；只返回能构造成 ExpertOutput 的行（ensemble dict 等非记录行跳过）。"""
    out = []
    for line in _Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(ExpertOutput(**_json.loads(line)))
        except (TypeError, ValueError):
            continue
    return out
