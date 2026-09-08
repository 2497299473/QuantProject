"""持仓历史网络可见性测试（2026-08-27 静默吞错修复）。

背景：holdings_history 此前对每年代 fetch 失败静默 continue，网络/代理异常时
返回空列表不报错——run.py 会带着 0 期持仓照常跑并出报告。修复后：
失败年份必须触发 warnings.warn（全失败/部分失败都告警，信息含原因）。
"""
import os
import sys
import unittest
import warnings
from pathlib import Path
from unittest import mock

# A6（2026-08-31）：重型测试门控——默认快路径跳过，RUN_SLOW_TESTS=1 才跑。
# 快路径：python3 -m unittest discover tests
# 全量：  RUN_SLOW_TESTS=1 python3 -m unittest discover tests
_IS_SLOW = os.environ.get("RUN_SLOW_TESTS") == "1"

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import lookthrough  # noqa: E402

FAKE_OK = {"date": "2025-03-31", "holdings": [{"market": "1", "code": "000001",
                                                "name": "平安银行", "pct": 9.0}]}
FAKE_OK2 = {"date": "2025-06-30", "holdings": [{"market": "1", "code": "600036",
                                                 "name": "招商银行", "pct": 8.0}]}


@unittest.skipUnless(_IS_SLOW, "重型/真实数据测试（76s）：RUN_SLOW_TESTS=1 才运行")
class TestHoldingsHistoryVisibility(unittest.TestCase):
    def test_all_years_fail_warns_and_empty(self):
        """全部年代拉取失败 → 必须告警且返回空（不静默）。"""
        with mock.patch.object(lookthrough, "fetch_holdings_year",
                               side_effect=OSError("connection refused")):
            with self.assertWarns(Warning) as cm:
                out = lookthrough.holdings_history("999999")
        self.assertEqual(out, [])
        msg = str(cm.warning)
        self.assertIn("connection refused", msg)
        self.assertIn("999999", msg)

    def test_partial_fail_warns_but_returns_ok_years(self):
        """部分年代失败 → 告警（说明缺失）但成功年份正常返回。"""
        def fake(code, year):
            if year == 2025:
                return [FAKE_OK, FAKE_OK2]
            raise OSError("timeout")
        with mock.patch.object(lookthrough, "fetch_holdings_year", side_effect=fake):
            with self.assertWarns(Warning):
                out = lookthrough.holdings_history("999999")
        self.assertEqual(len(out), 2)
        # effective_date 仍按披露滞后天数计算（2025-03-31 → +25 天）
        self.assertEqual(out[0]["date"], "2025-03-31")
        self.assertIn("effective_date", out[0])

    def test_no_fail_no_warning(self):
        """全部成功 → 不告警（不制造噪音）。"""
        with mock.patch.object(lookthrough, "fetch_holdings_year",
                               return_value=[FAKE_OK]):
            with warnings.catch_warnings():
                warnings.simplefilter("error")   # 任何 warning 都会变异常
                out = lookthrough.holdings_history("999999")
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
