"""B++-5（2026-09-23）：指标「不足/不可算」与真实 0 分离 contract tests。

fast 层纪律：纯函数 + 合成数据，零网络、零 I/O。钉死四条：
1) status 核心：真实 0 分 → (0.0, OK)；样本不足/恒值/空输入/越界 → (None, NOT_COMPUTABLE)；
2) 遗留外壳哨兵逐位保留（rank_ic 0.0 / brier 0.0 / ace 1.0），既有消费方零改动兼容；
3) calibration_curve 纯增量 status 键：bins/ace 数值不变，无有效桶 → NOT_COMPUTABLE；
4) 证据层接线：真实 0 IC 落 {value: 0.0, status: OK}，退化校准槽位 NOT_COMPUTABLE
   且不牵连 Brier 等相邻槽位；节点整体仍过 schema v2 校验。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from backtest_forecast import (                    # noqa: E402
    brier_multiclass, brier_multiclass_status, calibration_curve,
    evidence_node_from_rows, rank_ic, rank_ic_status)
from core import validation_schema as S            # noqa: E402

P_TRAIN = np.array([0.3, 0.3, 0.4])


class TestRankIcStatus(unittest.TestCase):
    def test_real_zero_is_ok_not_sentinel(self):
        """对称构造 rho 恰为 0：x=-5..5，y=x²（秩完全对称）→ (0.0, OK)。"""
        xs = [float(x) for x in range(-5, 6)]
        ys = [float(x * x) for x in xs]
        val, st = rank_ic_status(xs, ys)
        self.assertEqual(st, S.STATUS_OK)
        self.assertEqual(val, 0.0)
        # 遗留外壳对真实 0 与「不可算」同返 0.0——这正是证据层必须走 status 核心的原因
        self.assertEqual(rank_ic(xs, ys), 0.0)

    def test_small_n_not_computable_legacy_sentinel(self):
        val, st = rank_ic_status([1.0] * 9, [2.0] * 9)
        self.assertIsNone(val)
        self.assertEqual(st, S.STATUS_NOT_COMPUTABLE)
        self.assertEqual(rank_ic([1.0] * 9, [2.0] * 9), 0.0)

    def test_constant_series_not_computable(self):
        val, st = rank_ic_status(list(range(20)), [7.0] * 20)
        self.assertIsNone(val)
        self.assertEqual(st, S.STATUS_NOT_COMPUTABLE)
        self.assertEqual(rank_ic(list(range(20)), [7.0] * 20), 0.0)

    def test_monotone_full_correlation(self):
        val, st = rank_ic_status(list(range(20)), [float(x) * 2 for x in range(20)])
        self.assertEqual(st, S.STATUS_OK)
        self.assertAlmostEqual(val, 1.0)


class TestBrierStatus(unittest.TestCase):
    def test_empty_not_computable_legacy_sentinel(self):
        y = np.zeros(0, dtype=int)
        p = np.zeros((0, 3))
        val, st = brier_multiclass_status(y, p)
        self.assertIsNone(val)
        self.assertEqual(st, S.STATUS_NOT_COMPUTABLE)
        self.assertEqual(brier_multiclass(y, p), 0.0)   # 历史哨兵逐位保留

    def test_real_zero_and_manual_value(self):
        """完美预测 → 真实 0.0 + OK；一般值与手算逐位一致。"""
        y = np.array([0, 2])
        p = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
        val, st = brier_multiclass_status(y, p)
        self.assertEqual((val, st), (0.0, S.STATUS_OK))
        p2 = np.array([[0.5, 0.5, 0.0], [0.0, 0.0, 1.0]])
        val2, st2 = brier_multiclass_status(y, p2)
        want = float(np.mean([np.sum((p2[0] - np.array([1, 0, 0])) ** 2), 0.0]))
        self.assertEqual(st2, S.STATUS_OK)
        self.assertAlmostEqual(val2, want)

    def test_bad_label_not_computable(self):
        val, st = brier_multiclass_status(np.array([0, 5]), np.zeros((2, 3)))
        self.assertIsNone(val)
        self.assertEqual(st, S.STATUS_NOT_COMPUTABLE)


class TestCalibrationCurveStatus(unittest.TestCase):
    def test_empty_shape_preserved_plus_status(self):
        out = calibration_curve(np.array([], dtype=float), np.array([], dtype=float))
        self.assertEqual(out["bins"], [])
        self.assertEqual(out["ace"], 1.0)               # 哨兵逐位保留
        self.assertEqual(out["status"], S.STATUS_NOT_COMPUTABLE)

    def test_no_valid_bins_not_computable(self):
        """p 全在 [0,1] 域外（如 -1）→ 无有效桶 → NOT_COMPUTABLE，ace=1.0 是哨兵。"""
        y = np.zeros(40, dtype=int)
        out = calibration_curve(y, np.full(40, -1.0))
        self.assertEqual(out["bins"], [])
        self.assertEqual(out["ace"], 1.0)
        self.assertEqual(out["status"], S.STATUS_NOT_COMPUTABLE)

    def test_real_zero_calibration_ok(self):
        """每桶实际频率恰等于桶中点 → 真实 ace=0.0 + OK（不是哨兵 1.0）。"""
        p, y = [], []
        for mid, lo in ((0.1, 0.05), (0.3, 0.25), (0.5, 0.45),
                        (0.7, 0.65), (0.9, 0.85)):
            p += [lo] * 9 + [mid]                       # 每桶 10 样本落入同一桶
            ones = round(mid * 10)                      # 频率恰等于桶中点（1/3/5/7/9 个 1）
            y += [1] * ones + [0] * (10 - ones)
        out = calibration_curve(np.array(y), np.array(p))
        self.assertEqual(out["status"], S.STATUS_OK)
        self.assertEqual(len(out["bins"]), 5)
        self.assertAlmostEqual(out["ace"], 0.0)


class TestEvidenceNodeWiring(unittest.TestCase):
    @staticmethod
    def _dates(n: int) -> list:
        return [f"2026-01-{i:02d}" if i <= 28 else f"2026-02-{i - 28:02d}"
                for i in range(1, n + 1)]

    def test_real_zero_ic_lands_ok_with_zero_value(self):
        """构造 rho 恰为 0（正负组秩均值相等）：真实 0 必须落 {0.0, OK}，
        不得被 30 行门槛、恒值短路或遗留哨兵吞成 NOT_COMPUTABLE。"""
        n = 33
        score = [float(i) for i in range(1, n + 1)]
        yr = [1.0 if i % 2 == 0 else -1.0 for i in range(n)]   # 17 个 +1，16 个 −1
        base = [float(n - i) for i in range(n)]                 # 非恒值即可
        node = evidence_node_from_rows(score, base, yr, self._dates(n), P_TRAIN)
        self.assertEqual(node["rank_ic"]["status"], S.STATUS_OK)
        self.assertEqual(node["rank_ic"]["value"], 0.0)
        self.assertEqual(node["decision_edge"]["status"], S.STATUS_OK)

    def test_degenerate_calibration_slot_not_zero(self):
        """p 全在域外 → 校准槽位 NOT_COMPUTABLE（value=null），Brier 等相邻槽位不受牵连；
        节点整体仍过 schema v2 校验（fail-closed 链路完好）。"""
        n = 40
        rng = np.random.default_rng(11)
        score = rng.uniform(0.0, 1.0, n)
        base = rng.uniform(-0.02, 0.02, n)
        yr = rng.uniform(-0.03, 0.03, n)
        po = np.zeros((n, 3))
        po[:, 2] = -1.0                                 # 全在 [0,1] 域外
        yo = rng.integers(0, 3, n)
        node = evidence_node_from_rows(score.tolist(), base.tolist(), yr,
                                       self._dates(n), P_TRAIN, po, yo)
        slot = node[S.MIDPOINT_CALIBRATION_ERROR_KEY]
        self.assertEqual(slot["status"], S.STATUS_NOT_COMPUTABLE)
        self.assertIsNone(slot["value"])
        self.assertEqual(node["brier"]["status"], S.STATUS_OK)
        self.assertEqual(node["b_majority"]["status"], S.STATUS_OK)
        ev = S.blank_evidence()
        ev["pooled"]["1"] = node
        ok, errs = S.validate_evidence(ev)
        self.assertTrue(ok)
        self.assertEqual(errs, [])


if __name__ == "__main__":
    unittest.main()
