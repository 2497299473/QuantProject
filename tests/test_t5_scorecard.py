"""T+5 评分卡纯函数测试（2026-09-01）。"""
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import numpy as np

from backtest_t5_scorecard import (scorecard_verdict, _economic_value,
                                   _window_slices, _parse_md_table_row)


class TestScorecardVerdict(unittest.TestCase):
    def test_partial_when_recent_nonpositive(self):
        self.assertEqual(scorecard_verdict(0.020, -0.051), "partial")

    def test_stable_when_both_positive(self):
        self.assertEqual(scorecard_verdict(0.020, 0.030), "stable")

    def test_fail_when_pooled_ci_lo_nonpositive(self):
        self.assertEqual(scorecard_verdict(-0.010, 0.030), "fail")


class TestEconomicValue(unittest.TestCase):
    def test_top_bottom_spread(self):
        rng = np.random.default_rng(0)
        p = rng.random(200)
        y = p * 0.05 + rng.normal(0, 0.01, 200)   # 与 p 正相关
        ev = _economic_value(p, y)
        self.assertGreater(ev["top_bottom_spread"], 0.0)
        self.assertEqual(ev["n_top"], 80)

    def test_small_sample_returns_zero(self):
        ev = _economic_value(np.array([0.5, 0.5]), np.array([0.0, 0.0]))
        self.assertEqual(ev["top_bottom_spread"], 0.0)


class TestWindowSlices(unittest.TestCase):
    def test_equal_split(self):
        dates = [f"2025-01-{i:02d}" for i in range(1, 11)]
        wins = _window_slices(dates, 4)
        self.assertEqual(len(wins), 2)
        self.assertEqual(len(wins[0]) + len(wins[1]), 10)

    def test_empty(self):
        self.assertEqual(_window_slices([], 63), [])


class TestParseMdRow(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(_parse_md_table_row("| a | b | c |"), ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
