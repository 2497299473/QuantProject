"""分位数校准纯函数测试（2026-09-01，GPT 五审 P1-C）。"""
import sys
import unittest
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from backtest_quantile_calib import (COV_LO, COV_HI, feat_row, pinball,
                                     mean_pinball, coverage_mask,
                                     cqr_scores, cqr_qhat, expand_interval)


class TestPinball(unittest.TestCase):
    def test_above(self):
        # y > yhat：罚 q 比例的欠估距离
        self.assertAlmostEqual(pinball(0.05, 0.03, 0.5), 0.5 * 0.02)

    def test_below(self):
        self.assertAlmostEqual(pinball(-0.05, -0.03, 0.5), 0.5 * 0.02)

    def test_asymmetric_q10_overestimate(self):
        # y < yhat（q10 高估）：罚 (1-q) 比例
        self.assertAlmostEqual(pinball(0.01, 0.05, 0.1), 0.9 * 0.04)

    def test_asymmetric_q90_underestimate(self):
        self.assertAlmostEqual(pinball(0.09, 0.05, 0.9), 0.9 * 0.04)


class TestMeanPinball(unittest.TestCase):
    def test_constant_loss_at_true_quantile(self):
        y = np.array([0.0, 0.01, 0.02, 0.03, 0.04])
        yhat = np.full_like(y, 0.02)   # 中位数=均值，对称分布下 q50 最优
        self.assertAlmostEqual(mean_pinball(y, yhat, 0.5), 0.006)  # 0.5*mean(|y-0.02|)=0.5*0.012


class TestCoverage(unittest.TestCase):
    def test_all_inside(self):
        q10 = np.array([-0.1, -0.2])
        q90 = np.array([0.1, 0.2])
        y = np.array([0.0, 0.05])
        self.assertEqual(coverage_mask(q10, q90, y).tolist(), [1.0, 1.0])

    def test_partial(self):
        q10 = np.array([-0.01, -0.1])
        q90 = np.array([0.01, 0.1])
        y = np.array([0.05, 0.05])       # 第一个越上界
        self.assertEqual(coverage_mask(q10, q90, y).tolist(), [0.0, 1.0])

    def test_band_predefined(self):
        self.assertEqual((COV_LO, COV_HI), (0.70, 0.90))


class TestFeatRow(unittest.TestCase):
    def test_b1_double_column(self):
        s = {"est_chg": 0.5, "est_sign": None, "breadth": 0.0}
        from backtest_quantile_calib import FEATURE_KEYS
        # 构造完整 key dict：其余 key 缺失 → mask=1
        row = feat_row(s)
        self.assertEqual(len(row), 2 * len(FEATURE_KEYS))
        i = FEATURE_KEYS.index("est_chg")
        self.assertEqual((row[2 * i], row[2 * i + 1]), (0.5, 0.0))
        i = FEATURE_KEYS.index("est_sign")
        self.assertEqual((row[2 * i], row[2 * i + 1]), (0.0, 1.0))
        i = FEATURE_KEYS.index("breadth")
        self.assertEqual((row[2 * i], row[2 * i + 1]), (0.0, 0.0))  # 真 0 非 missing


class TestCQR(unittest.TestCase):
    """CQR 再校准纯函数（2026-09-01 backlog 落地）。"""

    def test_cqr_scores_inside_outside(self):
        # 区间内：浅侧深度（负值）；区间外：到边距离（正值）
        s = cqr_scores([0.0, 0.2, -0.3], [-0.1, 0.1, 0.0], [0.1, 0.15, 0.05])
        self.assertAlmostEqual(s[0], -0.1)      # y=0 在 [−0.1,0.1] 内：max(−0.1−0, 0−0.1)=−0.1
        self.assertAlmostEqual(s[1], 0.05)      # y=0.2 超上界：max(0.1−0.2, 0.2−0.15)=0.05
        self.assertAlmostEqual(s[2], 0.3)       # y=−0.3 低于下界：max(0−(−0.3), −0.3−0.05)=0.3

    def test_cqr_scores_degenerate_interval_inf(self):
        # lo > hi 交叉 → inf，不参与定标
        s = cqr_scores([0.0], [0.1], [-0.1])
        self.assertTrue(np.isinf(s[0]))

    def test_cqr_qhat_finite_sample(self):
        # n=9, alpha=0.10 → k=ceil(10*0.9)=9 → 第 9 阶（最大值）
        scores = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09]
        self.assertAlmostEqual(cqr_qhat(scores, 0.10), 0.09)

    def test_cqr_qhat_too_few_returns_inf(self):
        # n=4, alpha=0.10 → k=ceil(5*0.9)=5 > n → inf（诚实不可用）
        self.assertTrue(np.isinf(cqr_qhat([0.01, 0.02, 0.03, 0.04], 0.10)))

    def test_cqr_qhat_filters_nonfinite(self):
        scores = [0.05, float("inf"), 0.01, 0.03]
        # 有效 n=3, alpha=0.10 → k=ceil(4*0.9)=4 > 3 → inf
        self.assertTrue(np.isinf(cqr_qhat(scores, 0.10)))
        # 有效 n=9 里夹 inf → 仍按 9 个有效分数算
        scores9 = [0.01 * i for i in range(1, 10)] + [float("inf")]
        self.assertAlmostEqual(cqr_qhat(scores9, 0.10), 0.09)

    def test_expand_interval_symmetric(self):
        lo, hi = expand_interval(np.array([-0.10]), np.array([0.10]), 0.05)
        self.assertAlmostEqual(lo[0], -0.15)
        self.assertAlmostEqual(hi[0], 0.15)

if __name__ == "__main__":
    unittest.main()
