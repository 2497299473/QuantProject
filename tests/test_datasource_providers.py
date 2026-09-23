"""三个日 K provider 的离线测例（V4 步 2）——**stub 掉 netutil，零网络、不碰 data/**。

对齐重构方案 §四步 2 验收「与旧实现同参数对拍，K 线逐根一致」：
逐根对拍需真实拉数（受铁律 7 约束，须人授权），此处先钉**解析/归一/分类**三层，
把「网络之外的全部逻辑」在离线侧锁死；真实对拍作为人授权的单独一步执行。

覆盖：
1. 腾讯：单页/分页拼接、跨页去重升序、页间节流被调用、**中途失败 fail-closed（P0-2）**、
   普通错误 → network: 前缀、空数据 → data: 前缀
2. 东财：解析 + 明文 HTTP URL 契约（TLS 指纹过滤的绕行前提）+ 失败分类
3. Tushare：无 token → skip:（不计降级）、上游 code!=0 → data:、复权合成数值正确
4. classify_exc：OSError 家族 / 类名兜底（curl_cffi 不在 OSError 树下）/ 未知归 data:
"""
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.datasource import FetchResult, SourceRegistry  # noqa: E402
from core.datasource.base import (  # noqa: E402
    DATA, NETWORK, SKIP, classify_exc,
)
from core.datasource.providers import (  # noqa: E402
    stock_eastmoney, stock_tencent, stock_tushare,
)

# 腾讯 qfqday 行格式：[date, open, close, high, low, ...]
_RAW_A = [["2026-01-05", "10.0", "10.5", "10.6", "9.9", "1000"],
          ["2026-01-06", "10.5", "10.2", "10.7", "10.1", "1200"]]
_RAW_B = [["2026-01-07", "10.2", "10.9", "11.0", "10.2", "900"]]


def _tencent_payload(node_key: str, rows):
    return {"data": {node_key: {"qfqday": rows}}}


def _rows(start: date, n: int) -> list[list[str]]:
    """造 n 根连续交易日行（date, open, close, high, low, vol）。"""
    return [[(start + timedelta(days=i)).isoformat(), "1", "1", "1", "1", "0"]
            for i in range(n)]


class TestTencentProvider(unittest.TestCase):
    def setUp(self):
        self.p = stock_tencent.TencentKlineProvider()
        self.symbol = "sz000001"

    def test_single_page_parses_ohlc_order(self):
        with mock.patch.object(stock_tencent.netutil, "http_get_json",
                               return_value=_tencent_payload(self.symbol, _RAW_A)):
            r = self.p.fetch(code="000001", market="0")
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.source, "tencent")
        self.assertEqual(r.payload["source"], "tencent")
        # 逐根：date, open, close, high, low —— 与 raw 的 1/2/3/4 位对应
        self.assertEqual(r.payload["klines"],
                         [("2026-01-05", 10.0, 10.5, 10.6, 9.9),
                          ("2026-01-06", 10.5, 10.2, 10.7, 10.1)])
        self.assertGreaterEqual(r.latency_ms, 0)

    def test_pagination_merges_dedupes_and_sorts_ascending(self):
        # 首页满 PAGE（触发翻页），次页含与首页重复的日期 + 更早日期
        page1 = _rows(date(2025, 1, 1), stock_tencent.PAGE)
        page2 = [["2025-01-01", "9", "9", "9", "9", "0"],   # 与首页重复
                 ["2024-12-31", "4", "4", "4", "4", "0"]]
        self.assertEqual(len(page1), stock_tencent.PAGE)   # 满页 → 会翻页
        calls = []

        def fake_get(url, timeout=None, **kw):
            calls.append(url)
            return _tencent_payload(self.symbol,
                                    page1 if len(calls) == 1 else page2)

        with mock.patch.object(stock_tencent.netutil, "http_get_json",
                               side_effect=fake_get), \
                mock.patch.object(stock_tencent.time, "sleep") as slept:
            r = self.p.fetch(code="000001", market="0")
        self.assertTrue(r.ok, r.error)
        dates = [k[0] for k in r.payload["klines"]]
        self.assertEqual(dates, sorted(dates), "必须升序")
        self.assertEqual(dates.count("2025-01-01"), 1, "跨页重复只留一根")
        self.assertIn("2024-12-31", dates)                 # 次页被并入
        self.assertEqual(len(calls), 2)
        slept.assert_called()                              # 页间节流（源级频控纪律）

    def test_partial_page_failure_is_not_success(self):
        """P0-2（2026-09-23 授权）：某页中途失败 ⇒ 整源判败，不返回半截数据。
        （旧实现「保留已取页继续出 ok=True」会把截断历史静默写缓存，已改语义）"""
        page1 = [["2026-01-0%d" % i, "1", "1", "1", "1", "0"]
                 for i in range(1, 5)] + [["2025-01-01", "2", "2", "2", "2", "0"]]
        page1 = page1 + [["2025-06-%02d" % i, "3", "3", "3", "3", "0"]
                         for i in range(1, stock_tencent.PAGE - 4)]
        self.assertEqual(len(page1), stock_tencent.PAGE)
        seq = [_tencent_payload(self.symbol, page1), OSError("boom")]

        with mock.patch.object(stock_tencent.netutil, "http_get_json",
                               side_effect=seq), \
                mock.patch.object(stock_tencent.time, "sleep"):
            r = self.p.fetch(code="000001", market="0")
        self.assertFalse(r.ok, "中途失败不得出 ok=True（否则截断数据污染缓存）")
        self.assertIsNone(r.payload)
        self.assertTrue(r.error.startswith(NETWORK), r.error)  # OSError → 网络类，计健康度
        self.assertIn("partial_page", r.error)
        self.assertIn("got=1/", r.error)

    def test_partial_page_data_class_failure_still_not_success(self):
        """中途失败且异常非传输类（如 ValueError）：同样 fail-closed，
        但前缀归 data:（不计网络降级）。"""
        page1 = _rows(date(2025, 1, 1), stock_tencent.PAGE)
        seq = [_tencent_payload(self.symbol, page1), ValueError("bad row")]
        with mock.patch.object(stock_tencent.netutil, "http_get_json",
                               side_effect=seq), \
                mock.patch.object(stock_tencent.time, "sleep"):
            r = self.p.fetch(code="000001", market="0")
        self.assertFalse(r.ok)
        self.assertTrue(r.error.startswith(DATA), r.error)
        self.assertIn("partial_page", r.error)

    def test_transport_error_is_network_prefixed(self):
        with mock.patch.object(stock_tencent.netutil, "http_get_json",
                               side_effect=OSError("conn reset")):
            r = self.p.fetch(code="000001", market="0")
        self.assertFalse(r.ok)
        self.assertTrue(r.error.startswith(NETWORK), r.error)

    def test_empty_payload_is_data_prefixed(self):
        with mock.patch.object(stock_tencent.netutil, "http_get_json",
                               return_value={"data": {}}):
            r = self.p.fetch(code="688382", market="1")
        self.assertFalse(r.ok)
        self.assertTrue(r.error.startswith(DATA), r.error)


class TestEastmoneyProvider(unittest.TestCase):
    def setUp(self):
        self.p = stock_eastmoney.EastmoneyKlineProvider()

    def test_parses_csv_rows(self):
        body = {"data": {"klines": ["2026-01-05,10.0,10.5,10.6,9.9,123",
                                    "2026-01-06,10.5,10.2,10.7,10.1,456"]}}
        with mock.patch.object(stock_eastmoney.netutil, "http_get_json",
                               return_value=body) as got:
            r = self.p.fetch(code="000001", market="0")
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.payload["klines"],
                         [("2026-01-05", 10.0, 10.5, 10.6, 9.9),
                          ("2026-01-06", 10.5, 10.2, 10.7, 10.1)])
        self.assertEqual(r.payload["source"], "eastmoney")
        url = got.call_args[0][0]
        self.assertTrue(url.startswith("http://"), "必须走明文 HTTP（TLS 指纹绕行前提）")
        self.assertIn("secid=0.000001", url)
        self.assertIn("fqt=1", url)

    def test_short_rows_skipped_and_empty_is_data_fail(self):
        body = {"data": {"klines": ["2026-01-05,10.0,10.5"]}}
        with mock.patch.object(stock_eastmoney.netutil, "http_get_json",
                               return_value=body):
            r = self.p.fetch(code="000001", market="0")
        self.assertFalse(r.ok)
        self.assertTrue(r.error.startswith(DATA), r.error)

    def test_transport_error_is_network_prefixed(self):
        import urllib.error
        with mock.patch.object(stock_eastmoney.netutil, "http_get_json",
                               side_effect=urllib.error.URLError("down")):
            r = self.p.fetch(code="000001", market="0")
        self.assertFalse(r.ok)
        self.assertTrue(r.error.startswith(NETWORK), r.error)


class TestTushareProvider(unittest.TestCase):
    def setUp(self):
        self.p = stock_tushare.TushareKlineProvider()

    def test_missing_token_is_skip_not_failure(self):
        with mock.patch.object(stock_tushare, "_tushare_token", return_value=""):
            r = self.p.fetch(code="000001", market="0")
        self.assertFalse(r.ok)
        self.assertTrue(r.error.startswith(SKIP), r.error)

    def test_api_error_is_data_prefixed(self):
        with mock.patch.object(stock_tushare, "_tushare_token", return_value="tk"), \
                mock.patch.object(stock_tushare, "_post",
                                  side_effect=stock_tushare.TushareApiError("积分不足")):
            r = self.p.fetch(code="000001", market="0")
        self.assertFalse(r.ok)
        self.assertTrue(r.error.startswith(DATA), r.error)

    def test_transport_error_is_network_prefixed(self):
        with mock.patch.object(stock_tushare, "_tushare_token", return_value="tk"), \
                mock.patch.object(stock_tushare, "_post",
                                  side_effect=ConnectionResetError("reset")):
            r = self.p.fetch(code="000001", market="0")
        self.assertFalse(r.ok)
        self.assertTrue(r.error.startswith(NETWORK), r.error)

    def test_forward_adjust_matches_old_formula(self):
        daily = [{"trade_date": "20260105", "open": 10.0, "high": 11.0,
                  "low": 9.0, "close": 10.5},
                 {"trade_date": "20260106", "open": 10.5, "high": 12.0,
                  "low": 10.0, "close": 11.0}]
        adj = [{"trade_date": "20260105", "adj_factor": 2.0},
               {"trade_date": "20260106", "adj_factor": 4.0}]
        calls = []

        def fake_post(api_name, token, params, fields, *, timeout):
            calls.append((api_name, params.get("offset", 0)))
            return daily if api_name == "daily" else adj

        with mock.patch.object(stock_tushare, "_tushare_token", return_value="tk"), \
                mock.patch.object(stock_tushare, "_post", side_effect=fake_post):
            r = self.p.fetch(code="000001", market="0")
        self.assertTrue(r.ok, r.error)
        self.assertEqual([c[0] for c in calls], ["daily", "adj_factor"])
        latest = 4.0                                  # 归一基准 = 最新复权因子
        k0 = r.payload["klines"][0]
        self.assertEqual(k0[0], "2026-01-05")
        self.assertAlmostEqual(k0[1], 10.0 * 2.0 / latest)
        self.assertAlmostEqual(k0[2], 10.5 * 2.0 / latest)
        self.assertAlmostEqual(k0[3], 11.0 * 2.0 / latest)
        self.assertAlmostEqual(k0[4], 9.0 * 2.0 / latest)


class TestClassifyExc(unittest.TestCase):
    def test_oserror_family_is_network(self):
        for exc in (OSError("x"), ConnectionResetError("x"),
                    TimeoutError("x"), FileNotFoundError("x")):
            self.assertEqual(classify_exc(exc), NETWORK, type(exc).__name__)

    def test_urllib_and_http_client_errors_are_network(self):
        import http.client
        import urllib.error
        for exc in (urllib.error.URLError("x"),
                    urllib.error.HTTPError("u", 403, "forbidden", {}, None),
                    http.client.RemoteDisconnected("x"),
                    http.client.IncompleteRead(b""),
                    __import__("ssl").SSLError("x")):
            self.assertEqual(classify_exc(exc), NETWORK, type(exc).__name__)

    def test_non_oserror_transport_is_matched_by_class_name(self):
        # curl_cffi 的 CurlError 不在 OSError 树下 —— 无网络依赖的类名兜底
        class CurlError(Exception):
            pass
        self.assertEqual(classify_exc(CurlError("x")), NETWORK)

    def test_json_decode_is_network_not_data(self):
        import json
        self.assertEqual(classify_exc(json.JSONDecodeError("x", "y", 0)), NETWORK)

    def test_unknown_exception_is_data(self):
        class Weird(Exception):
            pass
        self.assertEqual(classify_exc(Weird("x")), DATA)
        self.assertEqual(classify_exc(ValueError("bad format")), DATA)


class TestFetchStockKlineDelegation(unittest.TestCase):
    """装配点验收（硬约束 1）：旧签名/返回形状/缓存/失败文本均不变。

    此处 stub 掉 provider 本体（真实取数受铁律 7 约束，须人授权），
    只钉「装配 → 链 → 缓存 → 异常」这条骨架。
    """

    class _Stub:
        def __init__(self, name, priority, result):
            self.name = name
            self.category = "stock_kline"
            self.priority = priority
            self.timeout_s = 1.0
            self._result = result
            self.calls = 0

        def fetch(self, **params):
            self.calls += 1
            return self._result

    def setUp(self):
        import core.stock_data as sd
        self.sd = sd
        self._orig_base = sd.BASE_DIR
        self._orig_registry = sd._registry
        self._orig_singleton = sd._registry_singleton
        self._tmp = tempfile.TemporaryDirectory()
        sd.BASE_DIR = Path(self._tmp.name)
        sd._registry_singleton = None

    def tearDown(self):
        self.sd.BASE_DIR = self._orig_base
        self.sd._registry = self._orig_registry
        self.sd._registry_singleton = self._orig_singleton
        self._tmp.cleanup()

    def _install(self, stubs):
        reg = SourceRegistry()
        for s in stubs:
            reg.register(s)
        self.sd._registry = lambda: reg
        return reg

    def _payload(self, source):
        return {"code": "000001", "market": "0",
                "klines": [["2026-01-05", 10.0, 10.5, 10.6, 9.9]],
                "source": source}

    def test_fresh_cache_short_circuits_chain(self):
        cache = Path(self._tmp.name) / "data" / "stock_klines" / "000001.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cached = self._payload("tencent")
        cache.write_text(__import__("json").dumps(cached), encoding="utf-8")

        def boom():
            raise AssertionError("TTL 内命中缓存时不得触发任何源")

        self.sd._registry = boom
        self.assertEqual(self.sd.fetch_stock_kline("000001", "0"), cached)

    def test_fallback_uses_backup_and_writes_cache(self):
        first = self._Stub("tencent", 0,
                           FetchResult(ok=False, source="tencent",
                                       error=f"{DATA}sz000001: 无 K 线"))
        second = self._Stub("eastmoney", 1,
                            FetchResult(ok=True, source="eastmoney",
                                        payload=self._payload("eastmoney")))
        self._install([first, second])
        out = self.sd.fetch_stock_kline("000001", "0")
        self.assertEqual(out["source"], "eastmoney")
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 1)
        cache = Path(self._tmp.name) / "data" / "stock_klines" / "000001.json"
        self.assertTrue(cache.exists(), "成功结果必须落缓存")
        self.assertEqual(__import__("json").loads(
            cache.read_text(encoding="utf-8")), out)

    def test_all_fail_raises_with_per_source_reasons(self):
        stubs = [
            self._Stub("tencent", 0,
                       FetchResult(ok=False, source="tencent",
                                   error=f"{NETWORK}OSError: conn reset")),
            self._Stub("eastmoney", 1,
                       FetchResult(ok=False, source="eastmoney",
                                   error=f"{DATA}em.000001: 无K线")),
            self._Stub("tushare", 2,
                       FetchResult(ok=False, source="tushare",
                                   error=f"{SKIP}TUSHARE_TOKEN 未配置")),
        ]
        self._install(stubs)
        with self.assertRaises(ValueError) as ctx:
            self.sd.fetch_stock_kline("000001", "0")
        msg = str(ctx.exception)
        self.assertIn("sz000001", msg)
        for name in ("tencent", "eastmoney", "tushare"):
            self.assertIn(name, msg, f"失败文本应逐源列出：{name}")
        cache = Path(self._tmp.name) / "data" / "stock_klines" / "000001.json"
        self.assertFalse(cache.exists(), "全灭不得写空缓存")

    def test_real_registry_order_matches_migration(self):
        reg = self.sd._registry()
        self.assertEqual([p.name for p in reg.chain_for("stock_kline")],
                         ["tencent", "eastmoney", "tushare"])
        self.assertIs(self.sd._registry(), reg, "装配点应为进程内单例")


if __name__ == "__main__":
    unittest.main(verbosity=2)
