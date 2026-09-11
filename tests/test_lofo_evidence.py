"""LOFO 证据接线测试（2026-08-30，GPT 四审 P1-⑬ + GPT 九 概率/可信度分离）。"""
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import forecast_engine as fe  # noqa: E402


def _samples(n=300):
    import random
    rng = random.Random(7)
    rows = []
    for i in range(n):
        est_chg = (i % 9) / 10.0 - 0.4
        fwd = est_chg * 2.0 + rng.uniform(-0.02, 0.02)
        rows.append({
            "fund": "TEST", "date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
            "est_chg": est_chg, "est_sign": 1 if est_chg > 0 else -1,
            "breadth": est_chg, "concentration": 0.5, "covered_pct": 70.0,
            "composite": 0, "score": est_chg,
            "fwd1": fwd, "fwd3": fwd * 1.5, "fwd5": fwd * 2.0,
        })
    return rows


class TestStabilityFrozenMapping(unittest.TestCase):
    """backtest_lofo.stability_from_evidence 冻结映射（跑前冻结，不因结果改判）。"""

    def test_frozen_mapping(self):
        from backtest_lofo import stability_from_evidence
        self.assertEqual(stability_from_evidence(305, 0.02, 0.25), 0.85)   # 泛化成立
        self.assertEqual(stability_from_evidence(305, -0.1, -0.01), 0.2)   # 泛化不成立
        self.assertEqual(stability_from_evidence(305, -0.05, 0.17), 0.6)   # CI 跨零
        self.assertEqual(stability_from_evidence(27, -0.47, 0.34), 0.5)    # 小样本
        self.assertEqual(stability_from_evidence(None, None, None), 0.5)   # 无证据


class TestConfidenceEatsEvidence(unittest.TestCase):
    """stability 不再全局 0.85 硬编码：逐基金证据驱动。"""

    def _eng(self, ready: bool) -> fe.ForecastEngine:
        eng = fe.ForecastEngine(cfg={"forecast": {"model_ready": ready}})
        eng._fit_ok = True
        eng.model_approved = ready  # 单测显式模拟完整 registry 批准契约
        eng._fund_evidence = {"002112": {"stability": 0.85, "verdict": "x", "horizons": {}}}
        return eng

    def test_evidence_changes_stability(self):
        eng = self._eng(ready=True)
        c_good = eng._confidence(1.0, None, 0.6, 0.01,
                                 data_quality={"coverage": 80.0}, fund_code="002112")
        c_none = eng._confidence(1.0, None, 0.6, 0.01,
                                 data_quality={"coverage": 80.0}, fund_code="999999")
        self.assertGreater(c_good, c_none)      # 有证据 0.85 > 无证据中性 0.5

    def test_not_ready_caps_stability(self):
        eng = self._eng(ready=False)
        c = eng._confidence(1.0, None, 0.6, 0.01,
                            data_quality={"coverage": 80.0}, fund_code="002112")
        # 未过模型闸 → stability 封顶 0.2（未验证模型不因证据抬升）
        self.assertLessEqual(c, 0.2 * 0.4 + 1.0 * 0.2 + 1.0 * 0.2 + 0.6 * 0.2 + 1e-9)

    def test_predict_meta_carries_evidence(self):
        eng = fe.ForecastEngine(cfg={"forecast": {"model_ready": True}})
        eng.fit(_samples())
        f = eng.predict(_samples()[0], fund_code="TEST")
        self.assertIn("fund_evidence", f.meta)
        d = fe.forecast_to_cn(f)
        self.assertIn("fund_evidence", d)


class TestReportTrustDisplay(unittest.TestCase):
    """报告层「概率 vs 可信度」分离展示（GPT 九）。"""

    @staticmethod
    def _fc(evidence: dict | None) -> dict:
        h = {"p_up": 0.99, "p_flat": 0.005, "p_down": 0.005, "e_return": 0.03,
             "q10": -0.02, "q50": 0.025, "q90": 0.08, "confidence": 0.86,
             "model_ready": True}
        return {"forecast": {"T1": dict(h), "T3": dict(h), "T5": dict(h),
                             "state": "UP", "path": "短期→中期持续上行",
                             "overall_confidence": 0.86, "model_ready": True,
                             "fund_evidence": evidence}}

    def test_trust_line_with_evidence(self):
        from core import report_generator
        f = self._fc({"stability": 0.85, "verdict": "跨基金泛化成立", "n_oos": 268})
        text = report_generator._forecast_section({"022853": f})
        self.assertIn("模型可信度 0.85", text)
        self.assertIn("跨基金泛化成立", text)

    def test_trust_line_without_evidence(self):
        from core import report_generator
        text = report_generator._forecast_section({"002112": self._fc(None)})
        self.assertIn("无 LOFO 证据", text)


if __name__ == "__main__":
    unittest.main()
