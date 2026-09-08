"""数据缓存契约测试：接口改版/解析漂移的第一道防线。

对本地缓存文件做结构校验（不访问网络）：字段存在、类型正确、日期升序、无空数据。
缓存目录为空时自动跳过（首次运行前无缓存）。

运行：python3 -m unittest tests.test_contracts -v
"""
import json
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
BASE = Path(__file__).resolve().parent.parent


def _valid_date(s) -> bool:
    try:
        date.fromisoformat(s)
        return True
    except (TypeError, ValueError):
        return False


class TestFundNavCache(unittest.TestCase):
    FUNDS = sorted((BASE / "data" / "klines").glob("*.json"))

    def test_cache_structure(self):
        if not self.FUNDS:
            self.skipTest("无基金净值缓存")
        for p in self.FUNDS:
            with self.subTest(fund=p.stem):
                d = json.loads(p.read_text(encoding="utf-8"))
                self.assertIn("code", d)
                self.assertTrue(d.get("name"))
                navs = d.get("navs")
                self.assertIsInstance(navs, list)
                self.assertGreater(len(navs), 100, "全量净值应远超 100 条")
                dates = [n[0] for n in navs]
                self.assertEqual(dates, sorted(dates), "净值必须按日期升序")
                self.assertTrue(all(_valid_date(x) for x in dates))
                self.assertTrue(all(isinstance(n[1], float) and n[1] > 0 for n in navs))


class TestStockKlineCache(unittest.TestCase):
    STOCKS = sorted((BASE / "data" / "stock_klines").glob("*.json"))

    def test_cache_structure(self):
        if not self.STOCKS:
            self.skipTest("无个股K线缓存")
        for p in self.STOCKS:
            with self.subTest(stock=p.stem):
                d = json.loads(p.read_text(encoding="utf-8"))
                ks = d.get("klines")
                self.assertIsInstance(ks, list)
                self.assertGreater(len(ks), 0, "缓存禁止写入空数据（防静默坏源污染）")
                dates = [k[0] for k in ks]
                self.assertEqual(dates, sorted(dates), "K线必须按日期升序")
                self.assertTrue(all(_valid_date(x) for x in dates))
                # OHLC 合法性：high >= max(o,c) >= min(o,c) >= low
                for k in ks[:: max(1, len(ks) // 50)]:  # 抽样 ~50 根
                    _dt, o, c, h, l = k
                    self.assertGreaterEqual(h, max(o, c))
                    self.assertLessEqual(l, min(o, c))


class TestHolidayCalendar(unittest.TestCase):
    def test_calendar_schema(self):
        d = json.loads((BASE / "data" / "holidays.json").read_text(encoding="utf-8"))
        self.assertIn("years", d)
        for year, groups in d["years"].items():
            self.assertRegex(year, r"^\d{4}$")
            for festival, days in groups.items():
                for day in days:
                    self.assertTrue(_valid_date(day), f"{festival} {day} 日期非法")
                    self.assertLess(date.fromisoformat(day).weekday(), 5,
                                    f"{day} 应为周一~五")


class TestHolidayCoverageGuard(unittest.TestCase):
    """防呆：日历未覆盖的年份必须能被发现（2027 目前未覆盖 = 已知缺口）。"""

    def test_calendar_years_detection(self):
        from run import calendar_years
        years = calendar_years()
        self.assertIn("2026", years)
        self.assertNotIn("2027", years,
                         "2027 未入日历——每年 12 月更新 holidays.json，此处断言将持续提醒")


if __name__ == "__main__":
    unittest.main(verbosity=2)
