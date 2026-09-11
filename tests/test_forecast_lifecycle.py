"""Forecast 生命周期与报告卫生测试（2026-08-28，GPT P0/P1 落地）。

覆盖：
- split_date_oos：OOS 段永不入训（冻结纪律，train 与 oos 日期不重叠）
- date_group_cv_masks：同日不跨 fold；embargo 剔除邻近交易日；首块无训练集
- _forecast_section：model_ready=false 时数值归零，true 时展示真值
"""
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from backtest_forecast import build_xy, date_group_cv_masks, split_date_oos  # noqa: E402
from core import report_generator  # noqa: E402


def _samples(dates: list[str]) -> list[dict]:
    return [{"fund": "T", "date": d, "fwd1": 0.01 * i} for i, d in enumerate(dates)]


class TestBuildXyAlignment(unittest.TestCase):
    def test_missing_features_are_retained_with_identity_alignment(self):
        rows = [
            {"date": "2026-01-01", "fund": "A", "fwd1": 0.01,
             "est_chg": None},
            {"date": "2026-01-02", "fund": "B", "fwd1": None,
             "est_chg": 0.2},
            {"date": "2026-01-03", "fund": "C", "fwd1": -0.01,
             "est_chg": 0.3},
        ]
        batch = build_xy(rows, 1, 0.003)
        self.assertEqual(len(batch.X), 2)
        self.assertEqual(batch.dates, ["2026-01-01", "2026-01-03"])
        self.assertEqual(batch.funds, ["A", "C"])
        self.assertEqual(len(batch.y), len(batch.yret))
        self.assertEqual(batch.X[0][1], 1.0)  # est_chg missing mask


class TestSplitDateOos(unittest.TestCase):
    def test_train_and_oos_disjoint_by_date(self):
        dates = [f"2025-01-{i:02d}" for i in range(1, 21)]
        train, oos, oos_start = split_date_oos(_samples(dates))
        self.assertTrue(all(s["date"] < oos_start for s in train))
        self.assertTrue(all(s["date"] >= oos_start for s in oos))
        # v7 P0-2：label-end purge 剔除 train 尾部 5 个交易日（默认 max_horizon=5）
        # split_i=16 → cutoff=dates[11]=2025-01-12 → train=11 日，oos=4 日
        self.assertEqual(len(train) + len(oos), 20 - 5)

    def test_oos_is_tail_20pct(self):
        dates = [f"2025-01-{i:02d}" for i in range(1, 11)]
        _, oos, oos_start = split_date_oos(_samples(dates))
        self.assertEqual(oos_start, "2025-01-09")
        self.assertEqual(len(oos), 2)

    def test_empty_samples_returns_none(self):
        self.assertEqual(split_date_oos([]), ([], [], None))

    def test_single_date_does_not_crash(self):
        s = [{"fund": "T", "date": "2025-01-01", "fwd1": 0.01}]
        train, oos, oos_start = split_date_oos(s)
        self.assertEqual(len(oos), 1)
        self.assertIsNotNone(oos_start)


class TestDateGroupCv(unittest.TestCase):
    @staticmethod
    def _dates() -> list[str]:
        out = []
        for d in [f"2025-01-{i:02d}" for i in range(1, 11)]:
            out += [d, d, d]               # 每交易日 3 行（模拟多基金横截面）
        return out

    def test_no_same_day_across_folds(self):
        dates = self._dates()
        for fm in date_group_cv_masks(dates, n_splits=5, embargo=1):
            tr_dates = {dates[i] for i in range(len(dates)) if fm["tr_mask"][i]}
            va_dates = {dates[i] for i in range(len(dates)) if fm["va_mask"][i]}
            self.assertTrue(tr_dates.isdisjoint(va_dates),
                            f"fold@{fm['fold']} 存在同日跨折")

    def test_embargo_removes_neighbor_days(self):
        uniq = sorted(set(self._dates()))   # 10 个唯一交易日
        fm = date_group_cv_masks(uniq, n_splits=2, embargo=2)[1]   # 第二块 start=5
        tr_dates = {uniq[i] for i in range(len(uniq)) if fm["tr_mask"][i]}
        self.assertNotIn(uniq[3], tr_dates)   # 紧邻的 2 个交易日被 purge
        self.assertNotIn(uniq[4], tr_dates)
        self.assertIn(uniq[2], tr_dates)

    def test_first_fold_has_no_train(self):
        f0 = date_group_cv_masks(sorted(self._dates()), n_splits=5, embargo=1)[0]
        self.assertEqual(f0["n_train"], 0)


def _fc(model_ready: bool, p_up: float = 0.99) -> dict:
    # v8（2026-08-30）：真分位数回归恢复 q50 列；max_dd 仍未建模
    h = {"p_up": p_up, "p_flat": 0.005, "p_down": 0.005, "e_return": 0.03,
         "q10": -0.02, "q50": 0.025, "q90": 0.08,
         "confidence": 0.86, "model_ready": model_ready}
    return {"forecast": {"T1": dict(h), "T3": dict(h), "T5": dict(h),
                         "state": "UP", "path": "短期→中期持续上行",
                         "overall_confidence": 0.86, "model_ready": model_ready}}


class TestReportZeroing(unittest.TestCase):
    def test_not_ready_shows_zeros(self):
        text = report_generator._forecast_section({"002112": _fc(False)})
        self.assertIn("⚠️", text)
        self.assertIn("0%", text)
        self.assertIn("综合置信 0.00", text)
        self.assertNotIn("99%", text)
        self.assertNotIn("+3.00%", text)

    def test_ready_shows_real_values(self):
        text = report_generator._forecast_section({"002112": _fc(True)})
        self.assertIn("99%", text)
        self.assertNotIn("综合置信 0.00", text)

    def test_header_v7_columns(self):
        """v8（2026-08-30）：Q50 列恢复（真分位数回归），E[return] 列保留。"""
        text = report_generator._forecast_section({"002112": _fc(True)})
        self.assertIn("E[return]", text)
        self.assertIn("Q50", text)
        # 报告脚注如实标注：真分位数回归 + max_dd 未建模
        self.assertIn("quantile 回归", text)
        self.assertIn("非正态近似", text)
        self.assertIn("未建模", text)


if __name__ == "__main__":
    unittest.main()