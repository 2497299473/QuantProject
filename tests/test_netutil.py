"""netutil 单测（离线，无真实网络）。

覆盖：
- _sort_v4_first DNS 排序（2026-09-02 push2his IPv6 断连根因）
- _is_retryable 分类（4xx/5xx 不重试、ECONNREFUSED 不重试、断连/重置/SSL/超时重试）
- http_get 实际绕过环境变量死代理（本地 http.server + 死端口代理）
- http_get 瞬断重试后恢复
"""
import http.client
import http.server
import socket
import ssl
import threading
import unittest
import urllib.error
import urllib.request

from core import netutil


def _info(family, ip):
    return (family, socket.SOCK_STREAM, 6, "", (ip, 443))


class TestSortV4First(unittest.TestCase):
    def test_mixed_v6_first_input(self):
        infos = [_info(socket.AF_INET6, "2406::1"), _info(socket.AF_INET, "1.2.3.4")]
        out = netutil._sort_v4_first(infos)
        self.assertEqual(out[0][0], socket.AF_INET)
        self.assertEqual(out[1][0], socket.AF_INET6)

    def test_v4_only_unchanged(self):
        infos = [_info(socket.AF_INET, "1.2.3.4"), _info(socket.AF_INET, "5.6.7.8")]
        out = netutil._sort_v4_first(infos)
        self.assertEqual([i[4][0] for i in out], ["1.2.3.4", "5.6.7.8"])

    def test_single_entry_unchanged(self):
        infos = [_info(socket.AF_INET6, "2406::1")]
        self.assertEqual(netutil._sort_v4_first(infos), infos)

    def test_installed_on_socket(self):
        self.assertTrue(getattr(socket.getaddrinfo, "_quantv1_v4_first", False))


class TestIsRetryable(unittest.TestCase):
    def test_http_error_not_retryable(self):
        for code in (404, 500, 502):
            e = urllib.error.HTTPError("http://x/", code, "err", {}, None)
            self.assertFalse(netutil._is_retryable(e), f"HTTP {code}")

    def test_conn_refused_not_retryable(self):
        e = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
        self.assertFalse(netutil._is_retryable(e))
        self.assertFalse(netutil._is_retryable(ConnectionRefusedError(111, "r")))

    def test_transient_errors_retryable(self):
        self.assertTrue(netutil._is_retryable(
            urllib.error.URLError(ConnectionResetError())))
        self.assertTrue(netutil._is_retryable(ssl.SSLError("record layer failure")))
        self.assertTrue(netutil._is_retryable(socket.timeout()))
        self.assertTrue(netutil._is_retryable(TimeoutError()))
        self.assertTrue(netutil._is_retryable(http.client.RemoteDisconnected()))


class _FlakyHandler(http.server.BaseHTTPRequestHandler):
    """前 fail_times 个请求直接断连（不发响应），之后返回 JSON 200。"""
    fail_times = 0

    def do_GET(self):
        type(self).fail_times -= 1
        if type(self).fail_times >= 0:
            self.connection.close()
            return
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _serve(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/"


class TestHttpGet(unittest.TestCase):
    def test_bypasses_dead_env_proxy(self):
        """环境变量指向死代理端口（127.0.0.1:9 无监听）时仍必须直连成功。"""
        _FlakyHandler.fail_times = 0
        srv, url = _serve(_FlakyHandler)
        try:
            old = {k: __import__("os").environ.get(k)
                   for k in ("http_proxy", "https_proxy")}
            import os
            os.environ["http_proxy"] = "http://127.0.0.1:9"
            os.environ["https_proxy"] = "http://127.0.0.1:9"
            try:
                self.assertEqual(netutil.http_get_json(url, retries=0)["ok"], True)
            finally:
                for k, v in old.items():
                    if v is None:
                        __import__("os").environ.pop(k, None)
                    else:
                        __import__("os").environ[k] = v
        finally:
            srv.shutdown()

    def test_retry_recovers_from_remote_disconnect(self):
        _FlakyHandler.fail_times = 1
        srv, url = _serve(_FlakyHandler)
        try:
            self.assertTrue(netutil.http_get_json(url, retries=2, backoff=0.01)["ok"])
        finally:
            srv.shutdown()

    def test_no_retry_on_hard_failure(self):
        """retries=0 时瞬断直接上抛（不吞错）。"""
        _FlakyHandler.fail_times = 1
        srv, url = _serve(_FlakyHandler)
        try:
            with self.assertRaises(Exception):
                netutil.http_get(url, retries=0)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
