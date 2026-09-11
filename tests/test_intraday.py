"""日内特征 + 决策引擎单元测试（v4 决策倾向层）。

运行：python3 -m unittest tests.test_intraday -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import intraday_features as ifx
from core.decision_engine import DecisionInput, evaluate

H = [
    {"market": "1", "code": "600519", "name": "贵州茅台", "pct": 20.0},
    {"market": "0", "code": "300308", "name": "中际旭创", "pct": 15.0},
    {"market": "0", "code": "002415", "name": "海康威视", "pct": 10.0},
]


def _quotes(chgs):
    return {code: {"name": code, "price": 10.0, "change_pct": chg, "time": "14:55"}
            for code, chg in chgs.items()}


class TestFreshness(unittest.TestCase):
    def test_fresh_under_45(self):
        self.assertEqual(ifx.holdings_freshness("2026-06-30", "2026-08-01"), 1.0)

    def test_stale_over_120(self):
        self.assertEqual(ifx.holdings_freshness("2026-04-01", "2026-08-01"), 0.0)

    def test_linear_decay_midpoint(self):
        self.assertAlmostEqual(ifx.holdings_freshness("2026-06-02", "2026-08-01"), 0.8)


class TestComputeFeatures(unittest.TestCase):
    def test_breadth_reliability_phase(self):
        est = {"est_change_pct": 1.25, "covered_pct": 45.0}
        quotes = _quotes({"600519": 3.0, "300308": 3.0, "002415": -2.0})
        f = ifx.compute_features(H, quotes, est, "2020-01-01", prev_est=0.5)
        self.assertAlmostEqual(f["breadth"], 25.0 / 45.0)
        self.assertAlmostEqual(f["close_phase_change"], 0.75)
        self.assertEqual(f["n_up"], 2)
        self.assertEqual(f["n_down"], 1)
        self.assertAlmostEqual(f["concentration"], 60.0 / 125.0)
        # snapshot 2020-01-01 → 新鲜度 0 → 可信度 = 0.5×覆盖率归一
        self.assertAlmostEqual(f["reliability"], 0.5 * 0.45)

    def test_concentration_one_dominates(self):
        est = {"est_change_pct": 2.0, "covered_pct": 45.0}
        quotes = _quotes({"600519": 10.0, "300308": -0.5, "002415": -0.5})
        f = ifx.compute_features(H, quotes, est, "2020-01-01")
        self.assertGreater(f["concentration"], 0.9)

    def test_empty_when_no_est(self):
        est = {"est_change_pct": None, "covered_pct": 0.0}
        self.assertEqual(ifx.compute_features(H, {}, est, "2020-01-01"), {})


class TestDecisionEngine(unittest.TestCase):
    def _inp(self, feat, ts=1, pool=None, acct=None):
        return DecisionInput(code="002112", name="测试基金", slot="post",
                             technical_score=ts, feat_1455=feat,
                             pool_est=pool, account_state=acct)

    @staticmethod
    def _feat(est=2.0, breadth=0.6, conc=0.3, cov=80.0, age=10, rel=0.7, up=7, down=3):
        return {"est_return": est, "breadth": breadth, "concentration": conc,
                "covered_pct": cov, "holdings_age_days": age, "reliability": rel,
                "n_up": up, "n_down": down}

    def test_gate_coverage_blocks(self):
        d = evaluate(self._inp(self._feat(cov=50.0)))
        self.assertEqual(d.action, "HOLD")
        self.assertTrue(any("覆盖率" in c for c in d.invalid_conditions))

    def test_history_gate_blocks_action(self):
        feat = self._feat(est=9.9, breadth=1.0, cov=90.0, up=10, down=0)
        pool = {"002112": 9.9, "025687": -3.0}
        d = evaluate(self._inp(feat, ts=3, pool=pool))
        self.assertEqual(d.action, "HOLD")           # history_validated=false
        self.assertEqual(d.candidate, "ADD")          # 但倾向分指向加仓候选
        self.assertTrue(any("history_validated" in c for c in d.invalid_conditions))

    def test_score_bounds(self):
        feat = self._feat(est=9.9, breadth=1.0, cov=90.0, up=10, down=0)
        pool = {"002112": 9.9, "025687": -3.0}
        d = evaluate(self._inp(feat, ts=3, pool=pool))
        self.assertLessEqual(d.score, 100)
        self.assertGreaterEqual(d.score, -100)

    def test_account_position_penalty(self):
        feat = self._feat(est=4.0, breadth=0.8, cov=80.0, up=8, down=2)
        hi = evaluate(self._inp(feat, acct={"current_weight": 0.78, "max_weight": 0.8,
                                            "cost_nav": 1.0, "last_nav": 1.2}))
        lo = evaluate(self._inp(feat, acct={"current_weight": 0.10, "max_weight": 0.8,
                                            "cost_nav": 1.0, "last_nav": 1.2}))
        self.assertLess(hi.raw["s_account"], 0)
        self.assertGreater(lo.raw["s_account"], 0)

    def test_mid_slot_no_phase(self):
        feat = self._feat(est=1.0, cov=80.0)
        inp = DecisionInput(code="002112", name="测试基金", slot="mid",
                            technical_score=1, feat_1130=feat)
        d = evaluate(inp)
        self.assertEqual(d.raw["est_1455"], None)

    def test_post_features_override_mid_with_fallback(self):
        mid = self._feat(est=1.0, breadth=-0.8, cov=55.0, age=30)
        post = {"est_return": 2.0, "breadth": 0.7, "covered_pct": 88.0}
        inp = DecisionInput(code="002112", name="测试基金", slot="post",
                            technical_score=1, feat_1130=mid, feat_1455=post)
        d = evaluate(inp)
        merged = d.raw["features"]
        self.assertEqual(merged["est_return"], 2.0)
        self.assertEqual(merged["breadth"], 0.7)
        self.assertEqual(merged["covered_pct"], 88.0)
        self.assertEqual(merged["holdings_age_days"], 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
