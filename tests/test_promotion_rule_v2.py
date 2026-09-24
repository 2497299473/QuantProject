"""B++-3（2026-09-23）：derive_promotion rule v2 contract tests。

fast 层纪律：纯函数 + 合成证据，零网络；registry 触点全程 tempdir 隔离
（同 test_model_registry 惯例）。钉死六条：
1) 签名/纯函数性不变：同输入必同输出；
2) 五门顺序判据链 power → performance → baseline_edge → calibration → provenance；
3) 契约 B 五反例全部不得 APPROVED，且各落到对应 blocked_* 门；
4) 功效阈值冻结前 APPROVED 结构性不可达（power.frozen=False → blocked_power）；
5) legacy 兼容：rejected → blocked（含 failed_horizons）；全过 → research_only；
   无依据 → pending；
6) B++-4 自动继承：手填 promotion=approved 撞上重推导非 approved →
   verify_approval 拒（promotion_evidence_inconsistent）——手写批准过不了纯函数。
"""
from __future__ import annotations

import inspect
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import model_registry                     # noqa: E402
from core import validation_schema as S             # noqa: E402


def _full_pass_evidence_v2(power_frozen: bool = True) -> dict:
    """五门全过的 schema v2 证据（power_frozen=False 用于反例：阈值未冻结）。"""
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
    ev["power"]["frozen"] = S.ev_ok(power_frozen)
    ev["power"]["n_power_fund"] = S.ev_ok(100)
    for k in ev["provenance"]:
        ev["provenance"][k] = S.ev_ok(f"{k}-ok")
    return ev


class TestFullPassAndPurity(unittest.TestCase):
    def test_full_pass_evidence_approves(self):
        d = model_registry.derive_promotion(
            {"decision": "approved", "evidence": _full_pass_evidence_v2()})
        self.assertEqual(d["status"], "approved")
        self.assertEqual(d["rule_version"], 2)
        self.assertIn("五门全过", d["reason"])

    def test_pure_function_same_input_same_output(self):
        v = {"decision": "approved", "evidence": _full_pass_evidence_v2()}
        self.assertEqual(model_registry.derive_promotion(v),
                         model_registry.derive_promotion(v))

    def test_signature_unchanged(self):
        """契约 B §12：纯函数签名 derive_promotion(validation) 不得变。"""
        params = list(inspect.signature(model_registry.derive_promotion).parameters)
        self.assertEqual(params, ["validation"])


class TestContractBAntiExamples(unittest.TestCase):
    """契约 B 五反例：每一个都必须被对应 blocked_* 门拦下，不得 APPROVED。"""

    def _derive(self, ev):
        return model_registry.derive_promotion({"decision": "approved", "evidence": ev})

    def test_a_pooled_pass_fund_edge_fail(self):
        """反例 1：pooled PASS + 基金 edge FAIL → blocked_baseline_edge。"""
        ev = _full_pass_evidence_v2()
        ev["funds"]["002112"]["5"]["decision_edge_ci"] = S.ev_ok([-0.09, 0.05])
        d = self._derive(ev)
        self.assertEqual(d["status"], "blocked_baseline_edge")
        self.assertIn("002112", d["reason"])

    def test_b_pooled_pass_fund_n_below_power(self):
        """反例 2：pooled PASS + 025687 n=35 < N_POWER → blocked_power。"""
        ev = _full_pass_evidence_v2()
        ev["funds"]["025687"]["5"]["n"] = S.ev_ok(35)
        self.assertEqual(self._derive(ev)["status"], "blocked_power")

    def test_b2_fund_insufficient_power_status(self):
        """反例 2'：基金节点整槽 INSUFFICIENT_POWER（B++-2 产出形态）→ blocked_power。"""
        ev = _full_pass_evidence_v2()
        ev["funds"]["025687"]["5"] = {
            k: (S.ev_ok(35) if k == "n" else S.ev_na(S.STATUS_INSUFFICIENT_POWER))
            for k in ev["funds"]["025687"]["5"]}
        d = self._derive(ev)
        self.assertEqual(d["status"], "blocked_power")
        self.assertIn("INSUFFICIENT_POWER", d["reason"])

    def test_c_power_not_frozen_structurally_unreachable(self):
        """反例 3a：功效阈值未冻结 → blocked_power（APPROVED 结构性不可达）。"""
        d = self._derive(_full_pass_evidence_v2(power_frozen=False))
        self.assertEqual(d["status"], "blocked_power")
        self.assertIn("结构性不可达", d["reason"])

    def test_d_edge_ci_crosses_zero(self):
        """反例 3b：IC > est_chg 但 pooled edge CI 跨零 → blocked_baseline_edge。"""
        ev = _full_pass_evidence_v2()
        ev["pooled"]["5"]["decision_edge"] = S.ev_ok(0.0615)
        ev["pooled"]["5"]["decision_edge_ci"] = S.ev_ok([-0.042, 0.169])
        d = self._derive(ev)
        self.assertEqual(d["status"], "blocked_baseline_edge")
        self.assertIn("pooled T+5", d["reason"])

    def test_e_calibration_fail(self):
        """反例 4：calibration FAIL → blocked_calibration。"""
        ev = _full_pass_evidence_v2()
        ev["pooled"]["3"][S.MIDPOINT_CALIBRATION_ERROR_KEY] = S.ev_ok(0.30)
        self.assertEqual(self._derive(ev)["status"], "blocked_calibration")

    def test_f_provenance_incomplete(self):
        """反例 5：provenance 缺口（UNKNOWN）→ blocked_provenance。"""
        ev = _full_pass_evidence_v2()
        ev["provenance"]["git_commit"] = S.ev_na(S.STATUS_UNKNOWN)
        self.assertEqual(self._derive(ev)["status"], "blocked_provenance")

    def test_g_malformed_evidence_fail_closed(self):
        """证据形态损坏（缺整块）→ fail-closed blocked_provenance，不 pending 不 approved。"""
        ev = _full_pass_evidence_v2()
        del ev["power"]
        self.assertEqual(self._derive(ev)["status"], "blocked_provenance")

    def test_h_horizon_subset_blocked_performance(self):
        """周期子集（某周期没跑 = UNKNOWN）→ blocked_performance 不可核验。"""
        ev = _full_pass_evidence_v2()
        ev["pooled"]["3"] = S.metric_node()
        self.assertEqual(self._derive(ev)["status"], "blocked_performance")

    def test_i_rejected_decision_blocked_regardless(self):
        d = model_registry.derive_promotion(
            {"decision": "rejected", "evidence": _full_pass_evidence_v2()})
        self.assertEqual(d["status"], "blocked")


class TestLegacyCompat(unittest.TestCase):
    def test_rejected_legacy_blocked_with_failed_horizons(self):
        metrics = {
            "1": {"ric_ci": [-0.066, 0.07], "decision": "rejected"},
            "3": {"ric_ci": [-0.061, 0.066], "decision": "rejected"},
            "5": {"ric_ci": [0.017, 0.142], "decision": "approved"},
        }
        d = model_registry.derive_promotion({"decision": "rejected", "metrics": metrics})
        self.assertEqual(d["status"], "blocked")
        self.assertEqual(d["failed_horizons"], ["1", "3"])
        self.assertIn("未全过", d["reason"])

    def test_all_approved_legacy_research_only(self):
        metrics = {str(h): {"ric_ci": [0.01, 0.05], "decision": "approved"}
                   for h in (1, 3, 5)}
        d = model_registry.derive_promotion({"decision": "approved", "metrics": metrics})
        self.assertEqual(d["status"], "research_only")

    def test_no_basis_pending(self):
        self.assertEqual(model_registry.derive_promotion(None)["status"], "pending")
        self.assertEqual(model_registry.derive_promotion(
            {"decision": "approved"})["status"], "pending")


class TestRegistryIntegration(unittest.TestCase):
    """apply_promotion / verify_approval 与 rule v2 的接线（tempdir 隔离真实 registry）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_registry_path = model_registry.REGISTRY_PATH   # 先存 Path 原值
        self._orig_registry = (model_registry.REGISTRY_PATH.read_text(encoding="utf-8")
                               if model_registry.REGISTRY_PATH.exists() else None)
        self._orig_models_dir = model_registry.MODELS_DIR
        model_registry.MODELS_DIR = self.tmp
        model_registry.REGISTRY_PATH = self.tmp / "registry.json"

    def tearDown(self):
        model_registry.MODELS_DIR = self._orig_models_dir
        # 先恢复 REGISTRY_PATH 属性本身，再回写内容——否则属性留在已删的
        # 临时目录上，泄漏进后加载的测试模块（本轮 12 连红的根因）。
        model_registry.REGISTRY_PATH = self._orig_registry_path
        if self._orig_registry is not None:
            model_registry.REGISTRY_PATH.write_text(self._orig_registry, encoding="utf-8")
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, evidence):
        pkl = self.tmp / "_v2_gate_model.pkl"
        pkl.write_bytes(b"b3-gate-bytes")
        model_registry.register_model(pkl, meta={}, snapshot_provenance={
            "snapshot_file": "samples_frozen_20260910.jsonl",
            "samples_sha256_lf": "ab" * 32, "kfp_recorded_sha256": "cd" * 32,
            "kfp_current_sha256": "cd" * 32, "kfp_comparability": "SAME"})
        proto = model_registry.make_feature_protocol(
            ["a"], masking=model_registry.B1_MASKING_PROTOCOL)
        self.assertTrue(model_registry.bind_feature_protocol(pkl.name, proto))
        entry = model_registry.get_model_entry(pkl.name)
        provenance = {
            "validation_mode": "ARTIFACT",
            "artifact_sha256": entry["sha256"],
            "dataset_sha256": entry["snapshot_provenance"]["samples_sha256_lf"],
            "git_commit": entry["git_commit"],
        }
        report = self.tmp / "report.log"
        payload = json.dumps(provenance, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        report.write_text("evidence" + "\\r\\n" + "PROVENANCE_JSON=" + payload, encoding="utf-8")
        self.assertTrue(model_registry.bind_validation(
            pkl.name, str(report), "approved",
            {str(h): {"decision": "approved", "ric_ci": [0.01, 0.05]}
             for h in (1, 3, 5)}))
        reg = model_registry.load_registry()
        reg["models"][pkl.name]["validation"]["evidence"] = evidence
        self.assertTrue(model_registry._save_registry(reg))
        return pkl, proto
    def test_apply_promotion_writes_v2_approved_and_verify_passes(self):
        pkl, proto = self._seed(_full_pass_evidence_v2())
        written, d = model_registry.apply_promotion(pkl.name)
        self.assertTrue(written)
        self.assertEqual(d["status"], "approved")
        promo = model_registry.get_model_entry(pkl.name)["promotion"]
        self.assertEqual(promo["status"], "approved")
        self.assertEqual(promo["rule_version"], 2)
        self.assertEqual(promo["derived_by"], "derive_promotion")
        ok, reason = model_registry.verify_approval(pkl, proto)
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "ok")

    def test_apply_promotion_blocked_power_for_unfrozen(self):
        pkl, _ = self._seed(_full_pass_evidence_v2(power_frozen=False))
        _, d = model_registry.apply_promotion(pkl.name)
        self.assertEqual(d["status"], "blocked_power")
        self.assertEqual(model_registry.get_model_entry(
            pkl.name)["promotion"]["status"], "blocked_power")

    def test_hand_forged_approved_cannot_survive_rederivation(self):
        """B++-4：verify_approval 的重推导一致性门自动继承 rule v2——
        手填 promotion=approved 撞上五门证据不过 → promotion_evidence_inconsistent。"""
        pkl, proto = self._seed(_full_pass_evidence_v2(power_frozen=False))
        self.assertTrue(model_registry.update_promotion(pkl.name, "approved", "手填伪造"))
        ok, reason = model_registry.verify_approval(pkl, proto)
        self.assertFalse(ok)
        self.assertEqual(reason, "promotion_evidence_inconsistent")


if __name__ == "__main__":
    unittest.main()
