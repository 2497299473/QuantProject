"""State Engine v0 测试：因果性（防前视）/ 状态编码 / 合成K / 转移统计聚合。

不变量：任何日期的状态只由该日及之前的数据决定——篡改未来数据不得改变过去状态。
"""
import datetime
import random
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import state_engine as se


def _navs(n: int = 220, seed: int = 7) -> list[tuple[str, float]]:
    """构造三阶段锯齿净值：上行 → 回撤 → 再上行，保证能出中央/趋势结构。"""
    rng = random.Random(seed)
    navs = []
    v = 1.0
    d = datetime.date(2024, 1, 2)
    for i in range(n):
        phase = 0.004 if i < 80 else (-0.003 if i < 130 else 0.004)
        v *= 1 + phase + rng.uniform(-0.008, 0.008)
        navs.append((d.isoformat(), round(v, 4)))
        d += datetime.timedelta(days=1)
    return navs


class TestSynthKlines(unittest.TestCase):
    def test_open_is_prev_close(self):
        navs = _navs(5)
        kl = se.synth_klines(navs)
        self.assertEqual(kl[0][1], kl[0][2])          # 首根 open == close == 首净值
        for i in range(1, 5):
            self.assertEqual(kl[i][1], navs[i - 1][1])  # open = 前一净值

    def test_high_low_extremes(self):
        kl = se.synth_klines(_navs(5))
        for d, o, c, h, l in kl:
            self.assertEqual(h, max(o, c))
            self.assertEqual(l, min(o, c))


class TestCausality(unittest.TestCase):
    def test_future_perturbation_does_not_change_past_state(self):
        navs = _navs(220)
        for i in (100, 160):
            st = se.state_at(navs, i)
            # 篡改 i 之后全部净值（放大 10 倍）——过去状态必须不变
            navs2 = navs[:i + 1] + [(d, v * 10.0) for d, v in navs[i + 1:]]
            st2 = se.state_at(navs2, i)
            self.assertEqual(st["state"], st2["state"], f"i={i} 状态被未来数据污染")
            self.assertEqual(st["trend"], st2["trend"])
            self.assertEqual(st["pos"], st2["pos"])

    def test_state_format(self):
        st = se.state_at(_navs(), 150)
        parts = st["state"].split("|")
        base_pos = parts[1].split("~")[0]
        self.assertIn(parts[0], {"up", "down", "consolidation", "expand", "na"})
        self.assertIn(base_pos, {"above", "inside", "below", "none"})
        if "~" in parts[1]:
            self.assertIn(parts[1].split("~")[1], {"fresh", "stale"})
        self.assertLessEqual(len(parts), 4, st["state"])


class TestTransitionTable(unittest.TestCase):
    def test_table_aggregates(self):
        navs = _navs(220)
        states = se.states_for_fund(navs)
        self.assertGreater(len(states), 0)
        table = se.build_transition_table({"TEST": states}, {"TEST": navs},
                                          oos_start="2024-12-31", min_n=5)
        self.assertIsInstance(table, dict)
        self.assertGreaterEqual(len(table), 1)
        for state, entry in table.items():
            self.assertIn("n_total", entry)
            self.assertIn("n_train", entry)
            self.assertIn("n_oos", entry)
            for h in (1, 3, 5):
                self.assertIn(f"h{h}", entry)
                self.assertIn("train", entry[f"h{h}"])
                self.assertIn("oos", entry[f"h{h}"])

    def test_sufficient_flag_respects_min_n(self):
        navs = _navs(220)
        states = se.states_for_fund(navs)
        table = se.build_transition_table({"TEST": states}, {"TEST": navs},
                                          min_n=10 ** 9)
        self.assertTrue(all(not e["sufficient"] for e in table.values()))


class TestStability(unittest.TestCase):
    """summarize_stability：train vs OOS 稳定性判定。"""

    def _mk_table(self, train_pup: float, oos_pup: float, oos_n: int = 25):
        def stats(pup, n):
            return {"n": n, "mean": 0.0, "med": 0.0, "p_up": pup, "q10": -0.01, "q90": 0.01}
        return {"up|above": {"n_total": 100, "n_train": 80, "n_oos": oos_n,
                             "sufficient": True,
                             "h1": {"train": stats(train_pup, 80), "oos": stats(oos_pup, oos_n)}}}

    def test_stable_when_close_and_agree(self):
        st = se.summarize_stability(self._mk_table(0.60, 0.55))
        self.assertTrue(st["up|above"][1]["stable"])

    def test_unstable_when_direction_flips(self):
        st = se.summarize_stability(self._mk_table(0.62, 0.38))
        self.assertFalse(st["up|above"][1]["stable"])
        self.assertIn("方向", st["up|above"][1]["reason"])

    def test_insufficient_oos_never_stable(self):
        st = se.summarize_stability(self._mk_table(0.60, 0.55, oos_n=5))
        self.assertFalse(st["up|above"][1]["stable"])


class TestDistanceBucket(unittest.TestCase):
    """v0.1 距离分桶：fresh/stale 判定与因果口径。"""

    def _rising(self, n=60):
        return [(f"d{k:02d}", round(1.0 + 0.01 * k, 4)) for k in range(n)]

    def test_above_fresh_and_stale(self):
        navs = self._rising()
        # 阈值低于首日净值：全序列恒在上方
        # i=8 时连续 9 日在上方 → fresh
        away, bucket = se._distance_bucket(navs, 8, "above", 0.95, 0.85)
        self.assertEqual(away, 9)
        self.assertEqual(bucket, "fresh")
        # i=55：连续 56 日 → stale
        away, bucket = se._distance_bucket(navs, 55, "above", 0.95, 0.85)
        self.assertEqual(away, 56)
        self.assertEqual(bucket, "stale")

    def test_below_symmetric(self):
        navs = [(f"d{k:02d}", round(2.0 - 0.01 * k, 4)) for k in range(40)]
        # zd=1.95：从 k=6 起 nav<zd；i=15 → away=10 → fresh
        away, bucket = se._distance_bucket(navs, 15, "below", 1.80, 1.95)
        self.assertEqual(away, 10)
        self.assertEqual(bucket, "fresh")
        # i=35 → away=30 → stale
        away, bucket = se._distance_bucket(navs, 35, "below", 1.80, 1.95)
        self.assertEqual(away, 30)
        self.assertEqual(bucket, "stale")

    def test_inside_none_no_bucket(self):
        navs = self._rising(20)
        self.assertEqual(se._distance_bucket(navs, 10, "inside", 1.05, 0.95), (0, None))
        self.assertEqual(se._distance_bucket(navs, 10, "none", None, None), (0, None))


if __name__ == "__main__":
    unittest.main()