# -*- coding: utf-8 -*-
"""R3-1 / R2-4 / R4-8 攻击探针（审校 Round 3/4 留档，tools/evidence_audit/）。

四个探针，展示当前证据法院对三类伪造的实际处置（@v4.4-r4fix-freeze-receipt）：
  P1   占位-OK 文本（frozen_dataset="unknown" 等）    → 可入档存储，
       promotion blocked_provenance（R2-4 白名单 + R4-5 契约相等）
  P2   真身份 + 伪造 power.frozen=True 且无独立冻结记录 → 可入档存储，
       promotion blocked_power（R4-8-lite 对账 POWER_FREEZE_PATH）
  P3   三身份伪造（≠ registry/report）                → CLI 反查失败 /
       bind 三向血缘拒绝（R2-1）
  OPEN 真身份 + 真文本 + 伪造指标 + 独立冻结记录在案  → promotion approved
       —— R3-1 computation lineage 未闭环，Phase B（独立重算）单独立项；
       本探针即 R4-7 要求的 S3-ID 红灯的人工可读版。

用法：python tools/evidence_audit/r3_attack_metrics_forge.py [repo_root]
默认 repo_root = 本文件上两级。全程零网络、临时 registry、真实 registry 零改动。
"""
import contextlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 \
    else Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.stdout.reconfigure(encoding="utf-8")

from core import model_registry as MR  # noqa: E402
from core import validation_schema as S  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="evidence_audit_"))
MR.MODELS_DIR = TMP / "model_registry"
MR.MODELS_DIR.mkdir(parents=True)
MR.REGISTRY_PATH = MR.MODELS_DIR / "registry.json"
# 独立冻结记录缺席 = 当前仓库真实状态（功效阈值尚未冻结）。
MR.POWER_FREEZE_PATH = TMP / "power_freeze_absent.json"
print(f"[isolation] registry → {MR.REGISTRY_PATH}")

FROZEN_PROV = {
    "snapshot_file": "samples_frozen_20260910.jsonl",
    "samples_sha256_lf": "be8e" + "0" * 60,
    "kfp_recorded_sha256": "f5a2" + "0" * 60,
    "kfp_current_sha256": "f5a2" + "0" * 60,
    "kfp_comparability": "SAME",
}

pkl = TMP / "models" / "_audit_model.pkl"
pkl.parent.mkdir(parents=True)
pkl.write_bytes(b"evidence-audit-model-bytes")
MR.register_model(pkl, meta={"n_train": 50}, snapshot_provenance=FROZEN_PROV)
entry = MR.get_model_entry(pkl.name)
REAL = {
    "artifact_sha256": entry["sha256"],
    "dataset_sha256": entry["snapshot_provenance"]["samples_sha256_lf"],
    "git_commit": entry["git_commit"],
}
report = TMP / "audit_report.log"
payload = json.dumps({"validation_mode": "ARTIFACT", **REAL},
                     ensure_ascii=False, sort_keys=True, separators=(",", ":"))
report.write_text("audit report\nPROVENANCE_JSON=" + payload, encoding="utf-8")
PROTO = MR.make_feature_protocol(["a"], masking=MR.B1_MASKING_PROTOCOL)
MR.bind_feature_protocol(pkl.name, PROTO)
print(f"[setup] registry artifact sha256 = {entry['sha256'][:16]}...（真实身份）")


def forged_evidence(identity="real", frozen=True, text_style="real"):
    """全过形态证据；自由变量 = 身份来源 / frozen 自报 / 文本风格。"""
    ev = S.blank_evidence()
    ev["decision"] = "approved"
    for h in ("1", "3", "5"):
        ev["pooled"][h].update({
            "n": S.ev_ok(923), "rank_ic": S.ev_ok(0.05),
            "rank_ic_ci": S.ev_ok([0.01, 0.09]), "base_ic": S.ev_ok(0.01),
            "decision_edge": S.ev_ok(0.04),
            "decision_edge_ci": S.ev_ok([0.01, 0.07]),
            "brier": S.ev_ok(0.58), "brier_ci": S.ev_ok([0.55, 0.61]),
            "b_majority": S.ev_ok(0.60),
            S.MIDPOINT_CALIBRATION_ERROR_KEY: S.ev_ok(0.11)})
    for code in S.PRODUCTION_FUNDS:
        for h in ("1", "3", "5"):
            ev["funds"][code][h].update({
                "n": S.ev_ok(200), "decision_edge": S.ev_ok(0.03),
                "decision_edge_ci": S.ev_ok([0.005, 0.06])})
    ev["power"]["frozen"] = S.ev_ok(frozen)
    ev["power"]["n_power_fund"] = S.ev_ok(100)
    ids = dict(REAL) if identity == "real" else {
        "artifact_sha256": "A" * 64, "dataset_sha256": "B" * 64,
        "git_commit": "0" * 40}
    if text_style == "placeholder":
        texts = {"frozen_dataset": "unknown", "feature_protocol": "unknown",
                 "produced_by": "unknown", "produced_at": "unknown"}
    else:
        texts = {"frozen_dataset": "samples_frozen_20260910.jsonl",
                 "feature_protocol": "protocol_version=1;feature_dim=14",
                 "contract_version": "forecast-v1",
                 "produced_by": "backtest_forecast.py",
                 "produced_at": "2026-09-25T12:00:00"}
    for k, v in {**ids, **texts}.items():
        ev["provenance"][k] = S.ev_ok(v)
    ev["provenance"]["historical_feature_mode"] = S.ev_ok("PIT_1455_SNAPSHOT")
    ev["provenance"]["kfp_comparability"] = S.ev_ok("SAME")
    return ev


import bind_validation_evidence as CLI  # noqa: E402


def run_cli(ev: dict, use_model: bool = False) -> tuple[int, str]:
    ev_file = TMP / "probe_evidence.json"
    ev_file.write_text(json.dumps(ev, ensure_ascii=False), encoding="utf-8")
    argv = ["bind_validation_evidence.py", "--report", str(report),
            "--evidence", str(ev_file)]
    if use_model:
        argv += ["--model", pkl.name]
    sys.argv = argv
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = CLI.main()
    except SystemExit as e:
        rc = int(e.code or 0)
    promo = (MR.get_model_entry(pkl.name).get("promotion") or {}).get("status")
    return rc, promo


def check(label, got, expect):
    mark = "PASS" if got == expect else "FAIL"
    print(f"[{mark}] {label}: got={got!r} expect={expect!r}")


print("== P1 占位-OK 文本（可存不可授权；frozen=True 先过门 1） ==")
rc1, promo1 = run_cli(forged_evidence(identity="real", frozen=True,
                                      text_style="placeholder"))
check("P1 promotion.status", promo1, "blocked_provenance")

print("== P2 真身份 + 伪造 frozen=True + 无独立冻结记录 ==")
rc2, promo2 = run_cli(forged_evidence(identity="real", frozen=True))
check("P2 promotion.status", promo2, "blocked_power")

print("== P3 三身份伪造（--model 直达 bind 层血缘核对） ==")
rc3, promo3 = run_cli(forged_evidence(identity="wrong", frozen=True),
                      use_model=True)
check("P3 CLI 退出码（拒绝绑定）", rc3, 1)

print("== OPEN 真身份+真文本+伪造指标+冻结记录在案（R3-1 未闭环） ==")
freeze = TMP / "power_freeze.json"
freeze.write_text(json.dumps({"rule_version": "power_freeze_v1",
                              "frozen": True, "n_power_fund": 100},
                             ensure_ascii=False), encoding="utf-8")
MR.POWER_FREEZE_PATH = freeze
rc4, promo4 = run_cli(forged_evidence(identity="real", frozen=True))
check("OPEN promotion.status", promo4, "approved")
print("  ↑ OPEN：R3-1 computation lineage 未闭环（Phase B 独立重算单独立项）；"
      "本行即 R4-7 要求的 S3-ID 红灯的人工可读版")

shutil.rmtree(TMP, ignore_errors=True)
print("[cleanup] tmp removed:", not TMP.exists())
