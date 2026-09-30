# -*- coding: utf-8 -*-
"""D3-01（2026-09-29 面 3 审查）：report↔evidence **配对锚点**的绑定层测例。

A 单面 3 核心结论（已核实）：``bind_validation_evidence`` 全链只验 Identity proof
（证据声称属于这个模型/数据/commit），**Computation proof 为零**；且 report 与
evidence 是两个**无锚点**的独立文件，可各自伪造、任意搭配。伪造 report（抄
registry 三值）+ 伪造 evidence（数值全绿）即可让五门全过推出 ``approved``。

本模块钉住 A 单验收标准的两条硬要求：

1. **伪造组合被拒 + registry 零残留**：report 的 PROVENANCE_JSON 与 registry 血统
   一致（过 validate_validation_provenance），但 ``evidence_sha256`` 锚点与实际提交
   的 evidence 内容不符 ⇒ bind 拒，一字不写。
2. **旧路径死亡条款**：无锚点的旧格式 v2 evidence **不得再获 approved 推导**
   （derive_promotion 门 0 落 blocked_provenance），且不得再被 bind 接受。

纯函数 + tempdir 隔离 registry，零重依赖零网络 ⇒ fast 层。
锚点纯函数口径见 tests/test_evidence_anchor.py；真实复算管线见
tests/test_evidence_recompute.py（slow 层）。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import unittest.mock   # verify_approval A4 段测例以 mock 放行重推导，子模块须显式导入
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import model_registry                     # noqa: E402
from core import validation_schema as S             # noqa: E402
import bind_validation_evidence as bve              # noqa: E402

ANCHOR = model_registry._EVIDENCE_ANCHOR_KEY


def _full_pass_evidence_v2(actual_provenance: dict | None = None) -> dict:
    """五门全过的 schema v2 证据（与 test_promotion_rule_v2 同款夹具）。"""
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
    if actual_provenance is not None:
        for key in ("artifact_sha256", "dataset_sha256", "git_commit"):
            value = str(actual_provenance.get(key) or "").strip()
            if not value:
                raise AssertionError(f"fixture requires real provenance: {key}")
            ev["provenance"][key] = S.ev_ok(value)
    return ev


class _RegistrySandbox(unittest.TestCase):
    """tempdir 隔离 registry（同 test_promotion_rule_v2 / test_model_registry 惯例）。

    纪律（08-30 registry 污染教训）：MODELS_DIR + REGISTRY_PATH 一并重定向，
    全程不触碰真实 data/model_registry/registry.json。
    """

    FROZEN_PROV = {
        "snapshot_file": "samples_frozen_20260910.jsonl",
        "samples_sha256_lf": "ab" * 32,
        "kfp_recorded_sha256": "cd" * 32,
        "kfp_current_sha256": "cd" * 32,
        "kfp_comparability": "SAME",
    }

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_registry_path = model_registry.REGISTRY_PATH
        self._orig_registry = (model_registry.REGISTRY_PATH.read_text(encoding="utf-8")
                               if model_registry.REGISTRY_PATH.exists() else None)
        self._orig_models_dir = model_registry.MODELS_DIR
        model_registry.MODELS_DIR = self.tmp
        model_registry.REGISTRY_PATH = self.tmp / "registry.json"

    def tearDown(self):
        model_registry.MODELS_DIR = self._orig_models_dir
        model_registry.REGISTRY_PATH = self._orig_registry_path
        if self._orig_registry is not None:
            model_registry.REGISTRY_PATH.write_text(self._orig_registry,
                                                    encoding="utf-8")
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_model(self, name="_d301_anchor_model.pkl") -> Path:
        pkl = self.tmp / name
        pkl.write_bytes(b"d3-01-anchor-bytes")
        self.assertIsNotNone(model_registry.register_model(
            pkl, meta={}, snapshot_provenance=dict(self.FROZEN_PROV)))
        return pkl

    def _write_report(self, prov: dict) -> Path:
        report = self.tmp / "d301_report.log"
        report.write_text(
            "validation evidence\nPROVENANCE_JSON="
            + json.dumps(prov, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")),
            encoding="utf-8")
        return report

    def _entry_provenance(self, pkl: Path, anchor=None) -> dict:
        """report 侧 PROVENANCE_JSON：血统三值取自 registry（必然对账通过），
        anchor=None ⇒ 旧格式（无 evidence_sha256 键）。"""
        entry = model_registry.get_model_entry(pkl.name)
        prov = {
            "validation_mode": "ARTIFACT",
            "artifact_sha256": entry["sha256"],
            "dataset_sha256": entry["snapshot_provenance"]["samples_sha256_lf"],
            "git_commit": entry["git_commit"],
        }
        if anchor is not None:
            prov[ANCHOR] = anchor
        return prov


class TestBindRejectsForgedPairing(_RegistrySandbox):
    """验收①：伪造组合（血统对、锚点错）被 bind 拒且 registry 零残留。"""

    def test_report_without_anchor_key_refuses_binding(self):
        """旧格式 report（无 evidence_sha256 键）+ 五门全过 evidence ⇒ 拒绑。

        这正是 A 单描述的「两个独立文件各自提供即可绑过」路径——现已死亡。
        """
        pkl = self._seed_model()
        ev = _full_pass_evidence_v2()
        report = self._write_report(self._entry_provenance(pkl, anchor=None))
        self.assertFalse(model_registry.bind_validation(
            pkl.name, str(report), "approved", evidence=ev))
        self.assertNotIn("validation", model_registry.get_model_entry(pkl.name),
                         "拒绑后 registry 必须零残留")

    def test_anchor_mismatch_refuses_binding(self):
        """锚点是**另一份** evidence 的哈希（血统三值全对）⇒ 拒绑 + 零残留。"""
        pkl = self._seed_model()
        honest = _full_pass_evidence_v2()
        # 攻击者提交的 evidence：数值被改（全绿但 rank_ic 伪造）
        forged = json.loads(json.dumps(honest, ensure_ascii=False))
        forged["pooled"]["1"]["rank_ic"] = S.ev_ok(0.99)
        # report 里声明的锚点 = **诚实那份**的哈希（伪造者拿旧报告的锚点充数）
        report = self._write_report(
            self._entry_provenance(pkl, anchor=S.canonical_evidence_sha256(honest)))
        self.assertFalse(model_registry.bind_validation(
            pkl.name, str(report), "approved", evidence=forged))
        self.assertNotIn("validation", model_registry.get_model_entry(pkl.name))

    def test_anchor_placeholder_refuses_binding(self):
        """锚点填占位值（"0"*64）⇒ 拒绑（不得因"有键"就放行）。"""
        pkl = self._seed_model()
        ev = _full_pass_evidence_v2()
        report = self._write_report(self._entry_provenance(pkl, anchor="0" * 64))
        self.assertFalse(model_registry.bind_validation(
            pkl.name, str(report), "approved", evidence=ev))
        self.assertNotIn("validation", model_registry.get_model_entry(pkl.name))

    def test_anchor_empty_string_refuses_binding(self):
        pkl = self._seed_model()
        ev = _full_pass_evidence_v2()
        report = self._write_report(self._entry_provenance(pkl, anchor=""))
        self.assertFalse(model_registry.bind_validation(
            pkl.name, str(report), "approved", evidence=ev))
        self.assertNotIn("validation", model_registry.get_model_entry(pkl.name))

    def test_matching_anchor_binds_and_stores_anchor(self):
        """对照正例：锚点与 evidence 内容一致 ⇒ 可绑，且锚点入库可审计。"""
        pkl = self._seed_model()
        ev = _full_pass_evidence_v2()
        anchor = S.canonical_evidence_sha256(ev)
        report = self._write_report(self._entry_provenance(pkl, anchor=anchor))
        self.assertTrue(model_registry.bind_validation(
            pkl.name, str(report), "approved", evidence=ev))
        validation = model_registry.get_model_entry(pkl.name)["validation"]
        self.assertEqual(validation[ANCHOR], anchor)
        self.assertEqual(validation["evidence"]["schema_version"], 2)

    def test_legacy_bind_without_evidence_still_works(self):
        """回归：不提交 v2 evidence 的 legacy 绑定不受锚点门影响（无证据可锚）。"""
        pkl = self._seed_model()
        report = self._write_report(self._entry_provenance(pkl, anchor=None))
        self.assertTrue(model_registry.bind_validation(
            pkl.name, str(report), "rejected",
            metrics={str(h): {"decision": "rejected", "ric_ci": [-0.06, 0.07]}
                     for h in (1, 3, 5)}))
        self.assertNotIn(ANCHOR,
                         model_registry.get_model_entry(pkl.name)["validation"])


class TestOldPathDeathInDerive(_RegistrySandbox):
    """验收③ 语义迁移「旧路径死亡」：无锚点的旧格式 evidence 不得再获 approved。"""

    def test_unanchored_v2_evidence_cannot_reach_approved(self):
        """旧格式 {decision, evidence}（五门数值全绿）⇒ blocked_provenance。

        这是 D3-01 的核心语义迁移：改前该形态直接推出 approved（A 单已核实），
        改后结构性不可达。
        """
        ev = _full_pass_evidence_v2()
        d = model_registry.derive_promotion({"decision": "approved", "evidence": ev})
        self.assertEqual(d["status"], "blocked_provenance")
        self.assertEqual(d["rule_version"], 2)
        self.assertIn("evidence_anchor_missing", d["reason"])
        self.assertIn("不得再获 approved", d["reason"])

    def test_mismatched_anchor_cannot_reach_approved(self):
        ev = _full_pass_evidence_v2()
        d = model_registry.derive_promotion({
            "decision": "approved", "evidence": ev, ANCHOR: "f" * 64})
        self.assertEqual(d["status"], "blocked_provenance")
        self.assertIn("evidence_anchor_mismatch", d["reason"])

    def test_anchored_evidence_reaches_approved(self):
        """新路径成立：带正确锚点的同一份证据仍可 approved（迁移不砍正常通道）。"""
        ev = _full_pass_evidence_v2()
        d = model_registry.derive_promotion({
            "decision": "approved", "evidence": ev,
            ANCHOR: S.canonical_evidence_sha256(ev)})
        self.assertEqual(d["status"], "approved")
        self.assertIn("五门全过", d["reason"])

    def test_gate0_precedes_five_gates(self):
        """门 0 先于五门：锚点不符时不得落到 blocked_power 等下游门（门序钉死）。"""
        ev = _full_pass_evidence_v2()
        ev["power"]["frozen"] = S.ev_ok(False)     # 同时踩门 1（功效未冻结）
        d = model_registry.derive_promotion({"decision": "approved", "evidence": ev})
        self.assertEqual(d["status"], "blocked_provenance")
        self.assertNotIn("blocked_power", d["status"])

    def test_legacy_paths_unaffected_by_gate0(self):
        """legacy 绑定（无 v2 evidence 块）不进门 0：rejected→blocked、全过→research_only。"""
        metrics = {str(h): {"ric_ci": [0.01, 0.05], "decision": "approved"}
                   for h in (1, 3, 5)}
        self.assertEqual(model_registry.derive_promotion(
            {"decision": "approved", "metrics": metrics})["status"], "research_only")
        rej = {str(h): {"ric_ci": [-0.06, 0.07], "decision": "rejected"}
               for h in (1, 3, 5)}
        self.assertEqual(model_registry.derive_promotion(
            {"decision": "rejected", "metrics": rej})["status"], "blocked")
        self.assertEqual(model_registry.derive_promotion(None)["status"], "pending")


class TestApplyPromotionWritesBlockedForUnanchored(_RegistrySandbox):
    """apply_promotion 落盘层：无锚点证据写入的 promotion 必为非 approved。"""

    def test_apply_promotion_after_unanchored_bind_is_blocked(self):
        pkl = self._seed_model()
        ev = _full_pass_evidence_v2()
        anchor = S.canonical_evidence_sha256(ev)
        report = self._write_report(self._entry_provenance(pkl, anchor=anchor))
        self.assertTrue(model_registry.bind_validation(
            pkl.name, str(report), "approved", evidence=ev))
        # 模拟攻击者手改 registry：抹掉锚点（保留五门全绿 evidence）
        reg = model_registry.load_registry()
        reg["models"][pkl.name]["validation"].pop(ANCHOR, None)
        self.assertTrue(model_registry._save_registry(reg))
        written, derived = model_registry.apply_promotion(pkl.name)
        self.assertTrue(written)
        self.assertEqual(derived["status"], "blocked_provenance")
        self.assertEqual(model_registry.get_model_entry(
            pkl.name)["promotion"]["status"], "blocked_provenance")

    def test_verify_approval_refuses_after_anchor_removed(self):
        """端到端：抹掉锚点后 verify_approval 必拒（授权门不认无锚点证据）。"""
        pkl = self._seed_model()
        proto = model_registry.make_feature_protocol(
            ["a"], masking=model_registry.B1_MASKING_PROTOCOL)
        self.assertTrue(model_registry.bind_feature_protocol(pkl.name, proto))
        ev = _full_pass_evidence_v2()
        anchor = S.canonical_evidence_sha256(ev)
        report = self._write_report(self._entry_provenance(pkl, anchor=anchor))
        self.assertTrue(model_registry.bind_validation(
            pkl.name, str(report), "approved", evidence=ev))
        model_registry.apply_promotion(pkl.name)
        ok, reason = model_registry.verify_approval(pkl, proto)
        self.assertTrue(ok, reason)          # 基线绿
        reg = model_registry.load_registry()
        reg["models"][pkl.name]["validation"].pop(ANCHOR, None)
        self.assertTrue(model_registry._save_registry(reg))
        ok2, reason2 = model_registry.verify_approval(pkl, proto)
        self.assertFalse(ok2)
        self.assertEqual(reason2, "promotion_evidence_inconsistent")


class TestVerifyApprovalAnchorReportRecheck(_RegistrySandbox):
    """A4 段锚点复核：registry 存的锚点必须等于 report 实际内容里的锚点。

    防「改 registry 内 evidence + 同步改存储锚点」后拿旧 report 充数，
    也防换 report 文件伪造配对。
    """

    def test_stored_anchor_diverging_from_report_is_refused(self):
        pkl = self._seed_model()
        proto = model_registry.make_feature_protocol(
            ["a"], masking=model_registry.B1_MASKING_PROTOCOL)
        self.assertTrue(model_registry.bind_feature_protocol(pkl.name, proto))
        ev = _full_pass_evidence_v2()
        anchor = S.canonical_evidence_sha256(ev)
        report = self._write_report(self._entry_provenance(pkl, anchor=anchor))
        self.assertTrue(model_registry.bind_validation(
            pkl.name, str(report), "approved", evidence=ev))
        model_registry.apply_promotion(pkl.name)
        # 攻击：registry 内 evidence + 存储锚点一起改（自洽），但 report 没换。
        # promotion 账面仍 approved（不重跑 apply_promotion），mock 重推导放行，
        # 专测 A4 段的报告复核（同 test_promotion_rule_v2.test_07 手法）。
        reg = model_registry.load_registry()
        node = reg["models"][pkl.name]["validation"]
        node[ANCHOR] = "e" * 64
        self.assertTrue(model_registry._save_registry(reg))
        with unittest.mock.patch.object(
                model_registry, "derive_promotion",
                return_value={"status": "approved", "rule_version": 2,
                              "failed_horizons": [], "reason": "mock"}):
            ok, reason = model_registry.verify_approval(pkl, proto)
        self.assertFalse(ok)
        self.assertEqual(reason, "evidence_anchor_report_mismatch")

    def test_anchor_consistent_with_report_passes(self):
        pkl = self._seed_model()
        proto = model_registry.make_feature_protocol(
            ["a"], masking=model_registry.B1_MASKING_PROTOCOL)
        self.assertTrue(model_registry.bind_feature_protocol(pkl.name, proto))
        ev = _full_pass_evidence_v2()
        report = self._write_report(
            self._entry_provenance(pkl, anchor=S.canonical_evidence_sha256(ev)))
        self.assertTrue(model_registry.bind_validation(
            pkl.name, str(report), "approved", evidence=ev))
        model_registry.apply_promotion(pkl.name)
        ok, reason = model_registry.verify_approval(pkl, proto)
        self.assertTrue(ok, reason)


class TestCliGateARefusesForgedPairing(_RegistrySandbox):
    """CLI 入口（bind_validation_evidence）门 A：伪造配对在 bind 之前即被拒。

    门 A 失败发生在门 B（复算，重依赖）之前 ⇒ 本类可在 fast 层跑通，
    不触发 numpy/sklearn 导入，也不碰真实冻结件。
    """

    def _run_cli(self, report: Path, evidence: Path, model: str) -> int:
        old_argv = list(sys.argv)
        try:
            sys.argv = ["bind_validation_evidence.py",
                        "--report", str(report),
                        "--evidence", str(evidence),
                        "--model", model]
            return bve.main()
        finally:
            sys.argv = old_argv

    def test_cli_rejects_report_without_anchor(self):
        pkl = self._seed_model()
        ev = _full_pass_evidence_v2(actual_provenance={
            "artifact_sha256": model_registry.get_model_entry(pkl.name)["sha256"],
            "dataset_sha256": self.FROZEN_PROV["samples_sha256_lf"],
            "git_commit": model_registry.get_model_entry(pkl.name)["git_commit"],
        })
        report = self._write_report(self._entry_provenance(pkl, anchor=None))
        ev_path = self.tmp / "forged_evidence.json"
        ev_path.write_text(json.dumps(ev, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        rc = self._run_cli(report, ev_path, pkl.name)
        self.assertEqual(rc, 2)
        self.assertNotIn("validation", model_registry.get_model_entry(pkl.name),
                         "CLI 拒绑后 registry 必须零残留")

    def test_cli_rejects_anchor_of_different_evidence(self):
        """A 单验收①原形：血统对、数值被改、锚点是诚实件的 ⇒ rc=2 + 零残留。"""
        pkl = self._seed_model()
        entry = model_registry.get_model_entry(pkl.name)
        real_prov = {"artifact_sha256": entry["sha256"],
                     "dataset_sha256": self.FROZEN_PROV["samples_sha256_lf"],
                     "git_commit": entry["git_commit"]}
        honest = _full_pass_evidence_v2(actual_provenance=real_prov)
        forged = json.loads(json.dumps(honest, ensure_ascii=False))
        forged["pooled"]["1"]["rank_ic"] = S.ev_ok(0.99)
        forged["pooled"]["1"]["rank_ic_ci"] = S.ev_ok([0.5, 0.9])
        report = self._write_report(self._entry_provenance(
            pkl, anchor=S.canonical_evidence_sha256(honest)))
        ev_path = self.tmp / "forged_evidence.json"
        ev_path.write_text(json.dumps(forged, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        rc = self._run_cli(report, ev_path, pkl.name)
        self.assertEqual(rc, 2)
        self.assertNotIn("validation", model_registry.get_model_entry(pkl.name))

    def test_cli_rejects_unreadable_evidence_before_gate_a(self):
        pkl = self._seed_model()
        report = self._write_report(self._entry_provenance(pkl, anchor="a" * 64))
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual(self._run_cli(report, bad, pkl.name), 2)
        self.assertNotIn("validation", model_registry.get_model_entry(pkl.name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
