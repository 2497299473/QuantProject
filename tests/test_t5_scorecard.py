"""T+5 评分卡纯函数测试（2026-09-01）。"""
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import numpy as np

from backtest_t5_scorecard import (scorecard_verdict, _economic_value,
                                   _window_slices, _parse_md_table_row,
                                   _read_wf_evidence)


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


class TestWfEvidenceColumnGuard(unittest.TestCase):
    """2026-09-22 离线冒烟抽到的真崩溃：WF md 四张表里 `| T+5 |` / `| 4 |`
    前缀跨表重复，解析器必须按列数认表，不得把覆盖率 / μ・σ 当 IC。"""

    MD = "\n".join([
        "| 口径 | RankIC | 95% CI | 样本 |", "|---|---:|---:|---:|",
        "| **WF pooled（部署式）** | +0.080 | [+0.020, +0.140] | 907 / 305 日 |", "",
        "| 折 | 测试窗 | n_train | WF T+5 IC | frozen T+5 IC | Δ |",
        "| 4 | 2026-04-13 ~ 2026-08-03 | 3069 | -0.035 | -0.012 | -0.023 |", "",
        "| 周期 | WF pooled IC | 95% CI | 最近折 IC |",
        "| T+5 | +0.080 | [+0.020, +0.140] | -0.035 |", "",
        # 干扰项：CQR 覆盖率表（7 列，cells[1] 带 %）+ Path-WF 折表（11 列）
        "| 周期 | pooled cov（外扩后） | raw cov | 名义 | qhat 均值 | qhat 范围 | 折数 |",
        "| T+5 | 84.2% | 53.1% | 80% | 0.0537 | [0.0493, 0.0577] | 4 |", "",
        "| 折 | 测试窗 | n_train | μ | σ | n_test | MC mdd_q10 | 真 mdd P10 "
        "| hit mdd10 | hit mdd50 | hit mfe50 |",
        "| 4 | 2026-04-13 ~ 2026-08-03 | 3069 | +0.00104 | 0.03212 | 259 "
        "| -0.1057 | -0.1307 | 17.8% | 61.0% | 31.3% |",
    ])

    def test_parses_ic_not_percent(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "wf.md"
            p.write_text(self.MD, encoding="utf-8")
            out = _read_wf_evidence(p)
        self.assertAlmostEqual(out["pooled_ic"], 0.080)
        self.assertEqual(out["pooled_ci"], (0.020, 0.140))
        self.assertEqual(len(out["folds"]), 1)          # Path-WF 的 | 4 | 行不入折表
        self.assertAlmostEqual(out["folds"][0]["wf_ic"], -0.035)
        self.assertAlmostEqual(out["folds"][0]["frozen_ic"], -0.012)
        # 本文件属 fast 层（不碰真实 output/）：真实 09-01 留档件的同形态由
        # 2026-09-22 离线冒烟验证（backtest_t5_scorecard.py --force，exit=0）。


if __name__ == "__main__":
    unittest.main()
