"""R2-1 / R2-4 / R1-4 修复验收（审校 Round 3，分支 v4.4-r2fix-evidence-lineage）。

钉死六件事（三层验收的单测层）：
L1 三向血缘：evidence 三身份与 report/registry 任一不一致 → bind 拒（含结构化原因）；
L2 文件绑定：evidence 文件 bind 后被篡改/丢失 → verify_approval 拒；
L3 未走文件绑定的 v2 证据不得授权（evidence_lineage_unbound）；
L4 R2-4 schema 白名单：身份字段 OK 态非 hex → schema 拒；UNKNOWN 保持合法；
L5 R2-4 占位-OK：文本 "unknown" 可入档存储，promotion blocked_provenance；
L6 R1-4/R1-12：bind_validation_detailed 结构化失败原因。

全部 tempdir 隔离、零网络；治理约束 2（UNKNOWN/占位可存、不得当 PASS）随 L4/L5 钉死。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import model_registry as MR          # noqa: E402
from core import validation_schema as S        # noqa: E402


def _full_pass(prov_overrides=None):
    """五门全过形态证据；身份字段默认 hex 合法，texts 用非占位串。"""
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
    texts = {
        "frozen_dataset": "samples_frozen_20260910.jsonl",
        "artifact_sha256": "ab" * 32,
        "dataset_sha256": "cd" * 32,
        "git_commit": "e" * 40,
        "feature_protocol": "protocol_version=1;feature_dim=14",
        "contract_version": "C1-lineage-test",
        "produced_by": "test-lineage-fixture",
        "produced_at": "2026-09-25T00:00:00",
    }
    for k, v in texts.items():
        ev["provenance"][k] = S.ev_ok(v)
    ev["provenance"]["historical_feature_mode"] = S.ev_ok("PIT_1455_SNAPSHOT")
    ev["provenance"]["kfp_comparability"] = S.ev_ok("SAME")
    for k, v in (prov_overrides or {}).items():
        ev["provenance"][k] = v
    return ev


class TestEvidenceLineage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_registry_path = MR.REGISTRY_PATH
        self._orig_registry = (MR.REGISTRY_PATH.read_text(encoding="utf-8")
                               if MR.REGISTRY_PATH.exists() else None)
        self._orig_models_dir = MR.MODELS_DIR
        MR.MODELS_DIR = self.tmp
        MR.REGISTRY_PATH = self.tmp / "registry.json"

    def tearDown(self):
        MR.MODELS_DIR = self._orig_models_dir
        MR.REGISTRY_PATH = self._orig_registry_path
        if self._orig_registry is not None:
            self._orig_registry_path.write_text(self._orig_registry, encoding="utf-8")
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _setup_model(self, name="_lineage_model.pkl"):
        pkl = self.tmp / name
        pkl.write_bytes(b"lineage-model-bytes")
        MR.register_model(pkl, meta={}, snapshot_provenance={
            "snapshot_file": "samples_frozen_20260910.jsonl",
            "samples_sha256_lf": "12" * 32, "kfp_recorded_sha256": "34" * 32,
            "kfp_current_sha256": "34" * 32, "kfp_comparability": "SAME"})
        proto = MR.make_feature_protocol(["a"], masking=MR.B1_MASKING_PROTOCOL)
        self.assertTrue(MR.bind_feature_protocol(pkl.name, proto))
        entry = MR.get_model_entry(pkl.name)
        real = {
            "artifact_sha256": entry["sha256"],
            "dataset_sha256": entry["snapshot_provenance"]["samples_sha256_lf"],
            "git_commit": entry["git_commit"],
        }
        prov = {"validation_mode": "ARTIFACT", **real}
        report = self.tmp / "report.log"
        payload = json.dumps(prov, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))
        report.write_text("lineage report\nPROVENANCE_JSON=" + payload,
                          encoding="utf-8")
        return pkl, proto, real, report

    def _evidence_real(self, real):
        return _full_pass(prov_overrides={
            "artifact_sha256": S.ev_ok(real["artifact_sha256"]),
            "dataset_sha256": S.ev_ok(real["dataset_sha256"]),
            "git_commit": S.ev_ok(real["git_commit"])})

    # ---- L1 三向血缘 ----
    def test_lineage_mismatch_blocks_bind_with_reasons(self):
        pkl, _, real, report = self._setup_model()
        ev = self._evidence_real(real)
        ev["provenance"]["artifact_sha256"] = S.ev_ok("ff" * 32)
        ok, reasons = MR.bind_validation_detailed(
            pkl.name, str(report), "approved", evidence=ev)
        self.assertFalse(ok)
        self.assertIn("evidence_lineage_artifact_sha256_mismatch_vs_report", reasons)
        self.assertIn("evidence_lineage_artifact_sha256_mismatch_vs_registry", reasons)

    def test_lineage_evidence_vs_report_mismatch(self):
        pkl, _, real, report = self._setup_model()
        ev = self._evidence_real(real)
        ev["provenance"]["dataset_sha256"] = S.ev_ok("9" * 64)   # ≠ report(=registry)
        ok, reasons = MR.bind_validation_detailed(
            pkl.name, str(report), "approved", evidence=ev)
        self.assertFalse(ok)
        self.assertIn("evidence_lineage_dataset_sha256_mismatch_vs_report", reasons)
        self.assertIn("evidence_lineage_dataset_sha256_mismatch_vs_registry", reasons)

    def test_lineage_ok_binds_and_records_file(self):
        pkl, _, real, report = self._setup_model()
        ev = self._evidence_real(real)
        ev_file = self.tmp / "ev.json"
        ev_file.write_text(json.dumps(ev, ensure_ascii=False), encoding="utf-8")
        ok, reasons = MR.bind_validation_detailed(
            pkl.name, str(report), "approved", evidence=ev,
            evidence_file=str(ev_file))
        self.assertTrue(ok, reasons)
        validation = MR.get_model_entry(pkl.name)["validation"]
        self.assertEqual(validation["evidence_file"], str(ev_file))
        self.assertEqual(validation["evidence_sha256"], MR._file_sha256(ev_file))

    # ---- L2 文件篡改 / 丢失 ----
    def _bind_with_file(self):
        pkl, proto, real, report = self._setup_model()
        ev = self._evidence_real(real)
        ev_file = self.tmp / "ev.json"
        ev_file.write_text(json.dumps(ev, ensure_ascii=False), encoding="utf-8")
        ok, reasons = MR.bind_validation_detailed(
            pkl.name, str(report), "approved", evidence=ev,
            evidence_file=str(ev_file))
        self.assertTrue(ok, reasons)
        written, _ = MR.apply_promotion(pkl.name)
        self.assertTrue(written)
        return pkl, proto, ev_file

    def test_tampered_evidence_file_rejected_at_verify_approval(self):
        pkl, proto, ev_file = self._bind_with_file()
        ev = json.loads(ev_file.read_text(encoding="utf-8"))
        ev["pooled"]["1"]["rank_ic"] = S.ev_ok(0.9)      # 篡改指标
        ev_file.write_text(json.dumps(ev, ensure_ascii=False), encoding="utf-8")
        ok, reason = MR.verify_approval(pkl, proto)
        self.assertFalse(ok)
        self.assertEqual(reason, "evidence_lineage_hash_mismatch")

    def test_missing_evidence_file_rejected(self):
        pkl, proto, ev_file = self._bind_with_file()
        ev_file.unlink()
        ok, reason = MR.verify_approval(pkl, proto)
        self.assertFalse(ok)
        self.assertEqual(reason, "evidence_lineage_file_unreadable")

    # ---- L3 未走文件绑定的 v2 证据不得授权 ----
    def test_dict_bound_v2_evidence_cannot_authorize(self):
        pkl, proto, real, report = self._setup_model()
        ev = self._evidence_real(real)
        ok, reasons = MR.bind_validation_detailed(
            pkl.name, str(report), "approved", evidence=ev)   # 无 evidence_file
        self.assertTrue(ok, reasons)
        written, _ = MR.apply_promotion(pkl.name)
        self.assertTrue(written)
        va_ok, reason = MR.verify_approval(pkl, proto)
        self.assertFalse(va_ok)
        self.assertEqual(reason, "evidence_lineage_unbound")

    # ---- L4 schema 白名单 / UNKNOWN 合法 ----
    def test_schema_rejects_non_hex_identity_ok_values(self):
        ev = _full_pass()
        ev["provenance"]["git_commit"] = S.ev_ok("git_commit-ok")
        ok, errs = S.validate_evidence(ev)
        self.assertFalse(ok)
        self.assertTrue(any("git_commit" in e for e in errs), errs)

    def test_unknown_provenance_stays_legal(self):
        ev = S.blank_evidence()
        ok, errs = S.validate_evidence(ev)
        self.assertTrue(ok, errs)

    # ---- L5 占位-OK：可存储、不可授权（治理约束 2） ----
    def test_placeholder_ok_stored_but_blocked_at_promotion(self):
        pkl, _, real, report = self._setup_model()
        ev = self._evidence_real(real)
        ev["provenance"]["frozen_dataset"] = S.ev_ok("unknown")
        ev_file = self.tmp / "ev_ph.json"
        ev_file.write_text(json.dumps(ev, ensure_ascii=False), encoding="utf-8")
        ok, reasons = MR.bind_validation_detailed(
            pkl.name, str(report), "approved", evidence=ev,
            evidence_file=str(ev_file))
        self.assertTrue(ok, reasons)                       # 血缘真实 → 可入档
        written, d = MR.apply_promotion(pkl.name)
        self.assertTrue(written)
        self.assertEqual(d["status"], "blocked_provenance")
        self.assertIn("占位", d["reason"])

    # ---- L6 结构化失败原因（R1-4/R1-12） ----
    def test_bind_detailed_reports_report_side_reasons(self):
        pkl, _, real, report = self._setup_model()
        report.write_text("tampered\n", encoding="utf-8")  # provenance 行没了
        ok, reasons = MR.bind_validation_detailed(
            pkl.name, str(report), "approved")
        self.assertFalse(ok)
        self.assertEqual(reasons,
                         ["validation_report_unreadable_or_provenance_missing"])

    def test_bind_detailed_reports_provenance_reject_reason(self):
        pkl, _, real, report = self._setup_model()
        bad_report = self.tmp / "bad_report.log"
        bad_report.write_text(
            "x\nPROVENANCE_JSON=" + json.dumps({
                "validation_mode": "ARTIFACT",
                "artifact_sha256": "f" * 64,      # 与 registry 不一致
                "dataset_sha256": real["dataset_sha256"],
                "git_commit": real["git_commit"],
            }, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        ok, reasons = MR.bind_validation_detailed(
            pkl.name, str(bad_report), "approved")
        self.assertFalse(ok)
        self.assertEqual(len(reasons), 1)
        self.assertTrue(reasons[0].startswith("validation_provenance_reject:"), reasons)


if __name__ == "__main__":
    unittest.main()
