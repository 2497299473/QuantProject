"""P0-1 两轨（2026-09-23）：decision_edge 审计指标纯函数测试。

口径：decision_edge = RankIC(模型得分) − RankIC(est_chg 单因子)，OOS 同样本、
同 fwd 标签（NAV[T] 口径）。只审计，不参与裁决门禁（进 ok 与否由 main() 的
既有条件决定，本指标不得改变它）。
"""
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from backtest_forecast import decision_edge_metrics, rank_ic


def _dates(n, start="2025-01-01"):
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


class TestDecisionEdge(unittest.TestCase):
    def test_edge_zero_when_score_equals_baseline(self):
        """score 与基线完全相同 → edge 恒 0（自身不产生「超额」）。"""
        rng = np.random.default_rng(7)
        est = rng.normal(0, 1, 120)
        y = est * 0.5 + rng.normal(0, 0.5, 120)
        out = decision_edge_metrics(est, est, y, _dates(24))
        self.assertAlmostEqual(out["edge"], 0.0, places=6)

    def test_edge_positive_when_score_better_than_baseline(self):
        """score 与 y 强相关、基线无关 → edge 显著为正。"""
        rng = np.random.default_rng(11)
        n = 150
        y = rng.normal(0, 1, n)
        est = rng.normal(0, 1, n)                     # 基线与 y 无关
        score = y + rng.normal(0, 0.3, n)             # score 强相关 y
        out = decision_edge_metrics(score, est, y, _dates(30))
        self.assertGreater(out["edge"], 0.05)
        self.assertIsNotNone(out["edge_ci"])

    def test_edge_is_model_minus_baseline(self):
        """edge 数值 = rank_ic(score) − rank_ic(est)（与既有 arith 口径同款减法）。"""
        rng = np.random.default_rng(21)
        n = 140
        y = rng.normal(0, 1, n)
        est = y * 0.4 + rng.normal(0, 0.9, n)
        score = y * 0.8 + rng.normal(0, 0.5, n)
        out = decision_edge_metrics(score, est, y, _dates(28))
        expected = rank_ic(score.tolist(), y.tolist()) \
            - rank_ic(est.tolist(), y.tolist())
        self.assertAlmostEqual(out["edge"], round(expected, 4), places=4)

    def test_short_series_gives_zero_edge_and_no_ci(self):
        """样本 <10 → rank_ic 按既有约定返回 0；CI 不足 → None（如实标注不造假）。"""
        out = decision_edge_metrics([0.1, 0.2, 0.3], [0.3, 0.2, 0.1],
                                    [0.5, 0.6, 0.4], ["d1", "d2", "d3"])
        self.assertEqual(out["edge"], 0.0)
        self.assertIsNone(out["edge_ci"])


if __name__ == "__main__":
    unittest.main()
