#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D3-01 ②（2026-09-29 面 3 审查）：validation evidence 的**独立复算门**。

为什么存在（A 单面 3 核心结论）
==============================
``bind_validation_evidence`` 全链校验的都是 **Identity proof**（这份证据声称属于
这个模型 / 这份数据 / 这个 commit），**Computation proof 为零**——没有任何机制
证明 evidence 里的数值是真算出来的。手工构造「身份正确、数值全绿」的 report +
evidence 即可让 ``derive_promotion`` 五门全过推出 ``approved``。

本模块补上 Computation proof：对 **frozen 件 + registry artifact 字节**独立重算
pooled 指标（含 CI），与 evidence 声明值逐位比对；不符 ⇒ 拒绑。

为什么必须连 CI 一起复算（A 单只点名 rank_ic/decision_edge，此处如实扩展）
==========================================================================
晋升五门的**实际判据是 CI 下界**，不是点估计：
  - 门 2 performance：``pooled[h].rank_ic_ci.value[0] <= 0`` ⇒ blocked_performance
  - 门 3 baseline_edge：``decision_edge_ci.value[0] <= 0`` ⇒ blocked_baseline_edge
只复算点估计的话，伪造者把 CI 下界填成正数即可过门——复算门形同虚设。
故 n / rank_ic / rank_ic_ci / base_ic / decision_edge / decision_edge_ci / brier /
brier_ci / b_majority / midpoint_calibration_error **全部**复算比对。

纪律（对齐 AGENTS.md 八荣八耻）
==============================
- **零新统计实现**（第 4 条「复用存量」）：全部复用 backtest_forecast 既有纯函数
  （split_date_oos / build_xy / rank_ic / cluster_bootstrap_ci / calibration_curve /
  brier_multiclass / decision_edge_metrics），口径与验证器逐字一致，不另起炉灶。
- **零网络**（铁律 7/8）：只读 forecast_outputs/ 冻结件与 data/models/ artifact 字节。
- **不碰裁决**：本模块只回答「这些数值能不能从冻结件 + 模型字节重算出来」，
  不判 promotion 好坏（那是 derive_promotion 五门的职权）。
- **不可算如实标注**（第 7 条）：算不出的槽位为 None，不造 0、不猜测。

用法（库；生产入口由 bind_validation_evidence.py 接线）::

    from evidence_recompute import recompute_and_check
    ok, mismatches, recomputed = recompute_and_check(
        evidence, base_dir=BASE_DIR, horizons=(1, 3, 5), flat_margin=0.003)
"""
from __future__ import annotations

import json
import pickle
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# 复算覆盖的 pooled 槽位（与 validation_schema.METRIC_KEYS 对齐；CI 槽位单独列，
# 因为它们是 [lo, hi] 对而非标量）。
SCALAR_SLOTS = ("n", "rank_ic", "base_ic", "decision_edge", "brier",
                "b_majority", "midpoint_calibration_error")
CI_SLOTS = ("rank_ic_ci", "decision_edge_ci", "brier_ci")

# 各槽位的 round 口径——必须与验证器落 evidence 时的口径逐位一致，否则复算值与
# 声明值会因舍入位数不同而假失配。来源（已核实，不凭印象）：
#   backtest_forecast.main()  results[h] 里 rank_ic/base_ic/oos_brier/b_majority/ace
#                             均 round(…, 3)；decision_edge 由 decision_edge_metrics
#                             返回 round(…, 4)
#   pooled_node_from_results  直接搬运上述已 round 的值（不二次舍入）
ROUND_DIGITS = {"rank_ic": 3, "base_ic": 3, "brier": 3, "b_majority": 3,
                "midpoint_calibration_error": 3, "decision_edge": 4}
CI_ROUND_DIGITS = 3          # _ev_ci：round(float(ci[i]), 3)
EDGE_CI_ROUND_DIGITS = 4     # decision_edge_metrics：round(ci[i], 4)


def _round(v, digits):
    return None if v is None else round(float(v), digits)


def load_frozen_samples(dataset_sha256: str, frozen_name: str,
                        base_dir: Path = BASE_DIR) -> tuple[list | None, str | None]:
    """定位并**独立校验** frozen 件（复算的输入必须是 evidence 声称的那份数据）。

    返回 (samples, None) 或 (None, 原因)。校验三重：
      ① 文件存在且可解析（forecast_outputs/<frozen_name>）；
      ② 走 frozen_dataset.resolve_samples 的 G-A 硬闸门（sha/行数/降级/个股失败）
         ——不复用第二套判据，与验证器同义；
      ③ G-A 实算的 samples_sha256_lf 必须等于 evidence 声称的 dataset_sha256
         （否则复算的是另一份数据，比对无意义）。

    刻意**不**因 G-B DRIFTED/INCOMPLETE 拒绝：G-B 是「与历史轮次可比性」的软标记，
    不影响「这份冻结件能不能重算出这些数值」。可比性门由 derive_promotion 门 5
    （kfp_comparability 必须 SAME）与 verify_approval 各自把守，此处不越权。
    """
    import frozen_dataset as fd

    if not frozen_name or not dataset_sha256:
        return None, "evidence 未声明 frozen_dataset / dataset_sha256，无法定位复算输入"
    snap = base_dir / fd.SNAPSHOT_DIRNAME / frozen_name
    if not snap.is_file():
        # 兼容调用方给相对/绝对路径的情形
        alt = Path(frozen_name)
        snap = alt if alt.is_file() else snap
    if not snap.is_file():
        return None, f"冻结件不存在：{frozen_name}"
    samples, info = fd.resolve_samples(str(snap), False, base_dir, live_loader=None)
    if info["mode"] != "FROZEN":
        return None, (f"冻结件未过 G-A 硬闸门（mode={info['mode']}）——"
                      "复算输入不可信，拒绝复算")
    actual_sha = str((info.get("snapshot_provenance") or {}).get("samples_sha256_lf") or "")
    if actual_sha != str(dataset_sha256).strip():
        return None, (f"冻结件 sha 与 evidence 声明不符（实算 {actual_sha[:12]}… / "
                      f"声明 {str(dataset_sha256)[:12]}…）——复算的不是同一份数据")
    return samples, None


def load_artifact_models(artifact_sha256: str, horizons, flat_margin: float,
                         base_dir: Path = BASE_DIR) -> tuple[dict | None, str | None]:
    """加载 registry artifact 权重字节（复算的另一半输入）。

    返回 (models, None) 或 (None, 原因)。**先校 registry sha 再反序列化**（与
    backtest_forecast.load_persisted_direction_artifact 同纪律：pickle 反序列化前
    必须过原始字节 SHA 校验），并校验 payload 的 model_version / feature_keys /
    horizons / flat_margin 与当前生产口径一致——口径不一致时复算数字无意义。

    刻意不要求 registry 条目带 snapshot_provenance：复算门的 dataset 身份来自
    **evidence 声明 + 冻结件实算 sha 双向核对**（load_frozen_samples ③），不依赖
    registry 侧冻结件字段（历史条目无该字段，见 D3-01 影响面声明）。
    """
    import sys
    if str(base_dir) not in sys.path:
        sys.path.insert(0, str(base_dir))
    from core import forecast_engine, model_registry

    want_sha = str(artifact_sha256 or "").strip().lower()
    if not want_sha:
        return None, "evidence 未声明 artifact_sha256，无法定位复算模型"
    entry_name = None
    for name, entry in (model_registry.load_registry().get("models") or {}).items():
        if str((entry or {}).get("sha256") or "").strip().lower() == want_sha:
            entry_name = name
            break
    if entry_name is None:
        return None, f"registry 中找不到 sha256={want_sha[:12]}… 的 artifact 条目"
    path = model_registry.PKL_DIR / entry_name
    raw, reason = model_registry.read_verified_model_bytes(path)
    if raw is None:
        return None, f"artifact 字节校验失败：{reason}"
    try:
        payload = pickle.loads(raw)
    except Exception as exc:                                  # noqa: BLE001
        return None, f"artifact 反序列化失败：{type(exc).__name__}"
    if not isinstance(payload, dict):
        return None, "artifact payload 非 dict"
    if payload.get("model_version") != forecast_engine.MODEL_VERSION:
        return None, ("artifact model_version 与当前生产版本不符（复算口径不一致）："
                      f"{payload.get('model_version')} vs {forecast_engine.MODEL_VERSION}")
    if payload.get("feature_keys") != list(forecast_engine.FEATURE_KEYS):
        return None, "artifact feature_keys 与当前协议不符（复算口径不一致）"
    if [str(x) for x in (payload.get("horizons") or [])] != [str(x) for x in horizons]:
        return None, "artifact horizons 与当前验证契约不符（复算口径不一致）"
    try:
        if abs(float(payload.get("flat_margin", -1.0)) - float(flat_margin)) > 1e-12:
            return None, "artifact flat_margin 与当前配置不符（复算口径不一致）"
    except (TypeError, ValueError):
        return None, "artifact flat_margin 不可解析"
    raw_models = payload.get("dirmodels") or {}
    models = {}
    for h in horizons:
        clf = raw_models.get(h, raw_models.get(str(h)))
        if clf is None or not hasattr(clf, "predict_proba"):
            return None, f"artifact 缺少 T+{h} 持久化方向模型"
        models[str(h)] = clf
    return models, None


def recompute_pooled(samples: list, models: dict, horizons, flat_margin: float):
    """从 frozen 样本 + artifact 模型**独立重算** pooled 指标（含 CI）。

    返回 ``{h_str: {slot: value_or_None}}``；算不出的槽位为 None（如实，不造 0）。
    全部统计口径复用 backtest_forecast 既有纯函数，零新实现。
    """
    import numpy as np
    import backtest_forecast as bf
    from core import forecast_engine
    from core import validation_schema as vs

    est_idx = forecast_engine.FEATURE_KEYS.index("est_chg")
    train_all, oos, _oos_start = bf.split_date_oos(samples)
    out: dict = {}
    for h in horizons:
        hs = str(h)
        node = {k: None for k in SCALAR_SLOTS + CI_SLOTS}
        XY = bf.build_xy(train_all, h, flat_margin)
        XYo = bf.build_xy(oos, h, flat_margin)
        if XY is None or XYo is None or len(XY[1]) < 100 or len(XYo[1]) < 30:
            # 样本不足：n 仍如实记（真实计数永远可算），其余不可算
            node["n"] = int(len(XYo[1])) if XYo is not None else 0
            out[hs] = node
            continue
        X, y, _yret = XY
        Xo, yo, yreto = XYo
        po = models[hs].predict_proba(Xo)
        score_up = po[:, 2]
        base_vec = [Xo[i][est_idx] for i in range(Xo.shape[0])]
        dates = XYo.dates

        node["n"] = int(len(yo))
        ric_v, ric_st = bf.rank_ic_status(score_up.tolist(), yreto.tolist())
        base_v, base_st = bf.rank_ic_status(base_vec, yreto.tolist())
        node["rank_ic"] = _round(ric_v, 3) if ric_st == vs.STATUS_OK else None
        node["base_ic"] = _round(base_v, 3) if base_st == vs.STATUS_OK else None

        if ric_st == vs.STATUS_OK:
            ci = bf.cluster_bootstrap_ci(
                lambda sub: bf.rank_ic(sub["x"].tolist(), sub["y"].tolist()),
                {"x": score_up, "y": yreto}, dates)
            node["rank_ic_ci"] = ([_round(ci[0], CI_ROUND_DIGITS),
                                   _round(ci[1], CI_ROUND_DIGITS)]
                                  if ci[0] == ci[0] else None)
        if ric_st == vs.STATUS_OK and base_st == vs.STATUS_OK:
            node["decision_edge"] = _round(float(ric_v) - float(base_v), 4)
            edge_ci = bf.cluster_bootstrap_ci(
                lambda sub: (bf.rank_ic(sub["x"].tolist(), sub["y"].tolist())
                             - bf.rank_ic(sub["e"].tolist(), sub["y"].tolist())),
                {"x": score_up, "e": np.asarray(base_vec, dtype=float), "y": yreto},
                dates)
            node["decision_edge_ci"] = ([_round(edge_ci[0], EDGE_CI_ROUND_DIGITS),
                                         _round(edge_ci[1], EDGE_CI_ROUND_DIGITS)]
                                        if edge_ci[0] == edge_ci[0] else None)
        b_v, b_st = bf.brier_multiclass_status(yo, po)
        node["brier"] = _round(b_v, 3) if b_st == vs.STATUS_OK else None
        if b_st == vs.STATUS_OK:
            b_ci = bf.cluster_bootstrap_ci(
                lambda sub: bf.brier_multiclass(sub["y"], sub["p"]),
                {"y": yo, "p": po}, dates)
            node["brier_ci"] = ([_round(b_ci[0], CI_ROUND_DIGITS),
                                 _round(b_ci[1], CI_ROUND_DIGITS)]
                                if b_ci[0] == b_ci[0] else None)
        p_train = np.bincount(y, minlength=3) / max(1, len(y))
        onehot = np.zeros((len(yo), 3))
        onehot[np.arange(len(yo)), yo] = 1.0
        node["b_majority"] = _round(
            float(np.mean(np.sum((onehot - p_train) ** 2, axis=1))), 3)
        calib = bf.calibration_curve((yo == 2).astype(int), score_up)
        node[vs.MIDPOINT_CALIBRATION_ERROR_KEY] = (
            _round(calib["ace"], 3) if calib["status"] == vs.STATUS_OK else None)
        out[hs] = node
    return out


def check_evidence_recompute(evidence: dict, recomputed: dict) -> tuple[bool, list[str]]:
    """evidence.pooled 声明值 vs 独立复算值**逐位比对**。返回 (ok, mismatches)。

    判据（fail-closed，A 单验收「误差 0」）：
      - 复算可算（非 None）而 evidence 标非 OK ⇒ 不符（把算得出的说成算不出）；
      - 复算不可算（None）而 evidence 标 OK 带值 ⇒ 不符（**凭空捏造数值**，
        正是 A 单指出的 Computation proof 缺口）；
      - 两侧都有值 ⇒ round 口径一致后必须**逐位相等**（标量与 CI 双元素同判）。
    """
    from core import validation_schema as vs

    mismatches: list[str] = []
    pooled = evidence.get("pooled")
    if not isinstance(pooled, dict):
        return False, ["evidence.pooled 缺失或非 dict，无从比对"]
    for hs, rec in sorted(recomputed.items()):
        node = pooled.get(hs)
        if not isinstance(node, dict):
            mismatches.append(f"pooled T+{hs}: evidence 无该周期节点（复算有值）")
            continue
        for slot in SCALAR_SLOTS:
            declared = node.get(slot)
            if not isinstance(declared, dict):
                mismatches.append(f"pooled T+{hs}.{slot}: 非 {{value,status}} 节点")
                continue
            d_status, d_value = declared.get("status"), declared.get("value")
            r_value = rec.get(slot)
            if r_value is None:
                if d_status == vs.STATUS_OK:
                    mismatches.append(
                        f"pooled T+{hs}.{slot}: 复算不可算（None）但 evidence 声明 "
                        f"OK={d_value!r} —— 数值无法从冻结件+artifact 重算得出")
                continue
            if d_status != vs.STATUS_OK:
                mismatches.append(
                    f"pooled T+{hs}.{slot}: 复算={r_value!r} 但 evidence 标 "
                    f"{d_status}（把算得出的说成算不出）")
                continue
            if not isinstance(d_value, (int, float)) or isinstance(d_value, bool):
                mismatches.append(f"pooled T+{hs}.{slot}: 声明值非数值 {d_value!r}")
                continue
            expected = r_value if slot == "n" else _round(r_value, ROUND_DIGITS[slot])
            if float(d_value) != float(expected):
                mismatches.append(
                    f"pooled T+{hs}.{slot}: 声明 {d_value!r} ≠ 复算 {expected!r}"
                    f"（diff={abs(float(d_value) - float(expected)):.3g}）")
        for slot in CI_SLOTS:
            declared = node.get(slot)
            if not isinstance(declared, dict):
                mismatches.append(f"pooled T+{hs}.{slot}: 非 {{value,status}} 节点")
                continue
            d_status, d_value = declared.get("status"), declared.get("value")
            r_ci = rec.get(slot)
            if r_ci is None:
                if d_status == vs.STATUS_OK:
                    mismatches.append(
                        f"pooled T+{hs}.{slot}: 复算 CI 不可得但 evidence 声明 "
                        f"OK={d_value!r} —— CI 是门 2/门 3 的实际判据，不得凭空声明")
                continue
            if d_status != vs.STATUS_OK:
                mismatches.append(
                    f"pooled T+{hs}.{slot}: 复算 CI={r_ci!r} 但 evidence 标 {d_status}")
                continue
            if not (isinstance(d_value, (list, tuple)) and len(d_value) == 2):
                mismatches.append(f"pooled T+{hs}.{slot}: 声明 CI 非双元素 {d_value!r}")
                continue
            if (float(d_value[0]) != float(r_ci[0])
                    or float(d_value[1]) != float(r_ci[1])):
                mismatches.append(
                    f"pooled T+{hs}.{slot}: 声明 CI=[{d_value[0]!r},{d_value[1]!r}] ≠ "
                    f"复算 CI=[{r_ci[0]!r},{r_ci[1]!r}]")
    return (not mismatches), mismatches


def recompute_and_check(evidence: dict, base_dir: Path = BASE_DIR,
                        horizons=(1, 3, 5),
                        flat_margin: float = 0.003) -> tuple[bool, list[str], dict]:
    """一站式复算门（bind_validation_evidence 接线点）。

    返回 (ok, reasons, recomputed)。任何前置不成立（定位不到冻结件 / artifact 口径
    不符 / 样本不足）都 ⇒ ok=False 且给出可读原因，**fail-closed 不放行**。
    """
    prov = evidence.get("provenance") or {}

    def _pv(key):
        slot = prov.get(key)
        if not isinstance(slot, dict):
            return None
        return slot.get("value")

    samples, err = load_frozen_samples(str(_pv("dataset_sha256") or ""),
                                       str(_pv("frozen_dataset") or ""), base_dir)
    if samples is None:
        return False, [f"复算输入不可用：{err}"], {}
    models, err = load_artifact_models(str(_pv("artifact_sha256") or ""),
                                       horizons, flat_margin, base_dir)
    if models is None:
        return False, [f"复算模型不可用：{err}"], {}
    recomputed = recompute_pooled(samples, models, horizons, flat_margin)
    ok, mismatches = check_evidence_recompute(evidence, recomputed)
    if not ok:
        return False, ["evidence 数值与独立复算不符（Computation proof 失败）："] + mismatches, recomputed
    return True, ["ok"], recomputed
