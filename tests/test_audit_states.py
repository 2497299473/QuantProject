"""V4-A（2026-09-17）：审计四层状态机。

背景：旧版 `audit_project.py` 把「0 FAIL」直接打印成 `PRODUCTION: NOT BLOCKED`，
而当时 `model_ready=false` / `history_validated=false` 恰恰是靠这两项为假才判 PASS
（检查项语义是「配置与未获批状态一致」）。于是审计读到的其实是
「配置与未获批状态一致」，却被表述成「生产可用」。

本测试把两条不变式钉死：
1. 四层状态互相独立，`audit_health` 好 ≠ `production_status` 可用；
2. `production_status == READY` 必须同时满足 model_ready ∧ history_validated
   ∧ audit_health != FAIL。
"""
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import audit_project as ap                                 # noqa: E402


class TestDerivedStates(unittest.TestCase):
    def setUp(self):
        self._cfg = ap._config
        self._models = ap._models

    def tearDown(self):
        ap._config = self._cfg
        ap._models = self._models

    def _inject(self, model_ready, history_validated, promotion="blocked"):
        ap._config = lambda: {
            "forecast": {"model_ready": model_ready},
            "decision": {"gates": {"history_validated": history_validated}},
        }
        ap._models = lambda: {"v3": {"promotion": {"status": promotion}}}

    def _audit(self, *statuses):
        a = ap.Audit()
        for i, s in enumerate(statuses):
            a.add(f"T{i}", "P0-研究有效性", "占位检查", s)
        return a

    # --- 当前真实状态（回归本轮的 P0 问题）--------------------------------
    def test_current_state_blocked_despite_zero_fail(self):
        self._inject(False, False)
        st = ap.derive_states(self._audit("PASS", "PASS", "WARN"))
        self.assertEqual(st["audit_health"], "PASS_WITH_WARNINGS")
        self.assertEqual(st["model_promotion"], "BLOCKED")
        self.assertEqual(st["action_enable"], "HOLD_ONLY")
        self.assertEqual(st["production_status"], "BLOCKED",
                         "0 FAIL 不得推出生产可用")

    def test_zero_fail_alone_never_yields_ready(self):
        # 全部 PASS、且没有任何 WARN —— 仍不足以 READY（模型未获批）
        self._inject(False, False)
        st = ap.derive_states(self._audit("PASS", "PASS"))
        self.assertEqual(st["audit_health"], "PASS")
        self.assertEqual(st["production_status"], "BLOCKED")

    # --- READY 的充分条件必须是三者同时满足 -------------------------------
    def test_ready_requires_all_three(self):
        self._inject(True, True, promotion="approved")
        st = ap.derive_states(self._audit("PASS"))
        self.assertEqual(st["model_promotion"], "APPROVED")
        self.assertEqual(st["action_enable"], "ENABLED")
        self.assertEqual(st["production_status"], "READY")

    def test_fail_audit_blocks_production_even_if_gates_approved(self):
        self._inject(True, True, promotion="approved")
        st = ap.derive_states(self._audit("PASS", "FAIL"))
        self.assertEqual(st["audit_health"], "FAIL")
        self.assertEqual(st["production_status"], "BLOCKED")

    def test_model_ready_with_blocked_registry_is_blocked(self):
        self._inject(True, True, promotion="blocked")
        st = ap.derive_states(self._audit("PASS"))
        self.assertEqual(st["model_promotion"], "BLOCKED")
        self.assertEqual(st["production_status"], "BLOCKED")

    # --- 门禁缺失比「明确锁死」更严 ---------------------------------------
    def test_missing_gate_is_blocked_not_hold_only(self):
        ap._config = lambda: {"forecast": {"model_ready": True},
                              "decision": {"gates": {}}}
        ap._models = lambda: {"v3": {"promotion": {"status": "approved"}}}
        st = ap.derive_states(self._audit("PASS"))
        self.assertEqual(st["action_enable"], "BLOCKED")
        self.assertEqual(st["production_status"], "BLOCKED")


class TestJsonCompatibility(unittest.TestCase):
    """V4 明确不做 breaking change：旧字段保留 + 新字段增加 + schema_version。"""

    def test_source_declares_legacy_and_new_fields(self):
        src = (BASE_DIR / "audit_project.py").read_text(encoding="utf-8")
        self.assertIn('"schema_version": "2.0"', src)
        for key in ('"production_status"', '"audit_health"',
                    '"model_promotion"', '"action_enable"'):
            self.assertIn(key, src)
        # 旧字段必须仍在（消费方兼容），且带兼容说明
        self.assertIn('"production":', src)
        self.assertIn("legacy_note", src)

    def test_production_field_semantics_unchanged(self):
        # 旧字段语义 = FAIL 计数，不得被改写成四层状态
        src = (BASE_DIR / "audit_project.py").read_text(encoding="utf-8")
        self.assertIn('"production": "BLOCKED" if n[FAIL] else "NOT BLOCKED"', src)


if __name__ == "__main__":
    unittest.main()
