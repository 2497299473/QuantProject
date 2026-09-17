"""基金净值多源层离线测例（V4 步 4，2026-09-17）——stub netutil，零网络、不碰真实 data/。

对齐重构方案 §四步 4 验收「东财失败时可回落新浪，_source 正确标注」：
真实回落需拉数（受铁律 7 约束，须人授权），此处把**网络之外的全部逻辑**钉死——
两 provider 的解析/失败分类 + 装配点 load_fund 的链行为（成功/回落/全灭降级/缓存命中）。

覆盖：
1. EastmoneyFundNavProvider：正常解析 / 无 netWorthTrend → data: / 空序列 → data: /
   未注入 pz_url → data: / 网络异常 → network:
2. SinaFundNavProvider：正常解析升序去重 / 行数可疑 → data: / 网络异常 → network:
3. 契约属性：eastmoney priority 0、sina priority 1、category=fund_nav
4. 装配点 load_fund：东财成功→fresh+source=eastmoney；东财失败→回落→fresh+source=sina；
   全链失败+有缓存→cache:fallback；TTL 内命中→cache；fresh 不覆盖 cache 缓存三态口径
5. 公开面 fetch_pingzhongdata 单源旧契约（失败抛 ValueError）
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import data_loader  # noqa: E402
from core.datasource.base import DATA, NETWORK  # noqa: E402
from core.datasource.providers import (  # noqa: E402
    fund_eastmoney, fund_sina,
)


def _pz_text(name: str, points: list) -> str:
    """造 pingzhongdata JS 文本片段（name + Data_netWorthTrend）。"""
    return (f'var fS_name = "{name}";\n'
            f'Data_netWorthTrend = {json.dumps(points)};')


def _nav_points(n: int, start_ms: int = 1_600_000_000_000) -> list:
    # 每天一条，x 为毫秒时间戳，y 为净值
    return [{"x": start_ms + i * 86_400_000, "y": 1.0 + i * 0.01} for i in range(n)]


class TestEastmoneyFundProvider(unittest.TestCase):
    def setUp(self):
        self.p = fund_eastmoney.EastmoneyFundNavProvider()

    def test_parses_name_and_navs(self):
        text = _pz_text("某基金", _nav_points(3))
        with mock.patch.object(fund_eastmoney.netutil, "http_get",
                               return_value=text):
            r = self.p.fetch(code="002112", pz_url="x/{code}.js")
        self.assertTrue(r.ok)
        self.assertEqual(r.payload["name"], "某基金")
        self.assertEqual(len(r.payload["navs"]), 3)
        self.assertEqual(r.payload["source"], "eastmoney")
        # 升序：日期字符串可比较
        dates = [d for d, _ in r.payload["navs"]]
        self.assertEqual(dates, sorted(dates))

    def test_missing_trend_is_data_failure(self):
        with mock.patch.object(fund_eastmoney.netutil, "http_get",
                               return_value='var fS_name = "x";'):
            r = self.p.fetch(code="002112", pz_url="x/{code}.js")
        self.assertFalse(r.ok)
        self.assertTrue(r.error.startswith(DATA))
        self.assertIn("Data_netWorthTrend", r.error)

    def test_empty_series_is_data_failure(self):
        text = _pz_text("x", [])
        with mock.patch.object(fund_eastmoney.netutil, "http_get",
                               return_value=text):
            r = self.p.fetch(code="002112", pz_url="x/{code}.js")
        self.assertFalse(r.ok)
        self.assertTrue(r.error.startswith(DATA))

    def test_no_url_is_data_failure(self):
        r = self.p.fetch(code="002112", pz_url="")
        self.assertFalse(r.ok)
        self.assertTrue(r.error.startswith(DATA))

    def test_network_error_prefixed_network(self):
        with mock.patch.object(fund_eastmoney.netutil, "http_get",
                               side_effect=OSError("boom")):
            r = self.p.fetch(code="002112", pz_url="x/{code}.js")
        self.assertFalse(r.ok)
        self.assertTrue(r.error.startswith(NETWORK))


class TestSinaFundProvider(unittest.TestCase):
    def setUp(self):
        self.p = fund_sina.SinaFundNavProvider()

    def _js(self, n: int):
        rows = [{"fbrq": f"2026-01-{i:02d}T00:00:00", "jjjz": str(1.0 + i * 0.01)}
                for i in range(n)]
        return {"result": {"data": {"data": rows}}}

    def test_parses_sorted_navs(self):
        with mock.patch.object(fund_sina.netutil, "http_get_json",
                               return_value=self._js(60)):
            r = self.p.fetch(code="002112")
        self.assertTrue(r.ok)
        self.assertEqual(len(r.payload["navs"]), 60)
        self.assertEqual(r.payload["source"], "sina")
        self.assertNotIn("name", r.payload)   # 新浪无名称字段，交装配点回退
        dates = [d for d, _ in r.payload["navs"]]
        self.assertEqual(dates, sorted(dates))

    def test_suspiciously_few_rows_is_data_failure(self):
        with mock.patch.object(fund_sina.netutil, "http_get_json",
                               return_value=self._js(10)):
            r = self.p.fetch(code="002112")
        self.assertFalse(r.ok)
        self.assertTrue(r.error.startswith(DATA))

    def test_network_error_prefixed_network(self):
        with mock.patch.object(fund_sina.netutil, "http_get_json",
                               side_effect=OSError("boom")):
            r = self.p.fetch(code="002112")
        self.assertFalse(r.ok)
        self.assertTrue(r.error.startswith(NETWORK))


class TestProviderContract(unittest.TestCase):
    def test_priority_and_category(self):
        em = fund_eastmoney.EastmoneyFundNavProvider()
        si = fund_sina.SinaFundNavProvider()
        self.assertEqual(em.category, "fund_nav")
        self.assertEqual(si.category, "fund_nav")
        self.assertLess(em.priority, si.priority)   # 东财链首，新浪备源
        self.assertGreater(em.timeout_s, 0)
        self.assertGreater(si.timeout_s, 0)


class TestLoadFundChain(unittest.TestCase):
    """装配点链行为：真走 run_chain + health，仅 stub 两 provider 的 netutil。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        data_loader._registry_singleton = None      # 隔离健康度
        self._orig_cache_path = data_loader._cache_path
        data_loader._cache_path = lambda code: self.tmp / f"{code}.json"
        # fetch_lsjz 是东财 f10 独立接口（不在多源链内），必须 stub：
        # 否则 load_fund 成功分支会真发东财请求，违反本文件零网络纪律。
        # 抛异常即走既有「申赎拿不到不阻断」分支（_lsjz_error 留痕）。
        self._lsjz = mock.patch.object(
            data_loader, "fetch_lsjz", side_effect=OSError("stub: no network in tests"))
        self._lsjz.start()

    def tearDown(self):
        self._lsjz.stop()
        data_loader._cache_path = self._orig_cache_path
        data_loader._registry_singleton = None
        self._tmp.cleanup()

    def _seed_cache(self, code: str, source: str = "fresh"):
        (self.tmp / f"{code}.json").write_text(json.dumps({
            "code": code, "name": "旧缓存名", "navs": [["2020-01-01", 1.0]],
            "_source": source,
        }), encoding="utf-8")

    def _both_fail(self):
        return (mock.patch.object(fund_eastmoney.netutil, "http_get",
                                  side_effect=OSError("em down")),
                mock.patch.object(fund_sina.netutil, "http_get_json",
                                  side_effect=OSError("sina down")))

    def test_eastmoney_success_marks_source(self):
        text = _pz_text("东财名", _nav_points(5))
        with mock.patch.object(fund_eastmoney.netutil, "http_get",
                               return_value=text):
            fund = data_loader.load_fund("002112", force_refresh=True)
        self.assertEqual(fund["_source"], "fresh")
        self.assertEqual(fund.get("source"), "eastmoney")
        self.assertEqual(fund["name"], "东财名")

    def test_eastmoney_fail_falls_back_to_sina(self):
        em = mock.patch.object(fund_eastmoney.netutil, "http_get",
                               side_effect=OSError("em down"))
        si = mock.patch.object(fund_sina.netutil, "http_get_json",
                               return_value={"result": {"data": {"data": [
                                   {"fbrq": f"2026-01-{i:02d}T00:00:00",
                                    "jjjz": str(2.0 + i * 0.01)} for i in range(60)]}}})
        with em, si:
            fund = data_loader.load_fund("002112", force_refresh=True)
        self.assertEqual(fund["_source"], "fresh")
        self.assertEqual(fund.get("source"), "sina")     # 回落成功，实际源标 sina
        self.assertGreaterEqual(len(fund["navs"]), 50)

    def test_all_fail_with_cache_is_fallback(self):
        self._seed_cache("002112")
        em, si = self._both_fail()
        with em, si:
            fund = data_loader.load_fund("002112", force_refresh=True)
        self.assertTrue(fund["_source"].startswith("cache:fallback"))
        self.assertEqual(fund["name"], "旧缓存名")

    def test_all_fail_no_cache_raises(self):
        em, si = self._both_fail()
        with em, si:
            with self.assertRaises(ValueError):
                data_loader.load_fund("999999", force_refresh=True)

    def test_ttl_cache_hit_short_circuits(self):
        self._seed_cache("002112")
        # 缓存 mtime 刚写入，TTL=12h 内命中；不应触发任何网络（无 mock，触发即报错）
        fund = data_loader.load_fund("002112", force_refresh=False)
        self.assertEqual(fund["_source"], "cache")


class TestPublicSingleSource(unittest.TestCase):
    def test_fetch_pingzhongdata_raises_on_failure(self):
        with mock.patch.object(fund_eastmoney.netutil, "http_get",
                               side_effect=OSError("down")):
            with self.assertRaises(ValueError):
                data_loader.fetch_pingzhongdata("002112")


if __name__ == "__main__":
    unittest.main(verbosity=2)
