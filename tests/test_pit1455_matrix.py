"""V4.4-步3 验收矩阵纯函数测例（fast 层）。

分层纪律（同 test_t5_scorecard.py）：本文件属 **fast 层 = 不碰真实 data/ 与 output/**
⇒ 全部用合成行；真实 0922 件的端到端形态由 2026-09-22 一次性离线实跑验证
（backtest_pit1455_matrix.py --retrain，exit=0），勿在此加读盘用例。
"""
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import numpy as np

import backtest_pit1455_matrix as M
from backtest_forecast import build_xy


def _row(date, fund, **kw):
    r = {"date": date, "fund": fund, "est_chg": 0.0, "est_sign": 0, "breadth": 0.0,
         "concentration": 0.5, "covered_pct": 80.0, "composite": 0.0, "score": 0.0}
    r.update(kw)
    return r


class TestArithSign(unittest.TestCase):
    """核心新增语义：1455 口径的基线是 −est_chg，超额 = 模型 IC − 基线 IC。"""

    def _run(self, arith_sign):
        rng = np.random.default_rng(7)
        n = 200
        e = rng.normal(0, 0.02, n)                       # est_chg
        # 构造 fwd 与 e 正相关 ⇒ rank_ic(score, fwd) 为正
        fwd = e + rng.normal(0, 0.01, n)
        proba = np.column_stack([np.full(n, 0.3), np.full(n, 0.3), 0.4 * (fwd - fwd.min())])
        proba = proba / proba.sum(axis=1, keepdims=True)
        c = M.cell_metrics("x", proba, fwd, [f"2025-01-{i % 20 + 1:02d}" for i in range(n)],
                           e, 0.003, n_boot=0, arith_sign=arith_sign)
        return c

    def test_old_sign_uses_positive_est_chg_baseline(self):
        c = self._run(1.0)
        self.assertAlmostEqual(c["arith_baseline_ic"], c["est_chg_ic"], places=6)

    def test_1455_sign_flips_baseline(self):
        c = self._run(-1.0)
        self.assertAlmostEqual(c["arith_baseline_ic"], -c["est_chg_ic"], places=6)

    def test_excess_is_model_minus_baseline(self):
        for sgn in (1.0, -1.0):
            c = self._run(sgn)
            self.assertAlmostEqual(c["excess_vs_arith"],
                                   round(c["rank_ic"] - c["arith_baseline_ic"], 4), places=4)


class TestProtocolParity(unittest.TestCase):
    """_feature_matrix / _fit_arrays 必须与生产 build_xy 逐位一致（否则样本集前提不成立）。"""

    ROWS = [_row("2025-01-02", "002112", fwd1=0.01, est_chg=0.005),
            _row("2025-01-03", "002112", fwd1=-0.02, est_chg=-0.01),
            _row("2025-01-06", "002207", fwd1=None, est_chg=0.02),        # 尾部无 label
            _row("2025-01-07", "002207", fwd1=0.001, est_chg=None)]       # 特征缺失 → mask

    def test_matrix_matches_build_xy_after_filter(self):
        X = M._feature_matrix(self.ROWS)
        XY = build_xy(self.ROWS, 1, 0.003)
        keep = [i for i, s in enumerate(self.ROWS) if s.get("fwd1") is not None]
        self.assertEqual(X.shape[1], XY.X.shape[1])
        self.assertTrue(np.array_equal(X[keep], XY.X))

    def test_missing_feature_sets_mask_column(self):
        X = M._feature_matrix(self.ROWS)
        last = X[-1]
        # est_chg 是第 1 个特征 ⇒ 列 0=值(填 0)、列 1=missing_mask(1)
        self.assertEqual(float(last[0]), 0.0)
        self.assertEqual(float(last[1]), 1.0)

    def test_fit_arrays_label_coding(self):
        X, y = M._fit_arrays(self.ROWS, "fwd1", 0.003)
        self.assertEqual(y.tolist(), [2, 0, 1])            # up / down / flat(0.001<0.003)
        self.assertEqual(len(X), 3)


class TestCellGuards(unittest.TestCase):
    def test_no_samples(self):
        c = M.cell_metrics("x", np.zeros((0, 3)), np.zeros(0), [], np.zeros(0), 0.003, 0)
        self.assertEqual(c["verdict"], "NO_SAMPLES")

    def test_low_n_flag(self):
        n = 10
        proba = np.tile(np.array([0.34, 0.33, 0.33]), (n, 1))
        c = M.cell_metrics("x", proba, np.full(n, 0.01), [f"d{i}" for i in range(n)],
                           np.full(n, 0.001), 0.003, n_boot=0)
        self.assertTrue(c["low_n"])
        self.assertIsNone(c["spread"])                     # 低样本不报经济价值

    def test_constant_prediction_gives_no_slope_or_spread(self):
        n = 120
        proba = np.tile(np.array([0.4, 0.2, 0.4]), (n, 1))
        c = M.cell_metrics("x", proba, np.linspace(-0.05, 0.05, n),
                           [f"2025-01-{i % 20 + 1:02d}" for i in range(n)],
                           np.zeros(n), 0.003, n_boot=0)
        self.assertIsInstance(c["rank_ic"], float)
        self.assertIsNone(c["calib_slope"])     # p_up 无方差 → 斜率不可估（不假装是 0）
        self.assertIsNone(c["spread"])          # 同上，经济价值不出数


class TestWindowTrack(unittest.TestCase):
    def test_windows_split_and_pairs_both_labels(self):
        # 20 交易日 × 4 基金 = 80 行；窗宽 10 日 ⇒ 每窗 40 行（≥ _window_track 的
        # 20 行守卫，勿为了测试方便去动生产阈值）
        rows = []
        for d in range(1, 21):
            for f in ("002112", "002207", "022853", "025687"):
                rows.append(_row(f"2025-01-{d:02d}", f, fwd1=0.01 * (d % 3 - 1),
                                 fwd1_1455=0.02 * (d % 3 - 1)))
        n = len(rows)
        proba = {1: np.column_stack([np.full(n, .3), np.full(n, .3),
                                     np.linspace(.3, .5, n)])}
        out = M._window_track(rows, proba, [r["date"] for r in rows], 10, (1,))
        self.assertEqual(len(out), 2)
        self.assertEqual([w["n"] for w in out], [40, 40])
        for w in out:
            self.assertIn("old", w["ic"]["1"])
            self.assertIn("1455", w["ic"]["1"])

    def test_windows_drop_undersized_window(self):
        rows = [_row(f"2025-01-{d:02d}", "002112", fwd1=0.01, fwd1_1455=0.01)
                for d in range(1, 21)]
        proba = {1: np.tile(np.array([.3, .3, .4]), (20, 1))}
        # 每窗 10 行 < 20 → 全部丢弃（守卫生效，不出假窗）
        self.assertEqual(M._window_track(rows, proba, [r["date"] for r in rows], 10, (1,)), [])


class TestLoadPitDupGuard(unittest.TestCase):
    def test_duplicate_fund_date_raises(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "dup.jsonl"
            import json
            line = json.dumps({"fund": "002112", "date": "2025-01-02", "fwd1_1455": 0.01})
            p.write_text(f"{line}\n{line}\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                M.load_pit_rows(p)


class TestContractConstants(unittest.TestCase):
    def test_horizons_and_labels(self):
        self.assertEqual(M.HORIZONS, (1, 3, 5))
        self.assertEqual(M.LABELS, ("old", "1455"))

    def test_step2_reference_is_frozen_string(self):
        # C 轨基准来自步 2 留档报告，改动必须同步报告口径
        self.assertEqual(M.C_REF, {1: 0.049, 3: 0.048, 5: -0.019})
        self.assertLessEqual(M.C_TOL, 0.002)


if __name__ == "__main__":
    unittest.main()
