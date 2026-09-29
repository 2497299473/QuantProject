#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D-01 探针：malformed registry 注入下各授权入口的行为（崩溃 vs 结构化拒绝）。

用法（venv）：
    .\\.venv\\Scripts\\python.exe -X utf8 evidence\\probes\\d01_malformed_probe.py

纪律：
- 全程 tempdir 隔离（替换 MODELS_DIR / REGISTRY_PATH / PKL_DIR），不触真实 registry；
- 零网络；只读 git（capture_provenance 已 mock，实际连 git 都不跑）；
- 同一脚本修复前/修复后各跑一次，输出逐条对照：
    CRASH <ExcType>  → 抛异常（(False, reason) 契约失效）
    REJECTED (False, '<reason>') → 结构化拒绝（fail-closed 成立）
    RETURNED <value> → 非崩溃非常规拒绝的返回值（如 get_model_entry）
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import model_registry as mr  # noqa: E402
from core import validation_schema as S  # noqa: E402

TRAIN_COMMIT = "d" * 40
FROZEN_PROV = {
    "snapshot_file": "samples_frozen_probe.jsonl",
    "samples_sha256_lf": "ab" * 32,
    "kfp_recorded_sha256": "cd" * 32,
    "kfp_current_sha256": "cd" * 32,
    "kfp_comparability": "SAME",
}


def full_pass_evidence_v2() -> dict:
    """五门全过的 schema v2 证据（与 tests/test_promotion_rule_v2 同款夹具）。"""
    ev = S.blank_evidence()
    ev["decision"] = "approved"
    for h in ("1", "3", "5"):
        ev["pooled"][h].update({
            "n": S.ev_ok(923), "rank_ic": S.ev_ok(0.05),
            "rank_ic_ci": S.ev_ok([0.01, 0.09]), "base_ic": S.ev_ok(0.01),
            "decision_edge": S.ev_ok(0.04), "decision_edge_ci": S.ev_ok([0.01, 0.07]),
            "brier": S.ev_ok(0.58), "brier_ci": S.ev_ok([0.55, 0.61]),
            "b_majority": S.ev_ok(0.60),
            S.MIDPOINT_CALIBRATION_ERROR_KEY: S.ev_ok(0.11)})
    for code in S.PRODUCTION_FUNDS:
        for h in ("1", "3", "5"):
            ev["funds"][code][h].update({
                "n": S.ev_ok(200), "decision_edge": S.ev_ok(0.03),
                "decision_edge_ci": S.ev_ok([0.005, 0.06])})
    ev["power"]["frozen"] = S.ev_ok(True)
    ev["power"]["n_power_fund"] = S.ev_ok(100)
    for k in ev["provenance"]:
        if k == "historical_feature_mode":
            ev["provenance"][k] = S.ev_ok("PIT_1455_SNAPSHOT")
        elif k == "kfp_comparability":
            ev["provenance"][k] = S.ev_ok("SAME")
        else:
            ev["provenance"][k] = S.ev_ok(f"{k}-ok")
    return ev


def probe(name: str, fn) -> None:
    """执行单条探针：崩溃记 CRASH，(False, reason) 记 REJECTED，其余记 RETURNED。"""
    try:
        out = fn()
    except Exception:
        exc_type, exc, _tb = sys.exc_info()
        first = traceback.format_exc().strip().splitlines()[-1]
        print(f"[PROBE] {name}: CRASH {exc_type.__name__} ({first})")
        return
    if isinstance(out, tuple) and len(out) == 2 and out[0] is False:
        print(f"[PROBE] {name}: REJECTED (False, {out[1]!r})")
    elif isinstance(out, tuple) and len(out) == 2 and out[0] is True:
        print(f"[PROBE] {name}: PASSED-THROUGH (True, {out[1]!r})  <-- 注意：放行")
    else:
        print(f"[PROBE] {name}: RETURNED {out!r}")


def seed_green(tmp: Path) -> Path:
    """构造 verify_approval 全绿条目（register+protocol+report+bind+promotion）。"""
    pkl = tmp / "_probe_green.pkl"
    pkl.write_bytes(b"probe-green-seed")
    with mock.patch("core.model_registry.capture_provenance",
                    return_value={"data_manifest_sha256": "m" * 16,
                                  "git_commit": TRAIN_COMMIT}):
        digest = mr.register_model(pkl, meta={}, snapshot_provenance=FROZEN_PROV)
    assert digest is not None, "seed: register_model failed"
    proto = mr.make_feature_protocol(["a"], masking=mr.B1_MASKING_PROTOCOL)
    assert mr.bind_feature_protocol(pkl.name, proto), "seed: bind_feature_protocol failed"
    entry = mr.get_model_entry(pkl.name)
    report = tmp / "_probe_report.log"
    prov_line = {
        "validation_mode": "ARTIFACT",
        "artifact_sha256": entry["sha256"],
        "dataset_sha256": FROZEN_PROV["samples_sha256_lf"],
        "git_commit": TRAIN_COMMIT,
    }
    report.write_text(
        "probe evidence\nPROVENANCE_JSON="
        + json.dumps(prov_line, sort_keys=True, separators=(",", ":")),
        encoding="utf-8")
    metrics = {str(h): {"decision": "approved", "ric_ci": [0.01, 0.05]}
               for h in (1, 3, 5)}
    assert mr.bind_validation(pkl.name, str(report), "approved", metrics,
                              evidence=full_pass_evidence_v2()), "seed: bind_validation failed"
    written, derived = mr.apply_promotion(pkl.name)
    assert written and derived["status"] == "approved", f"seed: promotion {derived}"
    ok, reason = mr.verify_approval(pkl, proto)
    assert ok, f"seed: verify_approval not green: {reason}"
    return pkl


def mutate(pkl_name: str, path: list, value) -> None:
    """按路径就地篡改 registry 条目（模拟手工编辑攻击面）。"""
    reg = mr.load_registry()
    node = reg["models"][pkl_name]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert mr._save_registry(reg)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="d01_probe_"))
    orig = (mr.MODELS_DIR, mr.REGISTRY_PATH, mr.PKL_DIR)
    try:
        mr.MODELS_DIR = tmp
        mr.REGISTRY_PATH = tmp / "registry.json"
        mr.PKL_DIR = tmp

        print(f"HEAD-ANCHOR: probe runs against working tree at {BASE_DIR}")
        green = seed_green(tmp)
        proto = mr.make_feature_protocol(["a"], masking=mr.B1_MASKING_PROTOCOL)

        # ---- 1. verify_feature_protocol：类型污染 / malformed ----
        def fp(field, value):
            mutate(green.name, ["feature_protocol", field], value)
            return mr.verify_feature_protocol(green.name, proto)
        probe("verify_feature_protocol protocol_version='abc'", lambda: fp("protocol_version", "abc"))
        probe("verify_feature_protocol protocol_version='1' (类型污染)", lambda: fp("protocol_version", "1"))
        probe("verify_feature_protocol protocol_version=True (类型污染)", lambda: fp("protocol_version", True))
        probe("verify_feature_protocol feature_keys='str' (非列表)", lambda: fp("feature_keys", "str"))
        mutate(green.name, ["feature_protocol"], proto)  # 复原，隔离下一探针（expected=None 须独立命中）
        probe("verify_feature_protocol expected=None", lambda: mr.verify_feature_protocol(green.name, None))
        mutate(green.name, ["feature_protocol"], "not-a-dict")
        probe("verify_feature_protocol feature_protocol=str (整块畸形)", lambda: mr.verify_feature_protocol(green.name, proto))
        mutate(green.name, ["feature_protocol"], proto)  # 复原

        # ---- 2. verify_approval：promotion / snapshot_provenance / stored_prov ----
        mutate(green.name, ["promotion"], "approved")
        probe("verify_approval promotion='approved' (str)", lambda: mr.verify_approval(green, proto))
        mutate(green.name, ["promotion"], {"status": "approved", "reason": "x",
                                           "updated_at": "t", "derived_by": "derive_promotion",
                                           "rule_version": 2})
        # 复原 promotion 为推导产物
        mr.apply_promotion(green.name)

        mutate(green.name, ["snapshot_provenance"], "SAME")
        probe("verify_approval snapshot_provenance='SAME' (str)", lambda: mr.verify_approval(green, proto))
        mutate(green.name, ["snapshot_provenance"], dict(FROZEN_PROV))

        mutate(green.name, ["validation", "provenance"], "not-a-dict")
        probe("verify_approval validation.provenance=str", lambda: mr.verify_approval(green, proto))
        # 复原：重绑（bind 会重写 validation 块）
        entry = mr.get_model_entry(green.name)
        report = tmp / "_probe_report.log"
        metrics = {str(h): {"decision": "approved", "ric_ci": [0.01, 0.05]} for h in (1, 3, 5)}
        mr.bind_validation(green.name, str(report), "approved", metrics,
                           evidence=full_pass_evidence_v2())
        mr.apply_promotion(green.name)

        probe("verify_approval expected=None", lambda: mr.verify_approval(green, None))

        # ---- 3. verify_validation_report：report_file 类型注入 ----
        mutate(green.name, ["validation", "report_file"], 123)
        probe("verify_validation_report report_file=123", lambda: mr.verify_validation_report(green.name))
        mr.bind_validation(green.name, str(report), "approved", metrics,
                           evidence=full_pass_evidence_v2())
        mr.apply_promotion(green.name)

        # ---- 4. entry 整体畸形：get_model_entry / read_verified_model_bytes / register_model ----
        reg = mr.load_registry()
        reg["models"]["_probe_bad_neighbor.pkl"] = "not-a-dict"
        assert mr._save_registry(reg)
        (tmp / "_probe_bad_neighbor.pkl").write_bytes(b"bad-neighbor")  # 文件在场，确保命中 entry 检查而非 file_missing
        probe("get_model_entry entry=str", lambda: mr.get_model_entry("_probe_bad_neighbor.pkl"))
        probe("read_verified_model_bytes entry=str", lambda: mr.read_verified_model_bytes(tmp / "_probe_bad_neighbor.pkl"))
        probe("verify_feature_protocol entry=str", lambda: mr.verify_feature_protocol("_probe_bad_neighbor.pkl", proto))

        new_pkl = tmp / "_probe_new.pkl"
        new_pkl.write_bytes(b"probe-new-bytes")
        with mock.patch("core.model_registry.capture_provenance",
                        return_value={"data_manifest_sha256": "m" * 16,
                                      "git_commit": TRAIN_COMMIT}):
            probe("register_model 畸形邻居在场", lambda: mr.register_model(new_pkl, meta={}))

        # ---- 5. 全绿对照（修复前后都必须 (True, 'ok')）----
        probe("verify_approval 全绿对照", lambda: mr.verify_approval(green, proto))
        return 0
    finally:
        mr.MODELS_DIR, mr.REGISTRY_PATH, mr.PKL_DIR = orig
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
