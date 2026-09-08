"""P2-④：drift_monitor 纯函数测试（2026-09-01）。"""
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import drift_monitor as dm  # noqa: E402


class TestPsiKs(unittest.TestCase):
    def test_psi_same_distribution_near_zero(self):
        import random
        rng = random.Random(1)
        xs = [rng.gauss(0, 1) for _ in range(500)]
        ys = [rng.gauss(0, 1) for _ in range(500)]
        # 同分布抽样噪声下 PSI 偶可达 ~0.05+（500 样本 10 桶），0.10 内均可接受
        self.assertLess(dm.psi_vs_ref(xs, ys), 0.10)

    def test_psi_shifted_distribution_large(self):
        import random
        rng = random.Random(1)
        xs = [rng.gauss(0, 1) for _ in range(500)]
        ys = [rng.gauss(3, 1) for _ in range(500)]
        self.assertGreater(dm.psi_vs_ref(xs, ys), 1.0)
        self.assertEqual(dm.psi_level(dm.psi_vs_ref(xs, ys)), "significant")

    def test_psi_level_thresholds(self):
        self.assertEqual(dm.psi_level(0.05), "stable")
        self.assertEqual(dm.psi_level(0.15), "moderate")
        self.assertEqual(dm.psi_level(0.40), "significant")
        self.assertEqual(dm.psi_level(None), "n/a")

    def test_psi_none_on_constant_feature(self):
        xs = [0.5] * 200
        self.assertIsNone(dm.psi_vs_ref(xs, xs))       # 常数 → 无法分桶

    def test_psi_discrete_feature_category_psi(self):
        # 离散特征（唯一值 ≤5）：类别 PSI，不受分位桶伪影影响
        import random
        rng = random.Random(2)
        ref = [rng.choice([-1, 0, 1]) for _ in range(400)]
        cur = [rng.choice([-1, 0, 1]) for _ in range(200)]
        v = dm.psi_vs_ref(ref, cur)
        self.assertIsNotNone(v)
        self.assertLess(v, 0.10)                        # 同分布 → 小值
        cur_shift = [1] * 200
        self.assertGreater(dm.psi_vs_ref(ref, cur_shift), 0.10)  # 分布真变了

    def test_psi_insufficient_samples_none(self):
        xs = [0.1, 0.2, 0.3]
        self.assertIsNone(dm.psi_vs_ref(xs, xs))

    def test_ks_perfect_separation(self):
        self.assertAlmostEqual(dm.ks_statistic(list(range(100)), list(range(100, 200))), 1.0)
        self.assertAlmostEqual(dm.ks_statistic(list(range(100)), list(range(100))), 0.0)

    def test_ks_none_on_insufficient(self):
        self.assertIsNone(dm.ks_statistic([1.0], [2.0]))

    def test_label_drift_values(self):
        train = [{"fwd1": 0.001}] * 100
        test = [{"fwd1": 0.003}] * 60
        d = dm.label_drift(train, test, 1)
        self.assertAlmostEqual(d["mu_diff"], 0.002, places=9)

    def test_label_drift_none_insufficient(self):
        self.assertIsNone(dm.label_drift([{"fwd1": 0.001}], [{"fwd1": 0.002}], 1))


if __name__ == "__main__":
    unittest.main()
