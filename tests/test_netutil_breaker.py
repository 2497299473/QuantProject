"""netutil v6 熔断器 / prime 退避 / wire 计数离线单测（面 8 D8-02/D8-06/D8-09，2026-10-03）。

零网络纪律（铁律 8 路 c）：全部 mock 构造输入——
- `_resolve_candidates` mock 掉 ⇒ 不发生真实 DNS；
- `_conn_for` mock 掉 ⇒ 不建任何 TCP/TLS 连接（连 loopback 都不用）；
- `_load_curl_cffi` mock 掉 ⇒ curl_cffi 兜底不出网。
真实传输行为（loopback http.server 断连/重试/死代理绕过）由既有
test_netutil.py（slow 层）覆盖；本文件只钉状态机、计数与分支可达性。

覆盖：
1. D8-02 熔断器：K 次连接层失败后同 host 新调用 **0 次建连** 快速失败
   （ThrottleSuspected）；成功清零；TTL 过期半开；HTTP 4xx/5xx 不计数；
   ThrottleSuspected 不可重试、classify_exc 归 network:。
2. D8-06 prime 退避：cookie 种不上时连续两次东财请求，第二次 0 次主页 GET；
   force=True 跳过硬退避；「距上次 prime 超 300s 强制重种」分支恢复可达。
3. D8-09 wire_attempts：retries=2 全失败场景 = 3；成功 = 1；curl 兜底另计。
"""
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import netutil  # noqa: E402
from core.datasource.base import NETWORK, classify_exc  # noqa: E402

HOST = "bk-test.invalid"          # 非东财域：入口 prime 不参与
URL = f"http://{HOST}/api/test"
EM_URL = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
_ONE_CANDIDATE = [(2, "203.0.113.7")]   # AF_INET，测试网段（RFC 5737，不可路由）


def _fake_conn_ok():
    """成功响应的假连接（status 200 + JSON 体）。"""
    resp = SimpleNamespace(status=200, reason="OK", read=lambda: b'{"ok": true}')
    return SimpleNamespace(request=lambda *a, **k: None,
                           getresponse=lambda: resp,
                           close=lambda: None)


def _fake_conn_http(status):
    resp = SimpleNamespace(status=status, reason="err", read=lambda: b"")
    return SimpleNamespace(request=lambda *a, **k: None,
                           getresponse=lambda: resp,
                           close=lambda: None)


class _NetutilMockMixin(unittest.TestCase):
    """公共桩：无 DNS、无建连、无 curl_cffi；每个测试后复原熔断/wire/cookie 状态。"""

    def _patch(self, conn_side_effect):
        p1 = mock.patch.object(netutil, "_resolve_candidates",
                               return_value=list(_ONE_CANDIDATE))
        p2 = mock.patch.object(netutil, "_conn_for", side_effect=conn_side_effect)
        p3 = mock.patch.object(netutil, "_load_curl_cffi", return_value=None)
        self.conn_mock = p2.start()
        for p in (p1, p3):
            p.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        self.addCleanup(p3.stop)

    def setUp(self):
        netutil.breaker_reset()
        netutil.reset_wire_attempts()
        netutil._em_cookie = ""
        netutil._em_cookie_at = 0.0
        netutil._em_last_prime = 0.0

    def tearDown(self):
        netutil.breaker_reset()
        netutil.reset_wire_attempts()
        netutil._em_cookie = ""
        netutil._em_cookie_at = 0.0
        netutil._em_last_prime = 0.0


class TestThrottleBreaker(_NetutilMockMixin):
    """D8-02：同 host 连续 K 次连接层失败 → 熔断，后续调用 0 建连。"""

    def _fail_conn(self, *a, **k):
        raise ConnectionResetError("reset by peer")

    def test_opens_after_k_failures_then_zero_connect(self):
        self._patch(self._fail_conn)
        # K-1 次：仍按原语义上抛连接层错误（未熔断）
        for _ in range(netutil._BREAKER_K - 1):
            with self.assertRaises(ConnectionResetError):
                netutil.http_get(URL, retries=0)
        # 第 K 次：失败计数达阈值，本调用仍以原错误上抛（开闸发生在计数后）
        with self.assertRaises(ConnectionResetError):
            netutil.http_get(URL, retries=0)
        self.assertEqual(self.conn_mock.call_count, netutil._BREAKER_K)
        # 第 K+1 次：ThrottleSuspected 快速失败，建连次数**不再增加**（验收核心）
        with self.assertRaises(netutil.ThrottleSuspected):
            netutil.http_get(URL, retries=0)
        self.assertEqual(self.conn_mock.call_count, netutil._BREAKER_K,
                         "熔断后必须 0 次新建连")
        # 带 retries 也不得放大：时间重试与 curl 兜底均被跳过
        with self.assertRaises(netutil.ThrottleSuspected):
            netutil.http_get(URL, retries=2, backoff=0)
        self.assertEqual(self.conn_mock.call_count, netutil._BREAKER_K)

    def test_throttle_not_retryable_and_classified_network(self):
        exc = netutil.ThrottleSuspected("x")
        self.assertFalse(netutil._is_retryable(exc),
                         "熔断异常不得进时间重试/curl 兜底")
        self.assertEqual(classify_exc(exc), NETWORK,
                         "熔断=频控征兆，必须计入健康度降级")

    def test_success_resets_consecutive_count(self):
        calls = {"n": 0}

        def conn(*a, **k):
            calls["n"] += 1
            if calls["n"] <= netutil._BREAKER_K - 1:      # 前 K-1 次失败
                raise ConnectionResetError("reset")
            if calls["n"] == netutil._BREAKER_K:          # 第 K 次成功 → 清零
                return _fake_conn_ok()
            raise ConnectionResetError("reset")           # 之后又失败

        self._patch(conn)
        for _ in range(netutil._BREAKER_K - 1):
            with self.assertRaises(ConnectionResetError):
                netutil.http_get(URL, retries=0)
        self.assertEqual(netutil.http_get_json(URL, retries=0)["ok"], True)
        # 清零后再失败 K-1 次仍不熔断（连续口径，非累计口径）
        for _ in range(netutil._BREAKER_K - 1):
            with self.assertRaises(ConnectionResetError):
                netutil.http_get(URL, retries=0)
        netutil._breaker_check(HOST)                      # 未达 K：不抛
        with self.assertRaises(ConnectionResetError):     # 第 K 次失败 → 开闸
            netutil.http_get(URL, retries=0)
        with self.assertRaises(netutil.ThrottleSuspected):
            netutil._breaker_check(HOST)

    def test_ttl_expiry_half_opens(self):
        self._patch(self._fail_conn)
        for _ in range(netutil._BREAKER_K + 1):
            with self.assertRaises(Exception):
                netutil.http_get(URL, retries=0)
        with self.assertRaises(netutil.ThrottleSuspected):
            netutil._breaker_check(HOST)
        # 把开闸时刻拨到 TTL 之外 → 半开：放行试探（仍以连接层错误上抛）
        netutil._breaker_opened_at[HOST] = time.time() - netutil._BREAKER_TTL - 10
        with self.assertRaises(ConnectionResetError):
            netutil.http_get(URL, retries=0)

    def test_http_errors_do_not_count(self):
        self._patch(lambda *a, **k: _fake_conn_http(500))
        import urllib.error
        for _ in range(netutil._BREAKER_K * 2):           # 服务端明确响应 ≠ 频控签名
            with self.assertRaises(urllib.error.HTTPError):
                netutil.http_get(URL, retries=0)
        netutil._breaker_check(HOST)                      # 不得熔断（不抛即过）
        self.assertEqual(netutil._breaker_fails.get(HOST, 0), 0)

    def test_conn_refused_does_not_count(self):
        def refused(*a, **k):
            raise ConnectionRefusedError("nobody listening")
        self._patch(refused)
        for _ in range(netutil._BREAKER_K * 2):
            with self.assertRaises(ConnectionRefusedError):
                netutil.http_get(URL, retries=0)
        netutil._breaker_check(HOST)
        self.assertEqual(netutil._breaker_fails.get(HOST, 0), 0)

    def test_breaker_reset_explicit(self):
        self._patch(self._fail_conn)
        for _ in range(netutil._BREAKER_K + 1):
            with self.assertRaises(Exception):
                netutil.http_get(URL, retries=0)
        with self.assertRaises(netutil.ThrottleSuspected):
            netutil._breaker_check(HOST)
        netutil.breaker_reset(HOST)
        netutil._breaker_check(HOST)                      # 复位后放行（不抛）


class TestPrimeBackoff(_NetutilMockMixin):
    """D8-06：prime 300s 退避——种不上 cookie 时不再按逻辑请求数线性重播主页 GET。"""

    def _fake_cc_counting(self):
        counter = {"gets": 0}

        def get(url, **kw):
            counter["gets"] += 1
            raise ConnectionError("stub: 主页也种不上（模拟 v5 实测 cookies:[]）")

        return SimpleNamespace(get=get), counter

    def test_two_em_requests_second_primes_zero_homepage_get(self):
        """验收口径：连续两次东财请求，第二次 prime 0 次主页 GET。"""
        self._patch(self._raise_reset)
        cc, counter = self._fake_cc_counting()
        with mock.patch.object(netutil, "_load_curl_cffi", return_value=cc):
            for _ in range(2):
                with self.assertRaises(ConnectionResetError):
                    netutil.http_get(EM_URL, retries=0)
        self.assertEqual(counter["gets"], 1,
                         "第二次东财请求的入口 prime 必须被 300s 退避抑制")

    def test_force_bypasses_backoff(self):
        self._patch(self._raise_reset)
        cc, counter = self._fake_cc_counting()
        with mock.patch.object(netutil, "_load_curl_cffi", return_value=cc):
            netutil.prime_eastmoney_session()             # 第 1 次（计数 1）
            netutil.prime_eastmoney_session()             # 退避（仍 1）
            netutil.prime_eastmoney_session(force=True)   # 强制（2）
        self.assertEqual(counter["gets"], 2)

    def test_reprime_branch_reachable_after_gap(self):
        """v5「距上次 prime 超 300s → 强制重种再走一轮」分支：修复后真实可达。"""
        self._patch(self._raise_reset)
        netutil._em_last_prime = time.time() - netutil._REPRIME_GAP - 100
        with mock.patch.object(netutil, "prime_eastmoney_session",
                               return_value=True) as prime_mock:
            with self.assertRaises(ConnectionResetError):
                netutil.http_get(EM_URL, retries=1, backoff=0)
        force_calls = [c for c in prime_mock.call_args_list
                       if c.kwargs.get("force") is True]
        self.assertEqual(len(force_calls), 1, "重种分支必须被执行（旧版永不可达）")
        # retries=1：2 次原生尝试 + 重种后 1 次 = 3 次建连尝试
        self.assertEqual(self.conn_mock.call_count, 3)

    @staticmethod
    def _raise_reset(*a, **k):
        raise ConnectionResetError("reset by peer")


class TestWireAttempts(_NetutilMockMixin):
    """D8-09：wire_attempts = 真实发包数（建连/curl/prime 各 +1）。"""

    def test_retries2_all_fail_counts_three(self):
        def fail(*a, **k):
            raise ConnectionResetError("reset")
        self._patch(fail)
        with self.assertRaises(ConnectionResetError):
            netutil.http_get(URL, retries=2, backoff=0)
        self.assertGreaterEqual(netutil.wire_attempts(), 3,
                                "验收：retries=2 场景 wire_attempts ≥ 3")
        self.assertEqual(netutil.wire_attempts(), 3)      # 单候选 IP，恰好 3

    def test_success_counts_one(self):
        self._patch(lambda *a, **k: _fake_conn_ok())
        netutil.http_get_json(URL, retries=0)
        self.assertEqual(netutil.wire_attempts(), 1)

    def test_curl_fallback_counted_separately(self):
        def fail(*a, **k):
            raise ConnectionResetError("reset")
        p1 = mock.patch.object(netutil, "_resolve_candidates",
                               return_value=list(_ONE_CANDIDATE))
        p2 = mock.patch.object(netutil, "_conn_for", side_effect=fail)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

        cc_calls = {"n": 0}

        def cc_get(url, **kw):
            cc_calls["n"] += 1
            raise ConnectionError("stub curl down")

        cc = SimpleNamespace(get=cc_get)
        with mock.patch.object(netutil, "_load_curl_cffi", return_value=cc):
            with self.assertRaises(ConnectionResetError):
                netutil.http_get(URL, retries=2, backoff=0)
        # 3 次原生建连 + 1 次 curl_cffi 兜底 = 4（上抛的仍是原生异常，语义不变）
        self.assertEqual(netutil.wire_attempts(), 4)
        self.assertEqual(cc_calls["n"], 1)

    def test_reset(self):
        self._patch(lambda *a, **k: _fake_conn_ok())
        netutil.http_get(URL, retries=0)
        netutil.reset_wire_attempts()
        self.assertEqual(netutil.wire_attempts(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
