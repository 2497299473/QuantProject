"""盘中 delta 实验 · 预注册骨架单测（2026-09-01）。

覆盖：
- pair_slots：mid+post 配对、缺 post 不配对、同 (date,fund) 去重
- build_delta_features：正常差值、缺失/非数值 → delta=None+missing
- paired_day_count：按天去重
- verdict_for：insufficient_data（门槛不足）/ stable / partial / fail / pending
"""
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from backtest_delta_intraday import (  # noqa: E402
    MIN_PAIRED_DAYS,
    build_delta_features,
    pair_slots,
    paired_day_count,
    verdict_for,
)

KEYS = ["est_chg", "est_sign", "breadth", "concentration",
        "covered_pct", "composite", "score"]


def rec(date, slot, fund, feats):
    return {"date": date, "slot": slot, "fund": fund, "features": feats}


F_MID = {"est_chg": 1.0, "est_sign": -1, "breadth": -0.5, "concentration": 0.2,
         "covered_pct": 70.0, "composite": 0, "score": 3}
F_POST = {"est_chg": 1.5, "est_sign": 1, "breadth": 0.1, "concentration": 0.25,
          "covered_pct": 70.0, "composite": 1, "score": 3}


class TestPairSlots(unittest.TestCase):
    def test_pairs_mid_post_same_day_fund(self):
        hist = [rec("2026-08-31", "mid", "002112", F_MID),
                rec("2026-08-31", "post", "002112", F_POST)]
        pairs = pair_slots(hist)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["date"], "2026-08-31")
        self.assertEqual(pairs[0]["fund"], "002112")

    def test_missing_post_not_paired(self):
        hist = [rec("2026-08-31", "mid", "002112", F_MID)]
        self.assertEqual(pair_slots(hist), [])

    def test_multi_day_sorted(self):
        hist = [rec("2026-09-01", "mid", "002112", F_MID),
                rec("2026-09-01", "post", "002112", F_POST),
                rec("2026-08-31", "mid", "002112", F_MID),
                rec("2026-08-31", "post", "002112", F_POST)]
        pairs = pair_slots(hist)
        self.assertEqual([p["date"] for p in pairs],
                         ["2026-08-31", "2026-09-01"])

    def test_empty_features_skipped(self):
        hist = [rec("2026-08-31", "mid", "002112", {}),
                rec("2026-08-31", "post", "002112", F_POST)]
        self.assertEqual(pair_slots(hist), [])


class TestBuildDelta(unittest.TestCase):
    def test_delta_post_minus_mid(self):
        d = build_delta_features(F_MID, F_POST, KEYS)
        self.assertAlmostEqual(d["est_chg"]["delta"], 0.5)
        self.assertAlmostEqual(d["breadth"]["delta"], 0.6)
        self.assertAlmostEqual(d["covered_pct"]["delta"], 0.0)
        self.assertFalse(d["est_chg"]["missing"])

    def test_missing_value_flagged(self):
        mid = dict(F_MID, est_chg=None)
        d = build_delta_features(mid, F_POST, KEYS)
        self.assertIsNone(d["est_chg"]["delta"])
        self.assertTrue(d["est_chg"]["missing"])

    def test_non_numeric_flagged(self):
        post = dict(F_POST, breadth="n/a")
        d = build_delta_features(F_MID, post, KEYS)
        self.assertIsNone(d["breadth"]["delta"])
        self.assertTrue(d["breadth"]["missing"])

    def test_all_keys_present(self):
        d = build_delta_features(F_MID, F_POST, KEYS)
        self.assertEqual(set(d.keys()), set(KEYS))


class TestPairedDayCount(unittest.TestCase):
    def test_days_unique(self):
        hist = [rec("2026-08-31", "mid", "002112", F_MID),
                rec("2026-08-31", "post", "002112", F_POST),
                rec("2026-08-31", "mid", "025687", F_MID),
                rec("2026-08-31", "post", "025687", F_POST)]
        self.assertEqual(paired_day_count(hist), 1)


class TestVerdict(unittest.TestCase):
    def test_insufficient_data(self):
        v = verdict_for(None, None, MIN_PAIRED_DAYS - 1)
        self.assertEqual(v["verdict"], "insufficient_data")

    def test_stable_when_positive_and_ci_lower_above_zero(self):
        v = verdict_for(0.03, (0.015, 0.05), MIN_PAIRED_DAYS)
        self.assertEqual(v["verdict"], "delta_stable")

    def test_stable_requires_ci_lower_bound_positive(self):
        v = verdict_for(0.03, (-0.005, 0.06), MIN_PAIRED_DAYS)
        self.assertEqual(v["verdict"], "delta_partial")

    def test_fail_when_negative(self):
        v = verdict_for(-0.02, (-0.04, 0.0), MIN_PAIRED_DAYS)
        self.assertEqual(v["verdict"], "delta_fail")

    def test_partial_in_band(self):
        v = verdict_for(0.005, (0.0, 0.01), MIN_PAIRED_DAYS)
        self.assertEqual(v["verdict"], "delta_partial")

    def test_pending_when_threshold_met_but_no_ic(self):
        v = verdict_for(None, None, MIN_PAIRED_DAYS)
        self.assertEqual(v["verdict"], "pending")


if __name__ == "__main__":
    unittest.main()
