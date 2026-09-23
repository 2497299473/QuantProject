"""lookthrough 报告侧 PIT 测例（2026-09-23：evaluate_lookthrough 不再取 history[-1]）。

背景：holdings_history 给每期附 effective_date（披露滞后后的生效日，法定披露
时限的保守下界）；回测侧（backtest_spread.load_samples）一直用
effective_snapshot 防前视，但报告侧 evaluate_lookthrough 曾直接取 history[-1]
——季末披露滞后窗内（25~95 天，年报最长）最新一期「尚未可知」，取了即让实时
报告前视。2026-09-23 一行修复：effective_snapshot(history, 今日)；无生效期 →
跳过（run.py lt_missing 探针显式降级，不造数）。
本测例钉死该纪律：fast 层，全 mock，零网络。
"""
from __future__ import annotations

import datetime
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import lookthrough  # noqa: E402

TODAY = "2026-09-23"

HOLD_OLD = [{"market": "1", "code": "600036", "name": "招商银行", "pct": 90.0}]
HOLD_NEW = [{"market": "1", "code": "000001", "name": "平安银行", "pct": 90.0}]


def _klines(n: int = 80) -> list:
    """80 根缓涨 K 线（ma5>ma20>ma60 多头排列；≥60 根满足 ma60/dd60 暖机）。

    形状与 stock_data.fetch_stock_kline 返回一致：(date, open, close, high, low) 升序。
    """
    d0 = datetime.date(2026, 6, 1)
    out, p = [], 10.0
    for i in range(n):
        d = (d0 + datetime.timedelta(days=i)).isoformat()
        c = p * 1.005
        out.append((d, p, c, c * 1.004, p * 0.996))
        p = c
    return out


def _history():
    # 最新期 09-30 的生效日 10-30（今日 09-23 未到）；06-30 期已生效
    return [
        {"date": "2026-06-30", "effective_date": "2026-09-03", "holdings": HOLD_OLD},
        {"date": "2026-09-30", "effective_date": "2026-10-30", "holdings": HOLD_NEW},
    ]


class TestEvaluateUsesEffectiveSnapshot(unittest.TestCase):
    def _run(self, history, today: str = TODAY) -> dict:
        with mock.patch.object(lookthrough, "holdings_history", return_value=history), \
             mock.patch("time.strftime", return_value=today), \
             mock.patch.object(lookthrough.stock_data, "fetch_stock_kline",
                               return_value={"code": "X", "market": "1", "klines": _klines()}), \
             mock.patch("core.chanlun.analyze", return_value={"events": []}):
            return lookthrough.evaluate_lookthrough(["000001"])

    def test_latest_not_effective_picks_last_effective(self):
        """核心：最新期未生效 → 必须取最近已生效期（06-30），而不是 history[-1]。"""
        out = self._run(_history())
        self.assertIn("000001", out)
        self.assertEqual(out["000001"]["snapshot_date"], "2026-06-30")
        # rows 应是 06-30 期的持仓（招商银行），不是 09-30 期（平安银行）
        self.assertEqual(out["000001"]["rows"][0]["code"], "600036")

    def test_latest_effective_unchanged(self):
        """行为兼容：最新期已生效时，取法与从前一致（仍取最新期）。"""
        hist = [
            {"date": "2026-06-30", "effective_date": "2026-09-03", "holdings": HOLD_OLD},
            {"date": "2026-09-30", "effective_date": "2026-09-20", "holdings": HOLD_NEW},
        ]
        out = self._run(hist)
        self.assertEqual(out["000001"]["snapshot_date"], "2026-09-30")
        self.assertEqual(out["000001"]["rows"][0]["code"], "000001")

    def test_no_effective_snapshot_skips_honestly(self):
        """无任何生效期（新基金首期尚在滞后窗内）→ 跳过，不前视、不造数；
        基金从结果中缺位，由 run.py lt_missing 探针显式降级。"""
        hist = [{"date": "2026-09-30", "effective_date": "2026-10-30", "holdings": HOLD_NEW}]
        self.assertEqual(self._run(hist), {})


if __name__ == "__main__":
    unittest.main()
