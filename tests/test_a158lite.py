"""P1-② A158-lite 特征模块单测（2026-09-10）。

锁死三件最容易在后续改动中被破坏的事：
  1. PIT 纪律——样本日当天净值不得参与（与 backtest_spread._nav_state_at 同口径）
  2. 短窗历史不足一律 NaN 且**不缩窗**、负下标不回绕
  3. 因果性——未来数据扰动不影响历史时点的特征值
另有纯净性检查（不得混入 est_chg/macd/r20/dd 等现有特征）。

零网络。运行：python -m unittest tests.test_a158lite -v
"""
import math
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "experiments" / "forecast_lab"))

from features_a158lite import (FAMILIES, WINDOWS, build_feature_rows,  # noqa: E402
                               compute_features, feature_keys)


class TestShapeAndNaming(unittest.TestCase):
    def test_count_and_uniqueness(self):
        keys = feature_keys()
        self.assertEqual(len(keys), len(FAMILIES) * len(WINDOWS))
        self.assertEqual(len(set(keys)), len(keys))

    def test_all_families_and_windows_present(self):
        keys = set(feature_keys())
        for w in WINDOWS:
            for f in FAMILIES:
                self.assertIn(f"a158_{f}_{w}", keys)


class TestNaNDiscipline(unittest.TestCase):
    def test_empty_and_tiny_history_all_nan(self):
        for n in (0, 1, 2, 4):
            f = compute_features([1.0 + 0.001 * i for i in range(n)])
            self.assertTrue(all(math.isnan(v) for v in f.values()), f"n={n}")

    def test_window_boundary_exact(self):
        # n = 6 → 只能支撑 w=5（需 w+1=6 个点）；w=10 及以上必须 NaN
        f = compute_features([1.0 + 0.001 * i for i in range(6)])
        self.assertFalse(math.isnan(f["a158_roc_5"]))
        for w in (10, 20, 30, 60):
            self.assertTrue(math.isnan(f[f"a158_roc_{w}"]), f"w={w}")

    def test_no_negative_index_wraparound(self):
        # 若实现误用负索引回绕，短序列会静默算出数值而不是 NaN
        f = compute_features([1.0, 1.1])
        self.assertTrue(math.isnan(f["a158_roc_60"]))
        self.assertTrue(math.isnan(f["a158_corr_60"]))


class TestCausality(unittest.TestCase):
    def test_future_perturbation_does_not_change_past(self):
        base = [1.0 + 0.001 * i + 0.0005 * ((i * 37) % 11) for i in range(70)]
        keys = feature_keys()
        for k in (40, 55, 69):
            a = compute_features(base[: k + 1])
            # 先把 k 之后的值改成巨值，再截断回 k——特征必须逐字段不变（无未来泄漏）
            poisoned = list(base)
            for j in range(k + 1, len(poisoned)):
                poisoned[j] = 999.0
            c = compute_features(poisoned[: k + 1])
            for key in keys:
                self.assertTrue(
                    (math.isnan(a[key]) and math.isnan(c[key]))
                    or abs(a[key] - c[key]) < 1e-12, f"{key} @k={k}")

    def test_deterministic(self):
        s = [1.0 + 0.01 * i for i in range(65)]
        self.assertEqual(compute_features(s), compute_features(s))


class TestKnownValues(unittest.TestCase):
    def setUp(self):
        self.mono = compute_features([1.01 ** i for i in range(65)])
        self.flat = compute_features([1.0] * 65)

    def test_monotone_rising(self):
        self.assertAlmostEqual(self.mono["a158_cntp_20"], 1.0, places=12)
        self.assertAlmostEqual(self.mono["a158_rank_20"], 1.0, places=12)
        self.assertAlmostEqual(self.mono["a158_rsv_20"], 1.0, places=12)
        self.assertGreater(self.mono["a158_rsqr_20"], 0.999)
        self.assertGreater(self.mono["a158_roc_20"], 0.0)
        self.assertGreater(self.mono["a158_sump_20"], 0.0)

    def test_flat_series_no_nan_pollution(self):
        self.assertAlmostEqual(self.flat["a158_roc_20"], 0.0, places=12)
        self.assertAlmostEqual(self.flat["a158_std_20"], 0.0, places=12)
        self.assertAlmostEqual(self.flat["a158_cntp_20"], 0.0, places=12)
        self.assertAlmostEqual(self.flat["a158_rsv_20"], 0.5, places=12)
        self.assertEqual(self.flat["a158_rsqr_20"], 0.0)
        self.assertEqual(self.flat["a158_corr_20"], 0.0)
        self.assertAlmostEqual(self.flat["a158_resi_20"], 0.0, places=12)


class TestPurity(unittest.TestCase):
    def test_absent_existing_features(self):
        banned = {"est_chg", "est_sign", "macd", "r20", "dd", "score", "composite"}
        self.assertFalse(set(feature_keys()) & banned)


class TestPITAlignment(unittest.TestCase):
    def test_sample_day_nav_excluded(self):
        # 样本日当天净值尚未公布（约 21~22 点才出）→ 不得参与
        navs = [("2020-01-01", 1.0), ("2020-01-02", 1.1), ("2020-01-03", 1.2)]
        rows = build_feature_rows([{"fund": "X", "date": "2020-01-03"}],
                                  {"X": navs}, windows=(5,))
        self.assertTrue(math.isnan(rows[("X", "2020-01-03")]["a158_roc_5"]))

    def test_equals_navs_prefix(self):
        navs = [(f"2020-01-{i:02d}", 1.0 + 0.01 * i) for i in range(1, 11)]
        rows = build_feature_rows([{"fund": "Y", "date": "2020-01-10"}],
                                  {"Y": navs}, windows=(5,))
        want = compute_features([v for _d, v in navs[:9]], windows=(5,))
        self.assertAlmostEqual(rows[("Y", "2020-01-10")]["a158_roc_5"],
                               want["a158_roc_5"], places=12)

    def test_missing_fund_or_first_day_all_nan(self):
        navs = [(f"2020-01-{i:02d}", 1.0 + 0.01 * i) for i in range(1, 11)]
        got = build_feature_rows([{"fund": "Y", "date": "2020-01-01"}],
                                 {"Y": navs}, windows=(5,))
        self.assertTrue(all(math.isnan(v) for v in got[("Y", "2020-01-01")].values()))
        none = build_feature_rows([{"fund": "Z", "date": "2020-01-10"}], {}, windows=(5,))
        self.assertTrue(all(math.isnan(v) for v in none[("Z", "2020-01-10")].values()))

    def test_sample_not_in_series_all_nan(self):
        navs = [(f"2020-01-{i:02d}", 1.0 + 0.01 * i) for i in range(1, 11)]
        got = build_feature_rows([{"fund": "Y", "date": "2019-12-31"}],
                                 {"Y": navs}, windows=(5,))
        self.assertTrue(all(math.isnan(v) for v in got[("Y", "2019-12-31")].values()))


if __name__ == "__main__":
    unittest.main()
