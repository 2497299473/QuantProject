# -*- coding: utf-8 -*-
"""Walk-Forward fold 构造测试（2026-09-01，GPT 五审 P0 落地）。"""
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from backtest_walk_forward import _window_slices, build_wf_folds  # noqa: E402


def _mk_samples(n_days=260, per_day=2, start="2024-01-01"):
    """造连续 n_days 个交易日的假样本（fwd5 标签齐全）。"""
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    out = []
    di = 0
    while len({s["date"] for s in out}) < n_days:
        d = (d0 + timedelta(days=di)).isoformat()
        di += 1
        if date.fromisoformat(d).weekday() >= 5:   # 跳周末
            continue
        for k in range(per_day):
            out.append({"date": d, "fund": f"F{k}",
                        "fwd1": 0.001, "fwd3": 0.002, "fwd5": 0.003,
                        "est_chg": 0.1, "est_sign": 1, "breadth": 0.5,
                        "concentration": 0.3, "covered_pct": 80.0,
                        "composite": 0.2, "score": 0.4})
    return out


class TestWindowSlices(unittest.TestCase):
    def test_equal_split(self):
        dates = [f"2025-{i:02d}-01" for i in range(1, 13)]
        ws = _window_slices(dates, 6)
        self.assertEqual(len(ws), 2)
        self.assertEqual(ws[0][0], dates[0])
        self.assertEqual(ws[-1][-1], dates[-1])
        self.assertEqual(len(ws[0]) + len(ws[-1]), 12)

    def test_empty(self):
        self.assertEqual(_window_slices([], 63), [])


class TestBuildWfFolds(unittest.TestCase):
    def test_purge_discipline(self):
        """train 样本 date < cutoff 且 cutoff 距 test_start 至少 max_horizon 个交易日。"""
        samples = _mk_samples(n_days=260, per_day=2)
        by_date = {}
        for s in samples:
            by_date.setdefault(s["date"], []).append(s)
        dates = sorted(by_date)
        oos_start = dates[208]
        oos = [s for s in samples if s["date"] >= oos_start]
        folds = build_wf_folds(samples, oos, oos_start,
                               window_days=26, max_horizon=5,
                               min_train_samples=100)
        self.assertTrue(len(folds) >= 2)
        pos = {d: i for i, d in enumerate(dates)}
        for f in folds:
            j = pos[f["test_start"]]
            self.assertEqual(pos[f["cutoff"]], j - 5)
            for s in f["train"]:
                self.assertLess(s["date"], f["cutoff"])
            # 测试窗与训练集日期无交集（label-end purge 保证）
            train_dates = {s["date"] for s in f["train"]}
            for s in f["test"]:
                self.assertNotIn(s["date"], train_dates)
            # expanding：折 2 的训练集应包含折 1 测试窗之前更多的数据
        self.assertGreater(len(folds[1]["train"]), len(folds[0]["train"]))

    def test_all_dates_before_oos_skipped(self):
        """oos_start 太靠前（无历史）→ 返回空列表。"""
        samples = _mk_samples(n_days=40, per_day=2)
        oos = samples
        folds = build_wf_folds(samples, oos, "2024-01-02",
                               window_days=10, max_horizon=20)
        self.assertEqual(folds, [])

    def test_min_train_samples(self):
        samples = _mk_samples(n_days=260, per_day=2)
        by_date = {}
        for s in samples:
            by_date.setdefault(s["date"], []).append(s)
        dates = sorted(by_date)
        oos_start = dates[130]
        oos = [s for s in samples if s["date"] >= oos_start]
        folds = build_wf_folds(samples, oos, oos_start,
                               window_days=52, max_horizon=5,
                               min_train_samples=10 ** 9)
        self.assertEqual(folds, [])


if __name__ == "__main__":
    unittest.main()
