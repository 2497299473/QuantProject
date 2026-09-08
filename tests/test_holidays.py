"""节假日边界测试：2026 全年 18 个休市工作日逐个验证 + 周末 + 正常交易日 + 节前节后。

运行：python3 -m unittest tests.test_holidays -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from run import HOLIDAYS, is_trading_day

D = date  # 简写


class TestHolidayCalendar(unittest.TestCase):
    def test_calendar_has_18_days_in_2026(self):
        days_2026 = [d for d in HOLIDAYS if d.startswith("2026")]
        self.assertEqual(len(days_2026), 18, "2026 年应为 18 个工作日休市日（上证公告〔2025〕45号）")

    def test_all_holidays_are_weekdays(self):
        for d in sorted(HOLIDAYS):
            dt = date.fromisoformat(d)
            self.assertLess(dt.weekday(), 5, f"{d} 应为周一~五（周末不应出现在日历中）")

    def test_holiday_closures_are_not_trading_days(self):
        # 逐个休市日：不是交易日
        for d in sorted(HOLIDAYS):
            self.assertFalse(is_trading_day(date.fromisoformat(d)), f"{d} 休市日不应为交易日")

    def test_weekends_are_not_trading_days(self):
        for d in ["2026-08-22", "2026-08-23", "2026-01-03", "2026-01-04"]:
            self.assertFalse(is_trading_day(date.fromisoformat(d)), f"{d} 周末不应为交易日")

    def test_normal_weekdays_are_trading_days(self):
        for d in ["2026-08-21", "2026-08-24", "2026-01-05", "2026-03-16"]:
            self.assertTrue(is_trading_day(date.fromisoformat(d)), f"{d} 普通工作日应为交易日")

    def test_boundaries_around_holidays(self):
        # 元旦：12-31（周四）交易，01-01/01-02 休，01-05（周一）恢复
        self.assertTrue(is_trading_day(D(2025, 12, 31)))
        self.assertFalse(is_trading_day(D(2026, 1, 1)))
        self.assertFalse(is_trading_day(D(2026, 1, 2)))
        self.assertTrue(is_trading_day(D(2026, 1, 5)))
        # 春节：02-13（周五）节前最后交易日，02-16~20 休，02-23（周一）节后首日
        self.assertTrue(is_trading_day(D(2026, 2, 13)))
        self.assertFalse(is_trading_day(D(2026, 2, 20)))
        self.assertTrue(is_trading_day(D(2026, 2, 23)))
        # 清明：04-03（周五）交易，04-06（周一）休，04-07 恢复
        self.assertTrue(is_trading_day(D(2026, 4, 3)))
        self.assertFalse(is_trading_day(D(2026, 4, 6)))
        self.assertTrue(is_trading_day(D(2026, 4, 7)))
        # 国庆：09-30（周三）交易，10-01/02、10-05/06/07 休，10-08（周四）恢复
        self.assertTrue(is_trading_day(D(2026, 9, 30)))
        self.assertFalse(is_trading_day(D(2026, 10, 1)))
        self.assertFalse(is_trading_day(D(2026, 10, 7)))
        self.assertTrue(is_trading_day(D(2026, 10, 8)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
