"""state_lookup 测试：查表语义 / 稳定判定（空真回归）/ 缺失状态容错。

用注入式 table_data 构造夹具，不依赖落盘产物内容。
"""
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import state_lookup as slk


def _fake_row(pup_h1_train=0.6, pup_h1_oos=0.55, n_oos=30):
    def seg(a, b):
        return {"train": {"n": 50, "mean": 0.001, "med": 0.0, "p_up": a, "q10": 0, "q90": 0},
                "oos": {"n": b, "mean": 0.001, "med": 0.0, "p_up": pup_h1_oos, "q10": 0, "q90": 0}}
    return {
        "n_total": n_oos + 50, "n_train": 50, "n_oos": n_oos,
        "h1": seg(pup_h1_train, n_oos),
        "h3": {"train": {"n": 50, "mean": 0, "med": 0, "p_up": 0.5, "q10": 0, "q90": 0},
               "oos": {"n": n_oos, "mean": 0, "med": 0, "p_up": 0.52, "q10": 0, "q90": 0}},
        "h5": {"train": {"n": 50, "mean": 0, "med": 0, "p_up": 0.5, "q10": 0, "q90": 0},
               "oos": {"n": n_oos, "mean": 0, "med": 0, "p_up": 0.51, "q10": 0, "q90": 0}},
    }


class TestLookup(unittest.TestCase):
    def _table(self, stab_states, row=None):
        return {"table": {"X|Y": row or _fake_row()},
                "stability": {"X|Y": stab_states}, "meta": {"oos_start": "2025-04-29"}}

    def test_all_stable_positive(self):
        stab = {h: {"p_up_train": 0.55, "stable": True} for h in (1, 3, 5)}
        ref = slk.lookup("X|Y", self._table(stab))
        self.assertTrue(ref["all_horizons_stable"])
        self.assertEqual(ref["horizons"]["T1"]["p_up"], 0.55)

    def test_insufficient_samples_not_vacuously_stable(self):
        """回归：全周期无评估（p_up_train=None）不得误判为稳定（对应空真 bug）。"""
        stab = {h: {"p_up_train": None, "stable": False} for h in (1, 3, 5)}
        ref = slk.lookup("X|Y", self._table(stab))
        self.assertFalse(ref["all_horizons_stable"])

    def test_partial_eval_one_unstable(self):
        stab = {1: {"p_up_train": 0.55, "stable": True},
                3: {"p_up_train": 0.9, "stable": False},
                5: {"p_up_train": None, "stable": False}}
        ref = slk.lookup("X|Y", self._table(stab))
        self.assertFalse(ref["all_horizons_stable"])

    def test_missing_state_returns_none(self):
        self.assertIsNone(slk.lookup("NO|PE", {}))

    def test_oos_preferred_over_train(self):
        stab = {1: {"p_up_train": 0.6, "stable": True}}
        ref = slk.lookup("X|Y", self._table(stab, _fake_row(pup_h1_oos=0.58)))
        self.assertEqual(ref["horizons"]["T1"]["p_up"], 0.58)


if __name__ == "__main__":
    unittest.main()
