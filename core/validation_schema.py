"""Validation Evidence Schema v2（B++-1，2026-09-23）——晋升证据形态的唯一权威。

为什么存在
==========
契约 B（evidence/contracts/forecast_promotion_contract_B_20260923.md）§13 只给了
JSON 草图。本模块把它钉成代码常量 + 纯函数校验器，供 B++-2（验证器产出 v2）、
B++-3（derive_promotion v2）共同引用——原则是「新增契约字段，而不是新增第二套
事实源」。

钉死四件事
==========
1) 证据块结构：顶层九键（schema_version/decision/protocol/pooled/funds/power/
   calibration/baseline/provenance）；pooled 与 funds 同构的 horizon 节点
   （周期 1/3/5，JSON canonical 键 = 字符串）；funds 键集 = 契约 B §2 四基金
   精确冻结（缺一、多一都非法——§7/F3「禁止只存部分基金证据」的机器化）。
2) 三态不可计算与真实数值正式分离（契约 B §13 尾注；B++-5 的落点）：
   每个 metric 槽位一律 {value, status} 双键——
   OK                 有值且可复核（value = 有限数值 / CI 对 / bool / 非空文本）；
   UNKNOWN            还没跑/还没填——「不知道」；
   INSUFFICIENT_POWER 样本不足（n < 功效阈值）——「现在判不了」（如 025687 n=35）；
   NOT_COMPUTABLE     定义上算不出来（缺输入/除零/NaN）——「算不出」。
   非 OK 态 value 一律 null；OK 态禁 null/NaN/Inf/bool 冒充数值——
   从结构上禁止「样本不足 / 不可计算 / 真实 0 分」三义混装成 0.0。
3) ACE 命名冻结（GPT 四审 P1）：现有 backtest_forecast.calibration_curve 实为
   「桶中点加权校准误差」（Σ|桶中点−实际频率|×桶样本数/N，等宽 5 桶，末桶含
   右端点），schema v2 字段名 midpoint_calibration_error；旧名 'ace' 禁入键集，
   不得继续泛称 ACE。
4) 基线钉死（契约 B §3/C3/C4）：baseline = {name: est_chg, unit: fraction}，
   禁止 promotion 运行后临时替换基线或量纲。

边界（本批不动什么）
==========
纯 schema 层：不含晋升裁决（B++-3 derive_promotion v2 另批）、不改验证器产出
（B++-2）、不改模型/门禁/registry/bind_validation/verify_approval。
零网络、零 I/O、纯函数；同输入必同输出（幂等）。
"""
from __future__ import annotations

import math

VALIDATION_SCHEMA_VERSION = 2

# ---- 证据值状态（契约 B §13 尾注：null 不是「通过」，三态不得混装）----
STATUS_OK = "OK"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_INSUFFICIENT_POWER = "INSUFFICIENT_POWER"
STATUS_NOT_COMPUTABLE = "NOT_COMPUTABLE"
ALL_EVIDENCE_STATUSES = (STATUS_OK, STATUS_UNKNOWN,
                         STATUS_INSUFFICIENT_POWER, STATUS_NOT_COMPUTABLE)

# ---- 契约 B §2 生产目标基金（F1..F4）——首次钉成代码常量（此前只在契约文本）----
PRODUCTION_FUNDS = ("002112", "002207", "022853", "025687")

# ---- 契约 B §5 主晋升周期（canonical JSON 键 = 字符串）----
HORIZONS = ("1", "3", "5")

# ---- 契约 B §3 唯一机械基线 ----
BASELINE_NAME = "est_chg"
BASELINE_UNIT = "fraction"

# ---- ACE 命名冻结（GPT 四审 P1）----
MIDPOINT_CALIBRATION_ERROR_KEY = "midpoint_calibration_error"
MIDPOINT_CALIBRATION_ERROR_DEF = (
    "midpoint_calibration_error = Σ_桶 |桶中点 − 实际频率| × 桶样本数 / 总样本数；"
    "等宽 5 桶（0~1），末桶含右端点；与 backtest_forecast.calibration_curve 返回的"
    " 'ace' 同一公式（2026-09-23 B++-1 冻结命名；'ace' 为旧产出字段名，schema v2 禁用）")

TOP_LEVEL_KEYS = ("schema_version", "decision", "protocol", "pooled", "funds",
                  "power", "calibration", "baseline", "provenance")

# pooled / fund × horizon 节点的冻结键集（契约 B §5：RankIC、Brier、ACE、
# decision_edge、多数类比较（b_majority）、est_chg 比较（base_ic））
METRIC_KEYS = ("n", "rank_ic", "rank_ic_ci", "base_ic", "decision_edge",
               "decision_edge_ci", "brier", "brier_ci", "b_majority",
               MIDPOINT_CALIBRATION_ERROR_KEY)
CI_KEYS = ("rank_ic_ci", "decision_edge_ci", "brier_ci")

# power 块（契约 B §6.2：功效阈值留白待预注册冻结，frozen=False 是可计算事实）
POWER_KINDS = {"n_power_fund": "scalar", "n_power_horizon": "scalar",
               "method": "text", "effect_size_target": "scalar",
               "confidence_level": "scalar", "target_power": "scalar",
               "cluster_structure": "text", "frozen": "bool"}

# calibration 块（契约 B §9：Quantile Q10/Q50/Q90 Coverage / Pinball / CQR coverage；
# 方向概率的校准误差在 horizon 节点内，此处只放 quantile 家族）
QUANTILES = ("q10", "q50", "q90")
QUANTILE_KEYS = ("coverage", "pinball", "cqr_coverage")

# provenance 块（契约 B §10：冻结件可溯源；均以非空文本入档）
PROVENANCE_KEYS = ("frozen_dataset", "dataset_sha256", "git_commit",
                   "feature_protocol", "contract_version",
                   "historical_feature_mode", "produced_by", "produced_at")


# ---------- 证据值构造器（产出方 B++-2 使用） ----------
def ev_ok(value):
    """有值且可复核。"""
    return {"value": value, "status": STATUS_OK}


def ev_unknown():
    """还没跑/还没填。"""
    return {"value": None, "status": STATUS_UNKNOWN}


def ev_na(status: str):
    """不可计算三态（UNKNOWN 之外）；非 OK 态 value 一律 null。"""
    if status not in ALL_EVIDENCE_STATUSES or status == STATUS_OK:
        raise ValueError(f"ev_na 只接受非 OK 证据状态，得到 {status!r}")
    return {"value": None, "status": status}


def canonical_horizon(h) -> str | None:
    """周期键规范化：1 / '1' → '1'；其余（'01'、1.0、'7'、True…）一律 None。"""
    s = str(h)
    return s if s in HORIZONS else None


# ---------- 模板（全 UNKNOWN，供 B++-2 从零填充） ----------
def metric_node() -> dict:
    """单 horizon 证据节点（pooled 与 fund 共用同构；键集冻结）。"""
    return {k: ev_unknown() for k in METRIC_KEYS}


def blank_evidence() -> dict:
    """schema v2 空模板：结构合法、全部槽位 UNKNOWN、power.frozen=False。

    「全 UNKNOWN 合法」是有意设计：schema 层描述证据形态，不裁决好坏——
    同一形态既可承载全过的证据，也可承载全不可算的证据。
    """
    return {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "decision": None,
        "protocol": {"version": None, "feature_dim": None},
        "pooled": {h: metric_node() for h in HORIZONS},
        "funds": {f: {h: metric_node() for h in HORIZONS} for f in PRODUCTION_FUNDS},
        "power": {k: (ev_ok(False) if k == "frozen" else ev_unknown())
                  for k in POWER_KINDS},
        "calibration": {q: {k: ev_unknown() for k in QUANTILE_KEYS}
                        for q in QUANTILES},
        "baseline": {"name": BASELINE_NAME, "unit": BASELINE_UNIT},
        "provenance": {k: ev_unknown() for k in PROVENANCE_KEYS},
    }


# ---------- 校验器（fail-closed：未知键、缺键、坏配对一律报错） ----------
def _finite_number(x) -> bool:
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(float(x)))


def _check_ev(node, path: str, errors: list, kind: str = "scalar") -> None:
    """单槽位 {value, status} 配对纪律。kind: scalar|pair|bool|text。"""
    if not (isinstance(node, dict) and set(node.keys()) == {"value", "status"}):
        errors.append(f"{path}: 须为 {{'value','status'}} 双键节点")
        return
    status, value = node["status"], node["value"]
    if status not in ALL_EVIDENCE_STATUSES:
        errors.append(f"{path}: 非法 status={status!r}"
                      f"（仅允许 {'/'.join(ALL_EVIDENCE_STATUSES)}）")
        return
    if status != STATUS_OK:
        if value is not None:
            errors.append(f"{path}: status={status} 时 value 必须为 null"
                          f"（非 OK 态禁带数值，防与真实 0 分混装）")
        return
    if value is None:
        errors.append(f"{path}: status=OK 但 value=null（OK 必须携带可复核值）")
        return
    if kind == "pair":
        if not (isinstance(value, (list, tuple)) and len(value) == 2):
            errors.append(f"{path}: CI 节点 OK 态 value 须为 [lo, hi] 双元素列表")
            return
        for i, x in enumerate(value):
            if not _finite_number(x):
                errors.append(f"{path}: CI[{i}] 非有限数值：{x!r}")
        return
    if kind == "bool":
        if not isinstance(value, bool):
            errors.append(f"{path}: OK 态 value 须为 bool")
        return
    if kind == "text":
        if not (isinstance(value, str) and value.strip()):
            errors.append(f"{path}: OK 态 value 须为非空字符串")
        return
    if not _finite_number(value):
        errors.append(f"{path}: OK 态 value 须为有限数值"
                      f"（不得为 bool/str/None/NaN/Inf）")


def _check_flat(path: str, node, spec: dict, errors: list) -> None:
    """键集精确冻结的扁平节点（power / quantile / provenance 共用）。"""
    if not isinstance(node, dict):
        errors.append(f"{path}: 须为 dict")
        return
    for k in node:
        if k not in spec:
            errors.append(f"{path}: 未知键 {k!r}（键集冻结）")
    for k, kind in spec.items():
        if k not in node:
            errors.append(f"{path}: 缺少键 {k}")
            continue
        _check_ev(node[k], f"{path}.{k}", errors, kind=kind)


def _check_metric_node(path: str, node, errors: list) -> None:
    if not isinstance(node, dict):
        errors.append(f"{path}: 须为 dict")
        return
    for k in node:
        if k not in METRIC_KEYS:
            errors.append(f"{path}: 未知指标键 {k!r}（schema v2 键集冻结，"
                          f"'ace' 等旧名禁用，改用 {MIDPOINT_CALIBRATION_ERROR_KEY}）")
    for k in METRIC_KEYS:
        if k not in node:
            errors.append(f"{path}: 缺少指标键 {k}")
            continue
        _check_ev(node[k], f"{path}.{k}", errors,
                  kind="pair" if k in CI_KEYS else "scalar")


def _check_horizon_group(path: str, group, errors: list) -> None:
    if not isinstance(group, dict):
        errors.append(f"{path}: 须为 dict")
        return
    canon: dict = {}
    for k in group:
        c = canonical_horizon(k)
        if c is None:
            errors.append(f"{path}: 非法周期键 {k!r}（仅允许 1/3/5）")
        elif c in canon:
            errors.append(f"{path}: 周期键重复（{k!r} 与既有键同义）")
        else:
            canon[c] = group[k]
    for h in HORIZONS:
        if h not in canon:
            errors.append(f"{path}: 缺少周期 {h}")
    for h in HORIZONS:
        if h in canon:
            _check_metric_node(f"{path}.{h}", canon[h], errors)


def _check_funds(funds, errors: list) -> None:
    if not isinstance(funds, dict):
        errors.append("funds: 须为 dict")
        return
    for f in funds:
        if f not in PRODUCTION_FUNDS:
            errors.append(f"funds: 未知基金 {f!r}（生产目标集 = 契约 B §2 四基金，多一即非法）")
    for f in PRODUCTION_FUNDS:
        if f not in funds:
            errors.append(f"funds: 缺少生产基金 {f}（契约 B §7/F3 禁止只存部分基金证据）")
    for f in PRODUCTION_FUNDS:
        if f in funds:
            _check_horizon_group(f"funds.{f}", funds[f], errors)


def _check_power(power, errors: list) -> None:
    _check_flat("power", power, POWER_KINDS, errors)


def _check_calibration(calib, errors: list) -> None:
    if not isinstance(calib, dict):
        errors.append("calibration: 须为 dict")
        return
    for q in calib:
        if q not in QUANTILES:
            errors.append(f"calibration: 未知分位键 {q!r}（仅允许 q10/q50/q90）")
    for q in QUANTILES:
        if q not in calib:
            errors.append(f"calibration: 缺少分位 {q}")
            continue
        _check_flat(f"calibration.{q}", calib[q],
                    {k: "scalar" for k in QUANTILE_KEYS}, errors)


def _check_baseline(baseline, errors: list) -> None:
    if not isinstance(baseline, dict):
        errors.append("baseline: 须为 dict")
        return
    if set(baseline.keys()) != {"name", "unit"}:
        errors.append("baseline: 键集须恰为 {name, unit}")
        return
    if baseline["name"] != BASELINE_NAME:
        errors.append(f"baseline.name: 须为 {BASELINE_NAME!r}（契约 B §3.1 禁止临时换基线）")
    if baseline["unit"] != BASELINE_UNIT:
        errors.append(f"baseline.unit: 须为 {BASELINE_UNIT!r}（C4 量纲纪律）")


def _check_provenance(prov, errors: list) -> None:
    _check_flat("provenance", prov, {k: "text" for k in PROVENANCE_KEYS}, errors)


def _check_protocol(protocol, errors: list) -> None:
    if not isinstance(protocol, dict):
        errors.append("protocol: 须为 dict")
        return
    allowed = {"version", "feature_dim", "feature_keys", "masking"}
    for k in protocol:
        if k not in allowed:
            errors.append(f"protocol: 未知键 {k!r}")
    for k in ("version", "feature_dim"):
        if k not in protocol:
            errors.append(f"protocol: 缺少键 {k}")
            continue
        v = protocol[k]
        if v is None:
            continue  # 未填（UNKNOWN 语义在结构层的等价表达，B++-2 落值）
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            errors.append(f"protocol.{k}: 须为正整数或 null")
    fk = protocol.get("feature_keys")
    if fk is not None and not (isinstance(fk, list)
                               and all(isinstance(x, str) for x in fk)):
        errors.append("protocol.feature_keys: 须为字符串列表或 null")
    m = protocol.get("masking")
    if m is not None and not isinstance(m, str):
        errors.append("protocol.masking: 须为字符串或 null")


def validate_evidence(evidence) -> tuple[bool, list[str]]:
    """schema v2 校验（纯函数、fail-closed、不推导 decision）。

    返回 (是否合法, 错误列表)。裁决权在 B++-3 derive_promotion v2——
    本函数只保证「能进晋升引擎的证据形态是完备且语义不混装的」。
    """
    errors: list[str] = []
    if not isinstance(evidence, dict):
        return False, ["evidence 必须为 dict"]
    for k in TOP_LEVEL_KEYS:
        if k not in evidence:
            errors.append(f"缺少顶层键 {k}")
    for k in evidence:
        if k not in TOP_LEVEL_KEYS:
            errors.append(f"未知顶层键 {k!r}（schema v2 顶层键集冻结）")
    if errors:
        return False, errors
    if evidence["schema_version"] != VALIDATION_SCHEMA_VERSION:
        errors.append(f"schema_version 须为 {VALIDATION_SCHEMA_VERSION}，"
                      f"得到 {evidence['schema_version']!r}")
    if evidence["decision"] not in (None, "approved", "rejected"):
        errors.append(f"decision 非法：{evidence['decision']!r}"
                      f"（仅允许 null/approved/rejected）")
    _check_protocol(evidence["protocol"], errors)
    _check_horizon_group("pooled", evidence["pooled"], errors)
    _check_funds(evidence["funds"], errors)
    _check_power(evidence["power"], errors)
    _check_calibration(evidence["calibration"], errors)
    _check_baseline(evidence["baseline"], errors)
    _check_provenance(evidence["provenance"], errors)
    return (not errors), errors
