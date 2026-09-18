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

    def _inject(self, model_ready, history_validated, promotion="blocked",
                active="v3", models=None):
        ap._config = lambda: {
            "forecast": {"model_ready": model_ready, "active_model": active},
            "decision": {"gates": {"history_validated": history_validated}},
        }
        ap._models = lambda: models if models is not None \
            else {active: {"promotion": {"status": promotion}}}

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
        """active 自身 blocked ⇒ BLOCKED（语义收紧后：判据是 active，不是全量）。"""
        self._inject(True, True, promotion="blocked")
        st = ap.derive_states(self._audit("PASS"))
        self.assertEqual(st["model_promotion"], "BLOCKED")
        self.assertEqual(st["production_status"], "BLOCKED")
        self.assertEqual(st["inputs"]["promotion_basis"], "active_promotion_blocked")

    # --- 门禁缺失比「明确锁死」更严 ---------------------------------------
    def test_missing_gate_is_blocked_not_hold_only(self):
        ap._config = lambda: {"forecast": {"model_ready": True, "active_model": "v3"},
                              "decision": {"gates": {}}}
        ap._models = lambda: {"v3": {"promotion": {"status": "approved"}}}
        st = ap.derive_states(self._audit("PASS"))
        self.assertEqual(st["action_enable"], "BLOCKED")
        self.assertEqual(st["production_status"], "BLOCKED")


# --- V4.1 ②（2026-09-18）：active / historical 分离 -------------------------
# 背景：旧实现把「registry 里所有 blocked 模型」当否决权。历史失败记录
# （v2/v3）会永久阻止未来模型晋升：v4 即便 approved，只要 v2 还留在 registry，
# model_promotion 仍是 BLOCKED。下面的测例钉住新语义：只看 active。

class TestActiveVsHistoricalModel(unittest.TestCase):
    """resolve_model_promotion 纯函数 + derive_states 集成的回归测例。"""

    def setUp(self):
        self._cfg = ap._config
        self._models = ap._models

    def tearDown(self):
        ap._config = self._cfg
        ap._models = self._models

    MODELS = {
        "forecast_v2.pkl": {"promotion": {"status": "blocked"}},   # 历史：08 月失败
        "forecast_v3.pkl": {"promotion": {"status": "blocked"}},   # 历史：08 月失败
        "forecast_v4.pkl": {"promotion": {"status": "approved"}},  # 新模型：证据合格
    }

    def _states(self, ready, active, models=None, audit=("PASS",)):
        ap._config = lambda: {
            "forecast": {"model_ready": ready, "active_model": active},
            "decision": {"gates": {"history_validated": True}},
        }
        ap._models = lambda: models if models is not None else self.MODELS
        a = ap.Audit()
        for i, s in enumerate(audit):
            a.add(f"T{i}", "P0-研究有效性", "占位", s)
        return ap.derive_states(a)

    # --- 纯函数层 -----------------------------------------------------------
    def test_pure_historical_blocked_does_not_veto_active(self):
        """回归 ② 主缺陷：历史 blocked 不得否决 active=approved。"""
        status, basis = ap.resolve_model_promotion(
            "forecast_v4.pkl", self.MODELS, model_ready=True)
        self.assertEqual(status, "APPROVED")
        self.assertEqual(basis["historical_blocked"],
                         ["forecast_v2.pkl", "forecast_v3.pkl"],
                         "历史 blocked 必须仍可见（档案事实不得被抹掉）")
        self.assertEqual(basis["all_blocked"], ["forecast_v2.pkl", "forecast_v3.pkl"],
                         "all_blocked = 全量 blocked（v4 approved 不在内），供旧字段兼容")

    def test_pure_active_blocked_still_blocked(self):
        status, basis = ap.resolve_model_promotion(
            "forecast_v3.pkl", self.MODELS, model_ready=True)
        self.assertEqual(status, "BLOCKED")
        self.assertEqual(basis["reason"], "active_promotion_blocked")

    def test_pure_missing_active_is_fail_closed(self):
        """active 未声明 / 未登记 ⇒ 一律 BLOCKED，不猜。"""
        s1, b1 = ap.resolve_model_promotion(None, self.MODELS, model_ready=True)
        self.assertEqual((s1, b1["reason"]), ("BLOCKED", "active_model_missing"))
        s2, b2 = ap.resolve_model_promotion("forecast_v9.pkl", self.MODELS,
                                            model_ready=True)
        self.assertEqual((s2, b2["reason"]), ("BLOCKED", "active_model_not_registered"))

    def test_pure_model_ready_false_never_approved(self):
        s, _ = ap.resolve_model_promotion("forecast_v4.pkl", self.MODELS,
                                          model_ready=False)
        self.assertEqual(s, "BLOCKED")

    def test_pure_pending_active_is_blocked(self):
        models = {"m.pkl": {"promotion": {"status": "pending"}}}
        s, b = ap.resolve_model_promotion("m.pkl", models, model_ready=True)
        self.assertEqual((s, b["reason"]), ("BLOCKED", "active_promotion_pending"))

    # --- 集成层（derive_states 经 config 注入） ------------------------------
    def test_states_first_real_promotion_not_deadlocked(self):
        """未来第一次真正晋升：v4 active+approved + v2/v3 历史 blocked ⇒ 不卡死。"""
        st = self._states(True, "forecast_v4.pkl")
        self.assertEqual(st["model_promotion"], "APPROVED")
        self.assertEqual(st["production_status"], "READY",
                         "历史失败记录不得永久否决未来模型的晋升资格")
        self.assertEqual(st["inputs"]["historical_blocked_models"],
                         ["forecast_v2.pkl", "forecast_v3.pkl"])

    def test_states_active_blocked_is_blocked(self):
        st = self._states(True, "forecast_v3.pkl")
        self.assertEqual(st["model_promotion"], "BLOCKED")
        self.assertEqual(st["production_status"], "BLOCKED")

    def test_states_active_missing_is_blocked(self):
        st = self._states(True, "")
        self.assertEqual(st["model_promotion"], "BLOCKED")
        self.assertEqual(st["inputs"]["promotion_basis"], "active_model_missing")

    def test_states_legacy_field_blocked_models_kept(self):
        """旧字段 blocked_models 语义不变（全量），新字段才表达拆分。"""
        st = self._states(True, "forecast_v4.pkl")
        self.assertEqual(st["inputs"]["blocked_models"],
                         ["forecast_v2.pkl", "forecast_v3.pkl"])
        self.assertEqual(st["inputs"]["active_model"], "forecast_v4.pkl")
        self.assertEqual(st["inputs"]["active_promotion"], "approved")


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
