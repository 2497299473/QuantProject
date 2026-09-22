"""14:55 PIT 契约 + 构建器测试（V4.4 步 1，2026-09-22）。

fast 层纪律：纯函数 + 注入的内存 nav_rows，不读真实 data/、零网络。
build() 的文件 IO 由 `python pit1455_dataset.py --dry-run` 手工验（全量 3371）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import pit1455_contract as C  # noqa: E402
import pit1455_dataset as D             # noqa: E402

NAV = [("2026-01-05", 5.0), ("2026-01-06", 5.1), ("2026-01-07", 5.05),
       ("2026-01-08", 5.2), ("2026-01-09", 5.3)]


class TestContract(unittest.TestCase):
    def test_est_chg_unit_bridge(self):
        # 全链唯一 %→小数 入口；契约钉死 fraction
        self.assertEqual(C.EST_CHG_UNIT, "fraction")
        self.assertAlmostEqual(C.est_chg_from_pct(1.23), 0.0123, places=12)
        self.assertAlmostEqual(C.est_chg_from_pct(-122.8), -1.228, places=12)
        self.assertIsNone(C.est_chg_from_pct(None))

    def test_nav_hat_and_fwd(self):
        self.assertAlmostEqual(C.estimate_nav_at_1455(5.0, 0.01), 5.05, places=12)
        self.assertAlmostEqual(C.fwd_from_1455(5.05, 5.055), 5.055 / 5.05 - 1, places=12)
        self.assertIsNone(C.estimate_nav_at_1455(0.0, 0.01))
        self.assertIsNone(C.estimate_nav_at_1455(None, 0.01))
        self.assertIsNone(C.estimate_nav_at_1455(5.0, None))
        self.assertIsNone(C.fwd_from_1455(0.0, 1.0))
        self.assertIsNone(C.fwd_from_1455(5.0, None))

    def test_provenance_shape(self):
        p = C.contract_provenance()
        self.assertEqual(p["contract_version"], C.CONTRACT_VERSION)
        self.assertEqual(p["label_denominator"], "nav_hat_1455")
        self.assertEqual(p["est_chg_unit"], "fraction")


class TestRecomputeRow(unittest.TestCase):
    def setUp(self):
        self.idx = {d: n for n, (d, _) in enumerate(NAV)}

    def test_golden_degenerates_to_original_label(self):
        """黄金校验：当 est_chg 恰等于 T 日真实涨跌时，14:55 label 必须 == 原 label。

        此时 nav_hat = navs[T-1]*(1+est) = navs[T]，公式应退化为 navs[T+h]/navs[T]-1。
        这条锁死实现正确性（分母用 T-1 而非 T、索引 i+h 对齐）。
        """
        i = self.idx["2026-01-07"]                       # T = 01-07, nav=5.05
        est_true = 5.05 / NAV[i - 1][1] - 1              # 真实 T 日涨跌
        s = {"fund": "X", "date": "2026-01-07", "est_chg": est_true, "fwd1": None}
        out = D.recompute_row(s, NAV, self.idx)
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out["_nav_hat_1455"], 5.05, places=9)
        for h in (1, 2):
            want = NAV[i + h][1] / 5.05 - 1
            self.assertAlmostEqual(out[f"fwd{h}_1455"], round(want, 6), places=6,
                                   msg=f"h={h}")

    def test_denominator_is_1455_estimate_not_final_nav(self):
        """语义校验：est_chg=0 时分母=navs[T-1]，label 应比原口径高（分母更小）。"""
        i = self.idx["2026-01-07"]
        s = {"date": "2026-01-07", "est_chg": 0.0}
        out = D.recompute_row(s, NAV, self.idx)
        self.assertAlmostEqual(out["_nav_hat_1455"], NAV[i - 1][1], places=9)
        # 原口径分母 = navs[T]=5.05 > navs[T-1]=5.1? 否；此处 navs[T-1]=5.1>5.05
        # ⇒ 分母变大，fwd1455 应 < 原 fwd。逐条断言方向，不靠巧合。
        orig1 = NAV[i + 1][1] / NAV[i][1] - 1
        self.assertLess(out["fwd1_1455"], orig1)
        self.assertAlmostEqual(out["fwd1_1455"], round(NAV[i + 1][1] / 5.1 - 1, 6), places=6)

    def test_skip_conditions(self):
        idx = self.idx
        # 无前一交易日（首行）
        self.assertIsNone(D.recompute_row({"date": NAV[0][0], "est_chg": 0.01}, NAV, idx))
        # date 不在缓存
        self.assertIsNone(D.recompute_row({"date": "2026-02-02", "est_chg": 0.01}, NAV, idx))
        # est_chg 缺失 → 契约要求不可造
        self.assertIsNone(D.recompute_row({"date": "2026-01-07", "est_chg": None}, NAV, idx))
        self.assertIsNone(D.recompute_row({"est_chg": 0.01}, NAV, idx))

    def test_missing_future_horizon_is_none_not_error(self):
        """尾部样本：h 超出缓存末尾 → 该 label 为 None，其余仍算（不整行丢）。"""
        i = self.idx["2026-01-09"]                       # 最后一日
        out = D.recompute_row({"date": "2026-01-09", "est_chg": 0.01}, NAV, self.idx)
        self.assertIsNotNone(out)
        self.assertIsNone(out["fwd1_1455"])
        self.assertIsNone(out["fwd20_1455"])

    def test_original_fields_preserved(self):
        s = {"fund": "002112", "date": "2026-01-07", "est_chg": 0.02,
             "est_sign": 1, "breadth": 0.5, "fwd5": 0.03}
        out = D.recompute_row(s, NAV, self.idx)
        for k, v in s.items():
            self.assertEqual(out[k], v)                  # 原行不动，新增并存
        self.assertIn("fwd5_1455", out)


if __name__ == "__main__":
    unittest.main()
