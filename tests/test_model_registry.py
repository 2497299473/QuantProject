"""P1：模型注册表 + sha256 完整性校验测试（2026-08-29，GPT 三审）。

覆盖：
- register → verify 往返一致
- hash 不匹配（模拟篡改）→ 拒绝
- registry 无该文件条目 → 拒绝
- 文件缺失 → 拒绝
- 注册表损坏 → load_registry 返回空结构（不抛异常）
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import model_registry  # noqa: E402


def _full_pass_evidence_v2():
    """rule v2 五门全过的 schema v2 证据夹具（B++-3；同款见 tests/test_promotion_rule_v2.py）。"""
    from core import validation_schema as S
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
        ev["provenance"][k] = S.ev_ok(f"{k}-ok")
    return ev


class TestModelRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = model_registry.MODELS_DIR
        self.tmp.mkdir(parents=True, exist_ok=True)
        self._orig_registry = model_registry.REGISTRY_PATH.read_text(
            encoding="utf-8") if model_registry.REGISTRY_PATH.exists() else None
        # 用独立测试文件名，避免污染真实 forecast_v*.pkl 条目
        self.test_pkl = self.tmp / "_test_registry_model.pkl"
        self.test_report = self.tmp / "_test_validation_report.log"
        self._cleanup()

    def _report_provenance(self, overrides=None):
        entry = model_registry.get_model_entry(self.test_pkl.name)
        snap = entry.get("snapshot_provenance") or {}
        prov = {
            "artifact_sha256": entry["sha256"],
            "dataset_sha256": snap.get("samples_sha256_lf"),
            "git_commit": entry.get("git_commit"),
        }
        if overrides:
            prov.update(overrides)
        return prov
    def _write_report(self, content="report-A", provenance=None, duplicate=False):
        payload = json.dumps(provenance or self._report_provenance(),
                             ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        lines = [content, "PROVENANCE_JSON=" + payload]
        if duplicate:
            lines.append("PROVENANCE_JSON=" + payload)
        self.test_report.write_text("\n".join(lines), encoding="utf-8")

    def _bind_report(self, pkl_name, decision="rejected", metrics=None,
                     provenance=None, duplicate=False):
        self._write_report(provenance=provenance, duplicate=duplicate)
        return model_registry.bind_validation(
            pkl_name, str(self.test_report), decision, metrics=metrics)
    def _cleanup(self):
        self.test_pkl.unlink(missing_ok=True)
        self.test_report.unlink(missing_ok=True)
        reg = model_registry.load_registry()
        reg["models"].pop(self.test_pkl.name, None)
        model_registry._save_registry(reg)

    def tearDown(self):
        self._cleanup()
        # 恢复原始注册表（测试期间可能删过条目）
        if self._orig_registry is not None:
            model_registry.REGISTRY_PATH.write_text(self._orig_registry, encoding="utf-8")
        else:
            model_registry.REGISTRY_PATH.unlink(missing_ok=True)

    def test_register_verify_roundtrip(self):
        self.test_pkl.write_bytes(b"fake-pickle-bytes")
        digest = model_registry.register_model(self.test_pkl, meta={"n_train": 100})
        self.assertIsNotNone(digest)
        self.assertEqual(len(digest), 64)
        ok, reason = model_registry.verify_model(self.test_pkl)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_hash_mismatch_rejected(self):
        self.test_pkl.write_bytes(b"original-bytes")
        model_registry.register_model(self.test_pkl, meta={})
        self.test_pkl.write_bytes(b"tampered-bytes")   # 模拟篡改
        ok, reason = model_registry.verify_model(self.test_pkl)
        self.assertFalse(ok)
        self.assertEqual(reason, "hash_mismatch")

    def test_no_entry_rejected(self):
        """未登记的文件 → no_entry（宁缺毋滥）。"""
        self.test_pkl.write_bytes(b"unregistered")
        ok, reason = model_registry.verify_model(self.test_pkl)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_entry")

    def test_missing_file_rejected(self):
        ok, reason = model_registry.verify_model(self.tmp / "_never_created.pkl")
        self.assertFalse(ok)
        self.assertEqual(reason, "file_missing")

    def test_register_missing_file_returns_none(self):
        digest = model_registry.register_model(self.tmp / "_ghost.pkl", meta={})
        self.assertIsNone(digest)

    def test_corrupt_registry_returns_empty(self):
        model_registry.REGISTRY_PATH.write_text("{not json", encoding="utf-8")
        reg = model_registry.load_registry()
        self.assertEqual(reg, {"models": {}})

    def test_bind_validation_and_promotion_roundtrip(self):
        """实际报告内容→现场重算 SHA→provenance→registry 往返。"""
        self.test_pkl.write_bytes(b"validation-test-bytes")
        model_registry.register_model(self.test_pkl, meta={"n_train": 50},
                                      snapshot_provenance=self.FROZEN_PROV)
        self.assertTrue(self._bind_report(
            self.test_pkl.name, decision="rejected",
            metrics={"5": {"rank_ic": 0.08}}))
        entry = model_registry.get_model_entry(self.test_pkl.name)
        self.assertEqual(entry["validation"]["report_sha256"],
                         model_registry._file_sha256(self.test_report))
        ok2 = model_registry.update_promotion(
            self.test_pkl.name, "blocked", "CI 跨零")
        self.assertTrue(ok2)
        self.assertEqual(entry["validation"]["decision"], "rejected")
        self.assertEqual(entry["validation"]["metrics"]["5"]["rank_ic"], 0.08)
        self.assertEqual(model_registry.get_model_entry(self.test_pkl.name)["promotion"]["status"], "blocked")
    def test_validation_report_content_provenance_mismatch_rejected(self):
        """报告侧三项 provenance 任一失配都拒绝；不接受调用者声明。"""
        self.test_pkl.write_bytes(b"model-A")
        model_registry.register_model(self.test_pkl, meta={},
                                      snapshot_provenance=self.FROZEN_PROV)
        self._write_report()
        self.assertTrue(model_registry.bind_validation(
            self.test_pkl.name, str(self.test_report), "rejected"))
        for key, bad in (("artifact_sha256", "b" * 64),
                         ("dataset_sha256", "c" * 64),
                         ("git_commit", "f" * 40)):
            self._write_report(provenance=self._report_provenance({key: bad}))
            self.assertFalse(model_registry.bind_validation(
                self.test_pkl.name, str(self.test_report), "rejected"))
    def test_validation_provenance_missing_rejected(self):
        """registry 无冻结样本 provenance → bind fail-closed。"""
        self.test_pkl.write_bytes(b"model-A")
        model_registry.register_model(self.test_pkl, meta={})
        self.test_report.write_text(
            "report-A\nPROVENANCE_JSON={}", encoding="utf-8")
        self.assertFalse(model_registry.bind_validation(
            self.test_pkl.name, str(self.test_report), "rejected"))
    def test_validation_report_missing_provenance_rejected(self):
        self.test_pkl.write_bytes(b"model-A")
        model_registry.register_model(self.test_pkl, meta={},
                                      snapshot_provenance=self.FROZEN_PROV)
        self.test_report.write_text("report-without-provenance", encoding="utf-8")
        self.assertFalse(model_registry.bind_validation(
            self.test_pkl.name, str(self.test_report), "rejected"))

    def test_validation_report_duplicate_provenance_rejected(self):
        self.test_pkl.write_bytes(b"model-A")
        model_registry.register_model(self.test_pkl, meta={},
                                      snapshot_provenance=self.FROZEN_PROV)
        self._write_report(duplicate=True)
        self.assertFalse(model_registry.bind_validation(
            self.test_pkl.name, str(self.test_report), "rejected"))

    def test_validation_report_malformed_provenance_rejected(self):
        self.test_pkl.write_bytes(b"model-A")
        model_registry.register_model(self.test_pkl, meta={},
                                      snapshot_provenance=self.FROZEN_PROV)
        self.test_report.write_text(
            "report\nPROVENANCE_JSON={not-json}", encoding="utf-8")
        self.assertFalse(model_registry.bind_validation(
            self.test_pkl.name, str(self.test_report), "rejected"))

    def test_bind_unknown_model_returns_false(self):
        """绑定不存在的模型 → False（不新增幽灵条目）。"""
        self.assertFalse(model_registry.bind_validation("_ghost.pkl", "x.log", "rejected"))

    def test_get_model_entry_missing_returns_none(self):
        self.assertIsNone(model_registry.get_model_entry("_ghost.pkl"))

    def test_validation_report_hash_is_enforced(self):
        self.test_pkl.write_bytes(b"approved-model")
        model_registry.register_model(self.test_pkl, meta={},
                                      snapshot_provenance=self.FROZEN_PROV)
        self._write_report(content="approved evidence")
        metrics = {str(h): {"decision": "approved", "ric_ci": [0.01, 0.05]}
                   for h in (1, 3, 5)}
        self.assertTrue(model_registry.bind_validation(
            self.test_pkl.name, str(self.test_report), "approved", metrics))
        entry = model_registry.get_model_entry(self.test_pkl.name)
        self.assertEqual(entry["validation"]["report_sha256"],
                         model_registry._file_sha256(self.test_report))
        ok, reason = model_registry.verify_validation_report(self.test_pkl.name)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.test_report.write_text("tampered evidence", encoding="utf-8")
        ok, reason = model_registry.verify_validation_report(self.test_pkl.name)
        self.assertFalse(ok)
        self.assertEqual(reason, "validation_report_hash_mismatch")
    def test_approval_requires_model_protocol_validation_and_promotion(self):
        self.test_pkl.write_bytes(b"approved-model")
        model_registry.register_model(self.test_pkl, meta={},
                                      snapshot_provenance=self.FROZEN_PROV)
        proto = model_registry.make_feature_protocol(
            ["a"], masking=model_registry.B1_MASKING_PROTOCOL)
        self.assertTrue(model_registry.bind_feature_protocol(self.test_pkl.name, proto))
        self._write_report(content="approved evidence")
        metrics = {str(h): {"decision": "approved", "ric_ci": [0.01, 0.05]}
                   for h in (1, 3, 5)}
        self.assertTrue(model_registry.bind_validation(
            self.test_pkl.name, str(self.test_report), "approved", metrics))
        reg = model_registry.load_registry()
        reg["models"][self.test_pkl.name]["validation"]["evidence"] = _full_pass_evidence_v2()
        self.assertTrue(model_registry._save_registry(reg))
        self.assertTrue(model_registry.apply_promotion(self.test_pkl.name)[0])
        ok, reason = model_registry.verify_approval(self.test_pkl, proto)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertTrue(model_registry.update_promotion(
            self.test_pkl.name, "blocked", "manual block"))
        ok, reason = model_registry.verify_approval(self.test_pkl, proto)
        self.assertFalse(ok)
        self.assertEqual(reason, "promotion_not_approved")
    def _bind_full_evidence(self, prov=None):
        """注册协议 + validation；报告 provenance 来自实际文件内容。"""
        self.test_pkl.write_bytes(b"approval-provenance-bytes")
        model_registry.register_model(self.test_pkl, meta={},
                                      snapshot_provenance=prov)
        proto = model_registry.make_feature_protocol(
            ["a"], masking=model_registry.B1_MASKING_PROTOCOL)
        self.assertTrue(model_registry.bind_feature_protocol(self.test_pkl.name, proto))
        entry = model_registry.get_model_entry(self.test_pkl.name)
        if prov is not None:
            rp = {"artifact_sha256": entry["sha256"],
                  "dataset_sha256": prov["samples_sha256_lf"],
                  "git_commit": entry["git_commit"]}
        else:
            rp = {"artifact_sha256": entry["sha256"],
                  "dataset_sha256": "d" * 64,
                  "git_commit": entry["git_commit"]}
        self._write_report(content="approved evidence", provenance=rp)
        metrics = {str(h): {"decision": "approved", "ric_ci": [0.01, 0.05]}
                   for h in (1, 3, 5)}
        ok = model_registry.bind_validation(
            self.test_pkl.name, str(self.test_report), "approved", metrics)
        self.assertEqual(ok, prov is not None)
        if prov is None:
            return proto
        reg = model_registry.load_registry()
        reg["models"][self.test_pkl.name]["validation"]["evidence"] = _full_pass_evidence_v2()
        self.assertTrue(model_registry._save_registry(reg))
        self.assertTrue(model_registry.apply_promotion(self.test_pkl.name)[0])
        return proto
    def test_approval_rejects_fresh_provenance(self):
        """FRESH 无冻结样本身份 → 即使报告内容完整也不能绑定。"""
        self.test_pkl.write_bytes(b"approval-provenance-bytes")
        model_registry.register_model(self.test_pkl, meta={}, snapshot_provenance=None)
        entry = model_registry.get_model_entry(self.test_pkl.name)
        self._write_report(provenance={
            "artifact_sha256": entry["sha256"],
            "dataset_sha256": "d" * 64,
            "git_commit": entry["git_commit"],
        })
        self.assertFalse(model_registry.bind_validation(
            self.test_pkl.name, str(self.test_report), "approved"))
        self.assertNotIn("validation",
                         model_registry.get_model_entry(self.test_pkl.name))
    def test_approval_rejects_legacy_entry_without_provenance_block(self):
        """历史条目（registry 无 snapshot_provenance 块）→ 同拒（不回补、不宽容）。"""
        proto = self._bind_full_evidence(prov=self.FROZEN_PROV)
        reg = model_registry.load_registry()
        reg["models"][self.test_pkl.name].pop("snapshot_provenance", None)
        self.assertTrue(model_registry._save_registry(reg))
        ok, reason = model_registry.verify_approval(self.test_pkl, proto)
        self.assertFalse(ok)
        self.assertEqual(
            reason, "snapshot_provenance_incomplete:"
                    "snapshot_file,samples_sha256_lf,kfp_recorded_sha256,"
                    "kfp_current_sha256,kfp_comparability")

    # ---------- v3（2026-09-01，GPT 五审）：feature_protocol ----------
    def test_feature_protocol_make_and_bind_roundtrip(self):
        """make → bind → verify 往返一致（B1 双列：2 特征 = 4 维）。"""
        self.test_pkl.write_bytes(b"protocol-test-bytes")
        model_registry.register_model(self.test_pkl, meta={"n_train": 10})
        proto = model_registry.make_feature_protocol(
            ["a", "b"], masking=model_registry.B1_MASKING_PROTOCOL)
        self.assertEqual(proto["n_features"], 2)
        self.assertEqual(proto["feature_dim"], 4)
        self.assertTrue(model_registry.bind_feature_protocol(self.test_pkl.name, proto))
        ok, reason = model_registry.verify_feature_protocol(
            self.test_pkl.name,
            model_registry.make_feature_protocol(
                ["a", "b"], masking=model_registry.B1_MASKING_PROTOCOL))
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_feature_protocol_dim_mismatch_rejected(self):
        """登记 4 维（带掩码）vs 期望 2 维（纯值）→ feature_dim_mismatch。"""
        self.test_pkl.write_bytes(b"protocol-mismatch-bytes")
        model_registry.register_model(self.test_pkl, meta={})
        masked = model_registry.make_feature_protocol(
            ["a", "b"], masking=model_registry.B1_MASKING_PROTOCOL)
        plain = model_registry.make_feature_protocol(["a", "b"])
        self.assertTrue(model_registry.bind_feature_protocol(self.test_pkl.name, masked))
        ok, reason = model_registry.verify_feature_protocol(self.test_pkl.name, plain)
        self.assertFalse(ok)
        self.assertEqual(reason, "feature_dim_mismatch")

    def test_feature_protocol_missing_rejected(self):
        """未登记协议 → no_protocol（宁缺毋滥：load 拒绝加载）。"""
        self.test_pkl.write_bytes(b"no-protocol-bytes")
        model_registry.register_model(self.test_pkl, meta={})
        ok, reason = model_registry.verify_feature_protocol(
            self.test_pkl.name,
            model_registry.make_feature_protocol(
                ["a"], masking=model_registry.B1_MASKING_PROTOCOL))
        self.assertFalse(ok)
        self.assertEqual(reason, "no_protocol")

    def test_feature_protocol_unknown_model(self):
        ok, reason = model_registry.verify_feature_protocol("_ghost.pkl", {})
        self.assertFalse(ok)
        self.assertEqual(reason, "no_entry")

    # ---------- v4（2026-09-01，GPT 五审 P3）：promotion 纯函数 ----------
    def _bind(self, decision, metrics, auto_promotion=False):
        """注册假模型并用实际报告内容绑定 validation。"""
        self.test_pkl.write_bytes(b"promotion-test-bytes")
        model_registry.register_model(self.test_pkl, meta={},
                                      snapshot_provenance=self.FROZEN_PROV)
        self._write_report(provenance=self._report_provenance())
        self.assertTrue(model_registry.bind_validation(
            self.test_pkl.name, str(self.test_report), decision,
            metrics=metrics, auto_promotion=auto_promotion))
    def test_derive_promotion_deterministic_blocked(self):
        """v2/v3 真实场景（T+1/T+3 跨零、T+5 过）→ blocked，且同输入两次调用逐字段一致。"""
        metrics = {
            "1": {"rank_ic": 0.005, "ric_ci": [-0.066, 0.07], "decision": "rejected"},
            "3": {"rank_ic": 0.002, "ric_ci": [-0.061, 0.066], "decision": "rejected"},
            "5": {"rank_ic": 0.08, "ric_ci": [0.017, 0.142], "decision": "approved"},
        }
        d1 = model_registry.derive_promotion({"decision": "rejected", "metrics": metrics})
        d2 = model_registry.derive_promotion({"decision": "rejected", "metrics": metrics})
        self.assertEqual(d1, d2)
        self.assertEqual(d1["status"], "blocked")
        self.assertEqual(d1["failed_horizons"], ["1", "3"])
        self.assertIn("未全过", d1["reason"])
        self.assertIn("model_ready 维持 false", d1["reason"])

    def test_derive_promotion_legacy_all_approved_research_only(self):
        """rule v2（B++-3）：legacy 三周期全过 → research_only——pooled-only 不再
        构成生产授权依据（契约 B §4），approved 出口仅对 schema v2 五门证据开放。"""
        metrics = {
            "1": {"ric_ci": [0.01, 0.05], "decision": "approved"},
            "3": {"ric_ci": [0.02, 0.06], "decision": "approved"},
            "5": {"ric_ci": [0.03, 0.07], "decision": "approved"},
        }
        d = model_registry.derive_promotion({"decision": "approved", "metrics": metrics})
        self.assertEqual(d["status"], "research_only")
        self.assertEqual(d["failed_horizons"], [])
        self.assertIn("schema v2", d["reason"])

    def test_derive_promotion_inconsistent_binding_blocked(self):
        """整体 approved 但有周期未过（绑定不一致）→ blocked（三周期全过是硬门槛）。"""
        metrics = {
            "1": {"ric_ci": [-0.05, 0.09], "decision": "rejected"},
            "3": {"ric_ci": [0.01, 0.05], "decision": "approved"},
            "5": {"ric_ci": [0.02, 0.06], "decision": "approved"},
        }
        d = model_registry.derive_promotion({"decision": "approved", "metrics": metrics})
        self.assertEqual(d["status"], "blocked")
        self.assertIn("需三周期全过", d["reason"])

    def test_derive_promotion_invalid_and_missing_metrics(self):
        """无依据/无 metrics → pending，不强判。"""
        d_none = model_registry.derive_promotion(None)
        self.assertEqual(d_none["status"], "pending")
        d_bad = model_registry.derive_promotion({"decision": "weird"})
        self.assertEqual(d_bad["status"], "pending")
        d_nom = model_registry.derive_promotion({"decision": "approved", "metrics": {}})
        self.assertEqual(d_nom["status"], "pending")
        self.assertIn("pending", d_nom["reason"])

    def test_bind_validation_auto_promotion_writes_derived(self):
        """bind_validation(auto_promotion=True) 自动推导 promotion，写入 derived_by（审计可辨）。"""
        self._bind("rejected", {
            "1": {"ric_ci": [-0.066, 0.07], "decision": "rejected"},
            "3": {"ric_ci": [-0.061, 0.066], "decision": "rejected"},
            "5": {"ric_ci": [0.017, 0.142], "decision": "approved"},
        }, auto_promotion=True)
        entry = model_registry.get_model_entry(self.test_pkl.name)
        promo = entry["promotion"]
        self.assertEqual(promo["status"], "blocked")
        self.assertEqual(promo["derived_by"], "derive_promotion")
        self.assertEqual(promo["rule_version"], 2)   # B++-3：rule v2

    def test_apply_promotion_dry_run_no_write(self):
        """dry_run 只推导不落盘；无 validation 不动现有 promotion（不强写 pending）。"""
        self._bind("approved", {
            "1": {"ric_ci": [0.01, 0.05], "decision": "approved"},
            "3": {"ric_ci": [0.02, 0.06], "decision": "approved"},
            "5": {"ric_ci": [0.03, 0.07], "decision": "approved"},
        })
        written, d = model_registry.apply_promotion(self.test_pkl.name, dry_run=True)
        self.assertFalse(written)
        self.assertEqual(d["status"], "research_only")   # rule v2：legacy 全过不再 approved
        entry = model_registry.get_model_entry(self.test_pkl.name)
        self.assertNotIn("promotion", entry)   # dry_run 未写入

        # 幽灵条目 → unknown
        w2, d2 = model_registry.apply_promotion("_ghost.pkl")
        self.assertFalse(w2)
        self.assertEqual(d2["status"], "unknown")

        # 已注册但无 validation → 不落盘、不动现有 promotion
        self.test_pkl.write_bytes(b"no-validation-bytes")
        model_registry.register_model(self.test_pkl, meta={})
        model_registry.update_promotion(self.test_pkl.name, "blocked", "历史手填")
        w3, d3 = model_registry.apply_promotion(self.test_pkl.name)
        self.assertFalse(w3)
        self.assertEqual(d3["status"], "pending")
        entry = model_registry.get_model_entry(self.test_pkl.name)
        self.assertEqual(entry["promotion"]["status"], "blocked")   # 历史手填未被动
        self.assertNotIn("derived_by", entry["promotion"])            # 仍是手填产物

class TestPreregDegradation(unittest.TestCase):
    """2026-09-13 D2：Shadow 预注册降级授权（prereg_shadow_v1）。

    覆盖：正例 / 未登记 / 无文件 / 停用 / sha 不符 / T+5 不显著 / T+1 负 IC /
    过期 / rule_version 不符；并验证降级**不影响** verify_approval 主授权门。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_registry_path = model_registry.REGISTRY_PATH   # 先存 Path 原值
        self._orig_registry = (model_registry.REGISTRY_PATH.read_text(
            encoding="utf-8") if model_registry.REGISTRY_PATH.exists() else None)
        self._orig_models_dir = model_registry.MODELS_DIR
        model_registry.MODELS_DIR = self.tmp
        model_registry.REGISTRY_PATH = self.tmp / "registry.json"
        self.pkl = self.tmp / "forecast_v3.pkl"
        self.report = self.tmp / "validation.log"
        self.prereg = self.tmp / "promotion_prereg.json"

    def tearDown(self):
        model_registry.MODELS_DIR = self._orig_models_dir
        # 先恢复 REGISTRY_PATH 属性本身再回写内容，防属性留在已删临时目录上
        model_registry.REGISTRY_PATH = self._orig_registry_path
        if self._orig_registry is not None:
            model_registry.REGISTRY_PATH.write_text(self._orig_registry,
                                                    encoding="utf-8")
        shutil.rmtree(self.tmp, ignore_errors=True)

    PROTO = None   # 由 _seed 填充（与 verify_approval 用同一份协议）

    def _report_provenance(self):
        entry = model_registry.get_model_entry(self.pkl.name)
        snap = entry.get("snapshot_provenance") or {}
        return {
            "artifact_sha256": entry["sha256"],
            "dataset_sha256": snap.get("samples_sha256_lf"),
            "git_commit": entry.get("git_commit"),
        }
    def _write_report(self):
        prov = self._report_provenance()
        payload = json.dumps(prov, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.report.write_text(
            "validation evidence" + eol + "PROVENANCE_JSON=" + payload,
            encoding="utf-8")
    def _seed(self, metrics=None, decision="rejected", model_bytes=b"v3-bytes"):
        self.pkl.write_bytes(model_bytes)
        model_registry.register_model(self.pkl, meta={"n_train": 10},
                                      snapshot_provenance=TestModelRegistry.FROZEN_PROV)
        # 绑特征协议，让 verify_approval 能推进到 validation 那一步（否则先被
        # feature_protocol:no_protocol 拦下，测不到「降级不松动展示门」的真正含义）
        TestPreregDegradation.PROTO = model_registry.make_feature_protocol(
            ["a"], masking=model_registry.B1_MASKING_PROTOCOL)
        model_registry.bind_feature_protocol(self.pkl.name, self.PROTO)
        self._write_report()
        m = metrics if metrics is not None else {
            "1": {"rank_ic": 0.005, "ric_ci": [-0.066, 0.07], "decision": "rejected"},
            "3": {"rank_ic": 0.002, "ric_ci": [-0.061, 0.066], "decision": "rejected"},
            "5": {"rank_ic": 0.08, "ric_ci": [0.017, 0.142], "decision": "approved"},
        }
        ok = model_registry.bind_validation(
            self.pkl.name, str(self.report), decision, metrics=m)
        self.assertTrue(ok)
        return model_registry._file_sha256(self.report)
    def _prereg(self, report_sha, expiry="2099-01-01", enabled=True,
                rule_version="prereg_shadow_v1", model_sha=None):
        reg = model_registry.load_registry()
        sha = model_sha or reg["models"][self.pkl.name]["sha256"]
        self.prereg.write_text(json.dumps({
            "rule_version": rule_version, "enabled": enabled,
            "grants": {self.pkl.name: {
                "model_sha256": sha, "report_sha256": report_sha,
                "expiry": expiry}}}, ensure_ascii=False), encoding="utf-8")

    def test_happy_path_and_scope_isolation(self):
        digest = self._seed()
        self._prereg(digest)
        ok, reason = model_registry.evaluate_prereg_degradation(
            self.pkl.name, prereg_path=self.prereg)
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "prereg_degraded")
        # 隔离性：降级授权不得松动对外展示门（validation/prompt 未过 → 仍拒）
        va, vreason = model_registry.verify_approval(self.pkl, self.PROTO)
        self.assertFalse(va)
        self.assertIn("validation", vreason)

    def test_no_grant_for_model(self):
        digest = self._seed()
        self._prereg(digest)
        ok, reason = model_registry.evaluate_prereg_degradation(
            "_other.pkl", prereg_path=self.prereg)
        self.assertFalse(ok)
        self.assertEqual(reason, "prereg_no_grant_for_model")

    def test_missing_file_and_disabled(self):
        ok, reason = model_registry.evaluate_prereg_degradation(
            self.pkl.name, prereg_path=self.tmp / "nope.json")
        self.assertFalse(ok)
        self.assertEqual(reason, "prereg_file_missing_or_unreadable")
        digest = self._seed()
        self._prereg(digest, enabled=False)
        ok2, r2 = model_registry.evaluate_prereg_degradation(
            self.pkl.name, prereg_path=self.prereg)
        self.assertFalse(ok2)
        self.assertEqual(r2, "prereg_not_enabled")

    def test_model_sha_mismatch(self):
        digest = self._seed()
        # 预注册登记了错误的 model_sha256（授权与当前 registry 条目脱钩）。
        # 注：真实文件篡改由 verify_model / load_models 拦（另有测例），
        # 本函数职责是「registry 记录值 vs 预注册登记值」的一致性。
        self._prereg(digest, model_sha="0" * 64)
        ok, reason = model_registry.evaluate_prereg_degradation(
            self.pkl.name, prereg_path=self.prereg)
        self.assertFalse(ok)
        self.assertEqual(reason, "model_sha_mismatch_vs_prereg")

    def test_report_sha_mismatch(self):
        digest = self._seed()
        self._prereg("f" * 64)   # 预注册绑了一份不是当前 validation 的报告 hash
        ok, reason = model_registry.evaluate_prereg_degradation(
            self.pkl.name, prereg_path=self.prereg)
        self.assertFalse(ok)
        self.assertEqual(reason, "report_sha_mismatch_vs_prereg")
        self.assertNotEqual(digest, "f" * 64)

    def test_t5_ci_lower_bound_must_be_positive(self):
        m = {"1": {"rank_ic": 0.005, "decision": "approved"},
             "3": {"rank_ic": 0.002, "decision": "approved"},
             "5": {"rank_ic": 0.05, "ric_ci": [-0.01, 0.11],
                   "decision": "approved"}}
        digest = self._seed(metrics=m)
        self._prereg(digest)
        ok, reason = model_registry.evaluate_prereg_degradation(
            self.pkl.name, prereg_path=self.prereg)
        self.assertFalse(ok)
        self.assertEqual(reason, "t5_ci_lower_bound_not_positive")

    def test_t5_not_approved(self):
        m = {"1": {"rank_ic": 0.01}, "3": {"rank_ic": 0.01},
             "5": {"rank_ic": 0.08, "ric_ci": [0.017, 0.142],
                   "decision": "rejected"}}
        digest = self._seed(metrics=m)
        self._prereg(digest)
        ok, reason = model_registry.evaluate_prereg_degradation(
            self.pkl.name, prereg_path=self.prereg)
        self.assertFalse(ok)
        self.assertEqual(reason, "t5_not_approved")

    def test_t1_negative_ic_rejected(self):
        m = {"1": {"rank_ic": -0.01, "decision": "rejected"},
             "3": {"rank_ic": 0.002, "decision": "rejected"},
             "5": {"rank_ic": 0.08, "ric_ci": [0.017, 0.142],
                   "decision": "approved"}}
        digest = self._seed(metrics=m)
        self._prereg(digest)
        ok, reason = model_registry.evaluate_prereg_degradation(
            self.pkl.name, prereg_path=self.prereg)
        self.assertFalse(ok)
        self.assertEqual(reason, "t1_rank_ic_negative")

    def test_expiry_boundary(self):
        digest = self._seed()
        self._prereg(digest, expiry="2026-10-12")
        ok, _ = model_registry.evaluate_prereg_degradation(
            self.pkl.name, prereg_path=self.prereg, today="2026-10-12")
        self.assertTrue(ok)                        # 到期当日仍有效
        ok2, r2 = model_registry.evaluate_prereg_degradation(
            self.pkl.name, prereg_path=self.prereg, today="2026-10-13")
        self.assertFalse(ok2)
        self.assertEqual(r2, "prereg_expired")     # 次日自动阻断，无静默续期

    def test_rule_version_mismatch(self):
        digest = self._seed()
        self._prereg(digest, rule_version="prereg_shadow_v0")
        ok, reason = model_registry.evaluate_prereg_degradation(
            self.pkl.name, prereg_path=self.prereg)
        self.assertFalse(ok)
        self.assertEqual(reason, "prereg_rule_version_mismatch_file")


if __name__ == "__main__":
    unittest.main()
