"""B 契约 §15-B1（2026-09-23）：backtest_action CI 统一按交易日聚类 bootstrap。

旧实现逐条 rng.choice 独立重抽，把同日多基金样本当独立样本，CI 偏乐观，
且与 Forecast 层已建立的按日聚类纪律不一致。现复用
backtest_forecast.cluster_bootstrap_ci（唯一实现，禁止另造第二套）。

本文件只做离线验证：复用同源、同日聚类效应方向、交易日不足 fail-closed。
"""
import datetime as dt
import sys
import unittest
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from backtest_action import bootstrap_ci, fmt_b   # noqa: E402
from backtest_forecast import cluster_bootstrap_ci   # noqa: E402


def _trading_days(n, start="2025-01-01"):
    d0 = dt.date.fromisoformat(start)
    out, d = [], d0
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def _clustered_vals(days=12, funds=5, seed=3):
    """同日同值 + 日间差异大：簇内完全相关 ⇒ 独立假设会严重高估精度。"""
    rng = np.random.default_rng(seed)
    dates = _trading_days(days)
    day_effect = rng.normal(0, 2.0, days)
    vals, dts = [], []
    for i, d in enumerate(dates):
        for _ in range(funds):
            vals.append(float(day_effect[i]))
            dts.append(d)
    return vals, dts, day_effect


class TestActionClusterBootstrap(unittest.TestCase):
    def test_same_result_as_unique_cluster_implementation(self):
        """与 cluster_bootstrap_ci 同参同结果——证明无第二套 bootstrap 实现。"""
        vals, dts, _ = _clustered_vals()
        got = bootstrap_ci(vals, dts, n_boot=999, seed=42)
        want = cluster_bootstrap_ci(
            lambda sub: float(np.mean(sub["v"])),
            {"v": np.asarray(vals, dtype=float)}, dts, n_boot=999, seed=42)
        self.assertEqual(got, want)

    def test_clustered_ci_wider_than_independence_assumption(self):
        """同日完全相关的样本，聚类 CI 必须显著宽于逐条独立口径。"""
        vals, dts, day_effect = _clustered_vals(days=12, funds=5)
        lo, hi = bootstrap_ci(vals, dts, n_boot=999, seed=42)
        sd_day = float(np.std(day_effect, ddof=1))
        se_indep = sd_day / np.sqrt(len(vals))          # 独立假设（错误）标准误
        self.assertGreater(hi - lo, 1.5 * 2 * 1.96 * se_indep)

    def test_few_trading_days_fail_closed_nan(self):
        """交易日 <10 → (nan, nan)，不再伪造 (0.0, 0.0)。"""
        out = bootstrap_ci([0.1, 0.2, 0.3], ["d1", "d2", "d3"])
        self.assertTrue(np.isnan(out[0]) and np.isnan(out[1]))

    def test_nan_ci_fails_gate_and_formats_honestly(self):
        """nan 下界使 A/B 判定自然 False（fail-closed），报告如实标 n/a。"""
        self.assertFalse(float("nan") > 0)
        self.assertIn("n/a", fmt_b({"n": 9, "mean": 1.0, "win": 50.0,
                                    "ci": (float("nan"), float("nan"))}))


if __name__ == "__main__":
    unittest.main()
