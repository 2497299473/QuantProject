"""V4 步 3：实时行情 provider + 装配点离线测例（stub netutil，零网络、不碰 data/）。

对齐重构方案 §四步 3 验收「断网时返回结构带 error 字段」：
1. provider 成功解析（GBK 文本 → quotes，字段名对齐旧实现）
2. provider 网络失败 → ok=False，error 带 `network:` 前缀
3. provider 解析出 0 条 → ok=False，error 带 `data:` 前缀（旧实现静默 {} 的病灶）
4. 装配点 fetch_realtime 成功形状：6 位代码键 + name/price/change_pct/time/pct/market
5. 装配点 fetch_realtime 失败留痕：返回 {"_error": ...}（步 3 唯一语义升级）
6. weighted_estimate 对 `_error` 键免疫（非 6 位代码不并入加权）→ est=None
7. 空 holdings → {}（不进网络）
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import real_time
from core.datasource.base import DATA, NETWORK
from core.datasource.providers import realtime_tencent as rt_mod


def _quote_line(sym: str, name: str, code: str, price: str,
                t: str, chg: str) -> str:
    """造一行腾讯原始报价（≥33 段，字段位对齐旧实现：[1]名 [2]码 [3]价 [30]时 [32]涨跌幅）。"""
    parts = ["0"] * 40
    parts[0] = f'v_{sym}="1'
    parts[1] = name
    parts[2] = code
    parts[3] = price
    parts[30] = t
    parts[32] = chg
    return "~".join(parts) + '"'


class TestRealtimeProvider(unittest.TestCase):
    def setUp(self):
        self.p = rt_mod.RealtimeTencentProvider()

    def test_contract_attrs(self):
        self.assertEqual(self.p.name, "tencent")
        self.assertEqual(self.p.category, "realtime_quote")
        self.assertEqual(self.p.priority, 0)
        self.assertGreater(self.p.timeout_s, 0)

    def test_parses_quotes_from_gbk(self):
        text = ";".join([_quote_line("sh600519", "贵州茅台", "600519",
                                     "1680.00", "20260917150000", "1.23"),
                         _quote_line("sz000651", "格力电器", "000651",
                                     "45.60", "20260917150000", "-0.88")])
        with mock.patch.object(rt_mod.netutil, "http_get_bytes",
                               return_value=text.encode("gbk")):
            r = self.p.fetch(symbols="sh600519,sz000651")
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.source, "tencent")
        q = r.payload["quotes"]
        self.assertEqual(set(q), {"600519", "000651"})
        self.assertEqual(q["600519"]["price"], 1680.0)
        self.assertEqual(q["600519"]["change_pct"], 1.23)
        self.assertEqual(q["000651"]["change_pct"], -0.88)
        self.assertEqual(q["600519"]["name"], "贵州茅台")

    def test_network_error_prefixed_network(self):
        with mock.patch.object(rt_mod.netutil, "http_get_bytes",
                               side_effect=OSError("connection reset")):
            r = self.p.fetch(symbols="sh600519")
        self.assertFalse(r.ok)
        self.assertTrue(r.error.startswith(NETWORK), r.error)

    def test_zero_quotes_is_data_failure(self):
        # 上游返回拦截页 / 全 price<=0 → 解析出 0 条：旧实现静默返回 {} 的病灶
        with mock.patch.object(rt_mod.netutil, "http_get_bytes",
                               return_value="no data here".encode("gbk")):
            r = self.p.fetch(symbols="sh600519")
        self.assertFalse(r.ok)
        self.assertTrue(r.error.startswith(DATA), r.error)


class TestAssemblyPoint(unittest.TestCase):
    """core/real_time.py 装配点：每个测试用全新 registry 单例，隔离健康度累积。"""

    def setUp(self):
        self._orig = real_time._registry_singleton
        real_time._registry_singleton = None

    def tearDown(self):
        real_time._registry_singleton = self._orig

    def test_empty_holdings_no_network(self):
        with mock.patch.object(rt_mod.netutil, "http_get_bytes",
                               side_effect=AssertionError("不应发请求")):
            self.assertEqual(real_time.fetch_realtime([]), {})

    def test_success_shape_backfills_pct_and_market(self):
        holdings = [{"market": "1", "code": "600519", "name": "贵州茅台", "pct": 9.5}]
        text = _quote_line("sh600519", "贵州茅台", "600519",
                           "1680.00", "20260917150000", "1.23")
        with mock.patch.object(rt_mod.netutil, "http_get_bytes",
                               return_value=text.encode("gbk")):
            out = real_time.fetch_realtime(holdings)
        self.assertNotIn("_error", out)
        q = out["600519"]
        self.assertEqual(q["price"], 1680.0)
        self.assertEqual(q["pct"], 9.5)         # 由装配点回填
        self.assertEqual(q["market"], "1")      # 由装配点回填

    def test_failure_leaves_error_not_silent_empty(self):
        # ★ 步 3 验收点：断网返回 {"_error": ...}，不再静默 {}
        holdings = [{"market": "1", "code": "600519", "name": "x", "pct": 9.5}]
        with mock.patch.object(rt_mod.netutil, "http_get_bytes",
                               side_effect=OSError("boom")):
            out = real_time.fetch_realtime(holdings)
        self.assertIn("_error", out)
        self.assertTrue(out["_error"].startswith(NETWORK), out["_error"])

    def test_weighted_estimate_immune_to_error_key(self):
        # {"_error": ...} 不含 6 位代码键 → 加权无覆盖 → est=None（走 degraded 分支）
        holdings = [{"market": "1", "code": "600519", "name": "x", "pct": 9.5}]
        est = real_time.weighted_estimate(holdings, {"_error": "network:boom"})
        self.assertIsNone(est["est_change_pct"])
        self.assertEqual(est["covered_pct"], 0.0)

    def test_weighted_estimate_matches_old_arithmetic(self):
        holdings = [{"market": "1", "code": "A", "name": "x", "pct": 60.0},
                    {"market": "1", "code": "B", "name": "y", "pct": 40.0}]
        quotes = {"A": {"change_pct": 2.0}, "B": {"change_pct": -1.0}}
        est = real_time.weighted_estimate(holdings, quotes)
        # (60*2 + 40*-1) / (60+40) = (120-40)/100 = 0.8
        self.assertAlmostEqual(est["est_change_pct"], 0.8)
        self.assertAlmostEqual(est["covered_pct"], 100.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
