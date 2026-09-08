"""P2：path_forecast + intraday_feature_store 测试（2026-08-29，GPT 三审）。"""
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import path_forecast as pf  # noqa: E402
from core import intraday_feature_store as fs  # noqa: E402


def _fake_samples(n=300):
    import random
    rng = random.Random(0)
    # v1.3：日期必须一天一行（生产样本一键 = 一基金一交易日）
    return [{"fund": "T", "date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
             "fwd1": rng.gauss(0.0005, 0.01)} for i in range(n)]


class TestPathForecast(unittest.TestCase):
    def test_window_mdd_mfe_known_values(self):
        # 路径 1.0 → 0.9 → 1.2 → 1.05：
        # MDD = 窗口内任意时点起的最大后续跌幅 = 1.2→1.05 的 -0.125（比起点 -0.1 更深）
        # MFE = 相对起点的最大后续涨幅 = 1.2/1.0-1 = +0.2
        path = [1.0, 0.9, 1.2, 1.05]
        self.assertAlmostEqual(pf._window_mdd(path), -0.125, places=9)
        self.assertAlmostEqual(pf._window_mfe(path[0], path), 0.2, places=9)

    def test_forecast_produces_distribution(self):
        out = pf.forecast_path(_fake_samples(), horizon=5, n_paths=500)
        self.assertTrue(out.model_ready)
        self.assertLessEqual(out.q10, out.e_return)
        self.assertGreaterEqual(out.q90, out.e_return)
        self.assertLessEqual(out.mdd_q50, 0.0)
        self.assertGreaterEqual(out.mfe_q50, 0.0)
        self.assertEqual(len(out.mid_path), 6)        # 起点 + 5 日
        self.assertTrue(all(v >= -1.0 for v in out.mid_path))

    def test_insufficient_data_placeholder(self):
        out = pf.forecast_path([{"fwd1": 0.01}] * 5, horizon=5)
        self.assertFalse(out.model_ready)
        self.assertEqual(out.e_return, 0.0)

    def test_deterministic_with_seed(self):
        a = pf.forecast_path(_fake_samples(), horizon=3, n_paths=200, seed=7)
        b = pf.forecast_path(_fake_samples(), horizon=3, n_paths=200, seed=7)
        self.assertEqual(a.e_return, b.e_return)
        self.assertEqual(a.q10, b.q10)

    def test_cn_output(self):
        out = pf.forecast_path(_fake_samples(), horizon=5, n_paths=200)
        cn = pf.path_forecast_cn(out)
        self.assertTrue(cn["model_ready"])
        for k in ("e_return", "q10", "q90", "mdd_q10", "mdd_q50", "mfe_q50"):
            self.assertIn(k, cn)

    def test_conditional_state_buckets_differ(self):
        """v1.1：不同状态桶 μ 不同 → 条件路径分布不同（up 期望 > down）。"""
        import random
        rng = random.Random(3)
        samples = []
        for i in range(300):
            trend = 1 if i < 150 else -1
            mu = 0.002 if trend == 1 else -0.002
            samples.append({"fund": "T",
                            "date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
                            "fwd1": rng.gauss(mu, 0.01), "state_trend": trend})
        up = pf.forecast_path_conditional(samples, 5, trend="up", n_paths=300)
        dn = pf.forecast_path_conditional(samples, 5, trend="down", n_paths=300)
        self.assertTrue(up.model_ready and dn.model_ready)
        self.assertGreater(up.e_return, dn.e_return)
        self.assertFalse(up.meta.get("fallback"))

    def test_conditional_fallback_global(self):
        """状态桶样本不足/缺失 → 回退全局参数，meta.fallback=True。"""
        samples = _fake_samples(50)          # 无 state_trend
        out = pf.forecast_path_conditional(samples, 5, trend="up", n_paths=200)
        self.assertTrue(out.model_ready)
        self.assertTrue(out.meta.get("fallback"))
        self.assertEqual(out.meta.get("bucket"), "global")


class TestRecentSigmaV12(unittest.TestCase):
    """v1.2 引入、v1.3 改基金级口径：σ 用最近 20 个交易日，窗口退化回退全样本。"""

    def _samples(self, rets):
        return [{"fund": "T", "date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
                 "fwd1": r} for i, r in enumerate(rets)]

    def test_recent_window_used(self):
        # 前 60 条大波动（σ≈0.03），最近 20 条平静（σ≈0.005）
        # → v1.2 σ 必须等于最近 20 条的标准差，远小于全样本 σ
        import math
        import random
        rng = random.Random(11)
        rets = [rng.gauss(0.0, 0.03) for _ in range(60)] + \
               [rng.gauss(0.0, 0.005) for _ in range(20)]
        out = pf.forecast_path(self._samples(rets), horizon=5, n_paths=200)
        self.assertTrue(out.model_ready)
        self.assertEqual(out.meta["sigma_src"], "recent20")
        params = pf.fit_path_params(self._samples(rets), 5)
        recent = rets[-20:]
        r_mu = sum(recent) / 20
        recent_var = sum((r - r_mu) ** 2 for r in recent) / 19
        self.assertAlmostEqual(params[1], math.sqrt(recent_var), places=12)

    def test_fallback_full_when_window_degenerate(self):
        # 尾部 20 条常数 → 近期窗口零方差退化 → 回退全样本 σ
        import math
        rets = [0.002] * 10 + [0.001] * 20
        params = pf.fit_path_params(self._samples(rets), 5)
        self.assertIsNotNone(params)
        mu = (10 * 0.002 + 20 * 0.001) / 30
        full_var = sum((r - mu) ** 2 for r in rets) / 29
        self.assertAlmostEqual(params[1], math.sqrt(full_var), places=12)
        self.assertEqual(pf.sigma_source(self._samples(rets)), "full")

    def test_sigma_source_none_when_insufficient(self):
        self.assertEqual(pf.sigma_source([{"fwd1": 0.01}] * 5), "none")

    def test_meta_exposed_in_cn(self):
        import random
        rng = random.Random(5)
        rets = [rng.gauss(0.0005, 0.01) for _ in range(80)]
        out = pf.forecast_path(self._samples(rets), horizon=3, n_paths=100)
        cn = pf.path_forecast_cn(out)
        self.assertIn("sigma_src", cn)
        self.assertEqual(cn["sigma_src"], "recent20")

    def test_conditional_bucket_sigma_src(self):
        # 桶内 150 条正常波动 → recent20；桶标签随 meta 暴露
        import random
        rng = random.Random(3)
        samples = []
        for i in range(300):
            trend = 1 if i < 150 else -1
            mu = 0.002 if trend == 1 else -0.002
            samples.append({"fund": "T",
                            "date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
                            "fwd1": rng.gauss(mu, 0.01), "state_trend": trend})
        up = pf.forecast_path_conditional(samples, 5, trend="up", n_paths=200)
        self.assertEqual(up.meta["sigma_src"], "recent20")
        self.assertFalse(up.meta["fallback"])

    def test_conditional_bucket_full_label(self):
        # 桶 25 条（5 随机 + 20 常数）→ 桶尾窗口退化 → bucket_full 标签
        import random
        rng = random.Random(7)
        samples = [{"fund": "T", "date": f"2024-01-{d + 1:02d}",
                    "fwd1": rng.gauss(0.001, 0.01), "state_trend": 1}
                   for d in range(5)]
        samples += [{"fund": "T", "date": f"2024-01-{d + 6:02d}", "fwd1": 0.001,
                     "state_trend": 1} for d in range(20)]
        out = pf.forecast_path_conditional(samples, 5, trend="up", n_paths=100)
        self.assertTrue(out.model_ready)
        self.assertEqual(out.meta["sigma_src"], "bucket_full")

class TestFundLevelWindowV13(unittest.TestCase):
    """v1.3（2026-09-01）：σ 窗口 = 每基金最近 20 个交易日（等权拼接）。

    回归背景（外部审阅 P0）：v1.2 在 4 基金池上 rets[-20:] 取 20 个
    (基金×日期) 混合行 ≈ 5 个交易日，且窗口基金构成随历史长度失衡。
    """

    def _regime_rows(self):
        # 55 个共同交易日：1-35 平静（σ=0.005），36-45 剧烈（σ=0.03），
        # 46-55 转平静。pooled 行口径（按日排序尾 20 行 = 最后 5 日）→
        # ≈0.005；基金级口径覆盖每基金最近 20 个交易日（36-55）→ 明显更大。
        import random
        rng = random.Random(42)
        rows = []
        for fund in ("A", "B", "C", "D"):
            for i in range(55):
                sigma = 0.005 if i < 35 or i >= 45 else 0.03
                rows.append({"fund": fund,
                             "date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
                             "fwd1": rng.gauss(0.0, sigma)})
        return rows

    def test_window_covers_20_trading_days_not_20_rows(self):
        """P0 回归主测试：窗口必须是 20 个交易日，不是 20 个混合行。"""
        import math
        rows = sorted(self._regime_rows(), key=lambda s: (s["date"], s["fund"]))
        params = pf.fit_path_params(rows, 5)
        self.assertIsNotNone(params)
        sigma = params[1]
        # v1.2 行为复现：按日排序后尾 20 行 = 最后 5 个交易日（46-55 平静段）
        pooled_tail = [float(s["fwd1"]) for s in rows[-20:]]
        p_mu = sum(pooled_tail) / len(pooled_tail)
        pooled_sigma = math.sqrt(sum((r - p_mu) ** 2 for r in pooled_tail)
                                 / (len(pooled_tail) - 1))
        # 基金级 20 日窗口含 36-45 剧烈段，σ 必须显著大于纯平静段
        self.assertGreater(sigma - pooled_sigma, 0.005)

    def test_expected_value_exact(self):
        import math
        rows = sorted(self._regime_rows(), key=lambda s: (s["date"], s["fund"]))
        params = pf.fit_path_params(rows, 5)
        window: list[float] = []
        for fund in sorted({s["fund"] for s in rows}):
            window += [float(s["fwd1"]) for s in rows if s["fund"] == fund][-20:]
        mu = sum(float(s["fwd1"]) for s in rows) / len(rows)
        w_mu = sum(window) / len(window)
        expected = math.sqrt(sum((r - w_mu) ** 2 for r in window)
                             / (len(window) - 1))
        self.assertAlmostEqual(params[1], expected, places=12)
        self.assertAlmostEqual(params[0], mu, places=15)

    def test_short_history_fund_capped(self):
        # 长历史基金（60 日）+ 短历史基金（25 日）：拼接窗口各取 20 日，
        # 不因 A 行数多而稀释/主导 B。
        import math
        import random
        rng = random.Random(9)
        rows = [{"fund": "A", "date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
                 "fwd1": rng.gauss(0.0, 0.005)} for i in range(60)]
        rows += [{"fund": "B", "date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
                  "fwd1": rng.gauss(0.0, 0.03)} for i in range(25)]
        params = pf.fit_path_params(rows, 5)
        window = ([float(s["fwd1"]) for s in rows if s["fund"] == "A"][-20:]
                  + [float(s["fwd1"]) for s in rows if s["fund"] == "B"][-20:])
        w_mu = sum(window) / len(window)
        expected = math.sqrt(sum((r - w_mu) ** 2 for r in window)
                             / (len(window) - 1))
        self.assertAlmostEqual(params[1], expected, places=12)

    def test_duplicate_fund_day_keeps_last(self):
        # 同 (fund, date) 重复行保留最后一条；去重后 <30 → None
        import math
        rows = [{"fund": "T", "date": f"2024-01-{d:02d}", "fwd1": 0.01}
                for d in range(1, 11)]
        rows += [{"fund": "T", "date": f"2024-01-{d:02d}", "fwd1": 0.5}
                 for d in range(1, 11)]        # 重复同键，值不同
        self.assertIsNone(pf.fit_path_params(rows, 5))
        self.assertEqual(pf.sigma_source(rows), "none")
        # 补足 30 个交易日后：去重生效，窗口=最后 20 日（全 0.02）
        # → 零方差退化 → 回退全样本 σ（标签 full）
        rows2 = rows + [{"fund": "T", "date": f"2024-02-{d:02d}", "fwd1": 0.02}
                        for d in range(1, 21)]
        params = pf.fit_path_params(rows2, 5)
        self.assertIsNotNone(params)
        self.assertEqual(pf.sigma_source(rows2), "full")
        mu = (10 * 0.5 + 20 * 0.02) / 30
        full_var = (10 * (0.5 - mu) ** 2 + 20 * (0.02 - mu) ** 2) / 29
        self.assertAlmostEqual(params[1], math.sqrt(full_var), places=12)

    def test_fund_scope_single_fund(self):
        import math
        import random
        rng = random.Random(13)
        rows = [{"fund": "A", "date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
                 "fwd1": rng.gauss(0.0005, 0.005)} for i in range(60)]
        rows += [{"fund": "B", "date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
                  "fwd1": rng.gauss(-0.001, 0.03)} for i in range(60)]
        params = pf.fit_path_params(rows, 5, fund_code="B")
        b_rets = [float(s["fwd1"]) for s in rows if s["fund"] == "B"]
        self.assertAlmostEqual(params[0], sum(b_rets) / len(b_rets), places=15)
        recent = b_rets[-20:]
        r_mu = sum(recent) / 20
        self.assertAlmostEqual(
            params[1],
            math.sqrt(sum((r - r_mu) ** 2 for r in recent) / 19), places=12)
        out = pf.forecast_path(rows, 5, n_paths=200, fund_code="A")
        self.assertTrue(out.model_ready)
        self.assertEqual(out.meta["sigma_scope"], "fund:A")
        self.assertEqual(out.meta["sigma_src"], "recent20")
        cn = pf.path_forecast_cn(out)
        self.assertEqual(cn["sigma_scope"], "fund:A")
        # 缺省（不给 fund_code）= 池口径
        out2 = pf.forecast_path(rows, 5, n_paths=200)
        self.assertEqual(out2.meta["sigma_scope"], "fund_pooled")


class TestFeatureStore(unittest.TestCase):
    def setUp(self):
        self._orig = fs.STORE_PATH
        self.test_path = fs.BASE_DIR / "data" / "_test_feature_store.jsonl"
        fs.STORE_PATH = self.test_path
        self.test_path.unlink(missing_ok=True)

    def tearDown(self):
        fs.STORE_PATH = self._orig
        self.test_path.unlink(missing_ok=True)

    def test_append_load_roundtrip(self):
        ok = fs.append_features("2026-08-29", "mid", "002112",
                                {"est_chg": 0.01, "breadth": 0.3},
                                context={"state": "up|above"}, model_version=1)
        self.assertTrue(ok)
        recs = fs.load_history()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["fund"], "002112")
        self.assertEqual(recs[0]["features"]["est_chg"], 0.01)
        self.assertEqual(recs[0]["context"]["state"], "up|above")

    def test_last_write_wins(self):
        fs.append_features("2026-08-29", "mid", "F", {"est_chg": 0.01})
        import time as _t
        _t.sleep(1.1)                     # 不同 timestamp 秒级
        fs.append_features("2026-08-29", "mid", "F", {"est_chg": 0.02})
        recs = fs.load_history(fund="F")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["features"]["est_chg"], 0.02)

    def test_filters(self):
        fs.append_features("2026-08-01", "mid", "A", {"est_chg": 0.0})
        fs.append_features("2026-08-02", "post", "A", {"est_chg": 0.0})
        fs.append_features("2026-08-02", "mid", "B", {"est_chg": 0.0})
        self.assertEqual(len(fs.load_history(fund="A")), 2)
        self.assertEqual(len(fs.load_history(slot="post")), 1)
        self.assertEqual(len(fs.load_history(since="2026-08-02")), 2)

    def test_empty_features_rejected(self):
        self.assertFalse(fs.append_features("2026-08-29", "mid", "F", {}))

    def test_corrupt_line_skipped(self):
        self.test_path.write_text("{bad json\n", encoding="utf-8")
        self.assertEqual(fs.load_history(), [])
        self.assertEqual(fs.coverage_summary()["n_records"], 0)


if __name__ == "__main__":
    unittest.main()
