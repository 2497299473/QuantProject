"""缠论全量形态学（core/chanlun.py）单元测试。

运行：python3 -m unittest tests.test_chanlun -v
"""
import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import chanlun


def _k(d0, i):
    return (d0 + datetime.timedelta(days=i)).isoformat()


def _bars(points, start="2023-01-02"):
    """points: [(open, close, high, low), ...] → klines [(date,o,c,h,l)]"""
    d0 = datetime.date.fromisoformat(start)
    return [(_k(d0, i), o, c, h, l) for i, (o, c, h, l) in enumerate(points)]


def _trend_bars():
    """上升折线（16 个路径点、每腿 7 根 K）：多笔多段 + 至少一个中枢。"""
    wps = [10, 16, 13, 17, 14, 18, 15, 19.5, 16, 20.5, 17, 21.5, 18, 22.5, 15, 12]
    rows, day = [], 0
    for a, b in zip(wps, wps[1:]):
        for k in range(7):
            v = a + (b - a) * (k + 1) / 7
            rows.append(((_k(datetime.date(2023, 1, 2), day)), v * 0.995, v, v * 1.01, v * 0.985))
            day += 1
    return rows


class TestMorphology(unittest.TestCase):
    def test_merge_inclusion_up_and_down(self):
        bars = [("2024-01-01", 0, 10, 11, 9), ("2024-01-02", 0, 10.5, 12, 8),   # 包含前根
                ("2024-01-03", 0, 13, 13.5, 12.5),                              # 上行非包含
                ("2024-01-04", 0, 13, 13.4, 12.6), ("2024-01-05", 0, 12.8, 13.2, 12.4)]  # 下行包含
        merged = chanlun.merge_inclusion(bars)
        self.assertEqual(len(merged), 3, "两处包含应各合并一次")
        self.assertEqual(merged[0]["high"], 12)   # 向上：高高点 + 较高低点
        self.assertEqual(merged[0]["low"], 9)
        self.assertEqual(merged[-1]["high"], 13.2)  # 向下：低低点 + 较低高点（取较小高点）
        self.assertEqual(merged[-1]["low"], 12.4)

    def test_fractals_alternate(self):
        bars = _bars([(9, 10, 10.5, 8.5), (10.5, 11, 11.5, 10), (12, 11.5, 12.5, 11),
                      (11.5, 11, 11.8, 10.5), (10.5, 10, 10.8, 9.5), (10, 9.5, 10.2, 9),
                      (9.5, 10.5, 11, 9.4), (11, 12, 12.5, 10.8), (12.5, 12, 12.8, 11.5),
                      (11.8, 11, 12, 10.8)])
        f = chanlun.find_fractals(chanlun.merge_inclusion(bars))
        types = [x["type"] for x in f]
        for a, b in zip(types, types[1:]):
            self.assertNotEqual(a, b, "分型必须顶底交替")
        self.assertIn("top", types)
        self.assertIn("bottom", types)

    def test_full_pipeline_structure(self):
        bars = _trend_bars()
        r = chanlun.analyze(bars)
        self.assertGreaterEqual(r["n_strokes"], 5, "上升折线应产生多笔")
        self.assertGreaterEqual(r["n_segs"], 3, "折线应产生多段")
        self.assertGreaterEqual(len(r["pivots"]), 1, "重叠区应至少构成一个中枢")
        p = r["pivots"][-1]
        self.assertGreater(p["ZG"], p["ZD"], "中枢区间必须非空 [ZD,ZG]")
        self.assertIn(r["trend"], ("up", "expand", "down", "consolidation"))
        for e in r["events"]:
            self.assertGreaterEqual(e["confirm_date"], e["date"],
                                    "事件确认日期不得早于事件日期（因果）")

    def test_confirm_dates_monotonic_kline(self):
        bars = _trend_bars()
        dates = [b[0] for b in bars]
        r = chanlun.analyze(bars)
        for e in r["events"]:
            self.assertIn(e["confirm_date"], dates)


if __name__ == "__main__":
    unittest.main(verbosity=2)
