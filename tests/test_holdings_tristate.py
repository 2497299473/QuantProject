"""持仓 F10 三态判定测试（2026-09-09 静默缺年修复，Summer 拍板合入生产）。

背景：002112/2020 在同日两次只读冻结中从 4 期静默变 0 期且**无任何告警**——HTTP 200 的
降级页被解析成 []，holdings_history 视为成功、不计入 failed_years，样本少一整年 →
RankIC 漂移 ±0.03~0.05。合入 `fetch_holdings_year` 三态判定后，降级页必须抛
DegradedResponse、走既有重试与缺年告警通道；确证空态仍正常返回 []。

与 test_holdings_visibility.py 的分工：那个文件测的是 holdings_history 的重试/告警接线
（且为 RUN_SLOW_TESTS 重型门控——每次失败年份内部 sleep 2+5 秒）；本文件测 fetch 层
三态判定，直接 mock netutil.http_get 喂响应、不触发退避睡眠，因此属**快路径默认运行**。
零真实网络请求。
"""
import re
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import lookthrough  # noqa: E402

_ROW = ("<tr><td class='tol'><a href='unify/r/1.600000'>浦发银行</a></td>"
        "<td class='tor'>9.00%</td></tr>")
_OK_TEXT = ("<div class='boxitem w790'><h4 class='t'>截止至："
            f"<font class='px12'>2020-12-31</font></h4><table>{_ROW}</table>")
_CONFIRMED_EMPTY = ('var apidata={ content:"<div>暂无数据</div>", count:0 };')
_DEGRADED = '<div class="antibot">url_required, verify</div>'
_PARSE_MISMATCH = "<div class='boxitem w790'><h4>改版了没有日期</h4></div>"


class _RawDirSandbox(unittest.TestCase):
    """离线测试统一把 `_RAW_DIR` 重定向到临时目录。

    教训（2026-09-09 首版漏掉）：未隔离时 9 项测试跑完在
    `forecast_outputs/f10_raw/` 留下 2 份**合成** antibot 样本，污染了“服务端真
    降级取证”专用目录——那个目录的唯一用途是留证据，混进测试件后既无法数真实
    事故，也会误导后续根因回溯。
    """

    def setUp(self):
        self._raw_td = tempfile.TemporaryDirectory()
        self._raw_patch = mock.patch.object(
            lookthrough, "_RAW_DIR", Path(self._raw_td.name))
        self._raw_patch.start()

    def tearDown(self):
        self._raw_patch.stop()
        self._raw_td.cleanup()


class TestFetchTriState(_RawDirSandbox):
    """fetch_holdings_year 三态：正常 / 确证空态 / 降级抛异常。"""

    def test_normal_response_parses(self):
        with mock.patch.object(lookthrough.netutil, "http_get", return_value=_OK_TEXT):
            out = lookthrough.fetch_holdings_year("002112", 2020)
        self.assertEqual([s["date"] for s in out], ["2020-12-31"])
        self.assertEqual(out[0]["holdings"][0]["code"], "600000")

    def test_confirmed_empty_returns_empty_list(self):
        """真无披露（新基金常见）→ 正常返回 []，不抛。"""
        with mock.patch.object(lookthrough.netutil, "http_get",
                               return_value=_CONFIRMED_EMPTY):
            self.assertEqual(lookthrough.fetch_holdings_year("022853", 2020), [])

    def test_degraded_page_raises_suspect(self):
        """降级页 0 期且无空态标记 → 必须抛（静默缺年的主路径）。"""
        with mock.patch.object(lookthrough.netutil, "http_get", return_value=_DEGRADED):
            with self.assertRaises(lookthrough.DegradedResponse) as cm:
                lookthrough.fetch_holdings_year("002112", 2020)
        self.assertEqual(cm.exception.kind, "SUSPECT_DEGRADED")
        self.assertIn("002112/2020", str(cm.exception))

    def test_parse_mismatch_raises(self):
        """有 boxitem 但解析 0 期（改版/结构漂移）→ 抛。"""
        with mock.patch.object(lookthrough.netutil, "http_get",
                               return_value=_PARSE_MISMATCH):
            with self.assertRaises(lookthrough.DegradedResponse) as cm:
                lookthrough.fetch_holdings_year("002112", 2020)
        self.assertEqual(cm.exception.kind, "PARSE_MISMATCH")

    def test_raw_dump_written_and_survives_failure(self):
        """降级时原始响应必须留档（否则像 08-29 那样无据可查）；写档失败不得掩盖异常。"""
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(lookthrough, "_RAW_DIR", Path(td) / "raw"), \
                 mock.patch.object(lookthrough.netutil, "http_get",
                                   return_value=_DEGRADED):
                with self.assertRaises(lookthrough.DegradedResponse) as cm:
                    lookthrough.fetch_holdings_year("002112", 2020)
            files = list((Path(td) / "raw").glob("002112_2020_*.html"))
            self.assertEqual(len(files), 1)
            self.assertIn("antibot", files[0].read_text(encoding="utf-8"))
            self.assertEqual(Path(cm.exception.raw_path), files[0])
        # 写档失败（模拟目录不可写）：仍抛异常，raw_path 降级为 None
        with mock.patch.object(lookthrough, "_dump_raw", return_value=None), \
             mock.patch.object(lookthrough.netutil, "http_get", return_value=_DEGRADED):
            with self.assertRaises(lookthrough.DegradedResponse) as cm2:
                lookthrough.fetch_holdings_year("002112", 2020)
        self.assertIsNone(cm2.exception.raw_path)
        self.assertIn("写档失败", str(cm2.exception))

    def test_http_exception_propagates_unchanged(self):
        """网络异常路径行为不变：仍由 netutil 抛、由外层重试接住。"""
        with mock.patch.object(lookthrough.netutil, "http_get",
                               side_effect=OSError("connection refused")):
            with self.assertRaises(OSError):
                lookthrough.fetch_holdings_year("002112", 2020)


class TestHistoryWiring(_RawDirSandbox):
    """holdings_history 接线：降级计入 failed_years、告警能区分降级 vs 断网、不嵌套重试。

    本类测试都 mock 了 `fetch_holdings_year`，但 `test_no_retry_nesting` 前半段走真实
    fetch + mock http_get，仍会触发留档→所以同样继承沙箱。
    """

    def test_degraded_year_counted_and_hint_distinguishes(self):
        def fake_fetch(code, year):
            if year == 2020:
                raise lookthrough.DegradedResponse(
                    f"F10 {code}/{year} 返回 0 期但非确证空态", kind="SUSPECT_DEGRADED")
            return [{"date": f"{year}-03-31",
                     "holdings": [{"market": "1", "code": "600000",
                                   "name": "浦发银行", "pct": 9.0}]}]
        with mock.patch.object(lookthrough, "fetch_holdings_year", side_effect=fake_fetch), \
             mock.patch("time.sleep"), \
             warnings.catch_warnings(record=True) as ws:
            warnings.simplefilter("always")
            out = lookthrough.holdings_history("999999")
        self.assertTrue(out)                       # 成功年份仍返回
        self.assertTrue(any("999999" in str(w.message) for w in ws))
        joined = " ".join(str(w.message) for w in ws)
        self.assertIn("2020", joined)              # 缺年点名
        self.assertIn("降级页", joined)            # 降级 vs 断网可区分
        self.assertNotIn("代理失效", joined)

    def test_network_failure_keeps_proxy_hint(self):
        """普通网络失败仍走"代理失效"措辞（两种 hint 不互串）。"""
        with mock.patch.object(lookthrough, "fetch_holdings_year",
                               side_effect=OSError("timeout")), \
             mock.patch("time.sleep"), \
             warnings.catch_warnings(record=True) as ws:
            warnings.simplefilter("always")
            lookthrough.holdings_history("999999")
        joined = " ".join(str(w.message) for w in ws)
        self.assertIn("代理失效", joined)
        self.assertNotIn("降级页", joined)

    def test_no_retry_nesting(self):
        """降级每年代恰好 3 次请求（1+2），不得出现研究版 retries=2 的嵌套放大（9 次）。"""
        calls = []

        def fake_http_get(*a, **k):
            calls.append(k)
            return _DEGRADED
        with mock.patch.object(lookthrough.netutil, "http_get", side_effect=fake_http_get), \
             mock.patch("time.sleep"):
            with self.assertRaises(lookthrough.DegradedResponse):
                lookthrough.fetch_holdings_year("002112", 2020)
        self.assertEqual(len(calls), 1)            # 单次 fetch 只发 1 请求
        outer = []

        def counting_fetch(code, year):
            outer.append(year)
            raise lookthrough.DegradedResponse("x", kind="SUSPECT_DEGRADED")
        with mock.patch.object(lookthrough, "fetch_holdings_year", side_effect=counting_fetch), \
             mock.patch("time.sleep"), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            lookthrough.holdings_history("999999")
        years_expected = lookthrough._cfg()["history_years"]
        self.assertEqual(len(outer), 3 * len(years_expected))   # 每年恰好 3 次


if __name__ == "__main__":
    unittest.main()
