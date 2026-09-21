"""PIT 前视防护判据测试（2026-09-17，配合 check_pit() 语义化落地）。

守护的**关键安全属性**：生效日滞后必须 ≥ 法定披露时限折算下界——
低于下界则对「卡法定时限才披露」的基金存在前视。
"""
import bisect
import datetime
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import audit_project as ap  # noqa: E402
from core import lookthrough as lt  # noqa: E402


def _weekdays(start: str, end: str) -> list[str]:
    """合成交易日（仅工作日、不含节假日）——用于确定性验证折算规则。"""
    d, out = datetime.date.fromisoformat(start), []
    e = datetime.date.fromisoformat(end)
    while d <= e:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


class TestPitLagFloor(unittest.TestCase):
    """下界计算：季报按 15 个交易日，半年报/年报按自然日。"""

    def setUp(self):
        self.tdays = _weekdays("2025-01-01", "2026-06-30")
        self.today = "2026-09-17"

    def test_quarter_uses_15_trading_days(self):
        # 2025-09-30（周二）之后第 15 个工作日 = 2025-10-21 → 下界 21 天
        i = bisect.bisect_right(self.tdays, "2025-09-30") + 14
        self.assertEqual(self.tdays[i], "2025-10-21")
        self.assertEqual(
            ap.pit_lag_floor("09-30", self.tdays, [2025], self.today), 21)

    def test_half_year_uses_60_natural_days(self):
        self.assertEqual(
            ap.pit_lag_floor("06-30", self.tdays, [2025], self.today), 60)

    def test_year_uses_90_natural_days(self):
        self.assertEqual(
            ap.pit_lag_floor("12-31", self.tdays, [2025], self.today), 90)

    def test_future_period_excluded(self):
        # 报告期晚于 today 者不参与（尚未披露，不算数）
        self.assertIsNone(
            ap.pit_lag_floor("12-31", self.tdays, [2030], self.today))

    def test_real_repo_lags_satisfy_floor(self):
        """回归护栏：本仓库现行 _QTR_LAG 必须全部 ≥ 下界（守住 09-30 修复）。"""
        tdays = ap.pit_trading_days()
        if tdays is None:
            self.skipTest("本地交易日历缺失，下界无法核验")
        years = [int(y) for y in lt._cfg()["history_years"]]
        today = datetime.date.today().isoformat()
        for mmdd in ("03-31", "06-30", "09-30", "12-31"):
            fl = ap.pit_lag_floor(mmdd, tdays, years, today)
            if fl is None:
                continue
            self.assertGreaterEqual(
                lt._QTR_LAG[mmdd], fl,
                f"{mmdd}: _QTR_LAG={lt._QTR_LAG[mmdd]} < 下界 {fl}（前视风险）")


class TestCheckPitVerdict(unittest.TestCase):
    """判据输出：当前仓库源码应判 PASS。"""

    def test_current_source_passes(self):
        # bundle 环境可能不带运行时行情缓存（data/stock_klines/510300.json）。
        # 日历缺失时 check_pit 诚实降级为 WARN（下界无法核验），不是回归——
        # 与 test_real_repo_lags_satisfy_floor 同款 skip guard（2026-09-21 评审第 6 条）。
        if ap.pit_trading_days() is None:
            self.skipTest("本地交易日历缺失（运行时缓存不随包分发），"
                          "check_pit 诚实降为 WARN，PASS 断言不适用")
        a = ap.Audit()
        ap.check_pit(a)
        self.assertEqual(len(a.checks), 1)
        self.assertEqual(a.checks[0].status, ap.PASS,
                         f"P0-1 应为 PASS，实际 {a.checks[0].status}：{a.checks[0].detail}")


if __name__ == "__main__":
    unittest.main()
