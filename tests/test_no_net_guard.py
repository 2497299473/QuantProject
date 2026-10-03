"""core.no_net_guard 离线测例（面 8 D8-08，2026-10-03）。

零网络纪律（铁律 8 路 c）：守卫探测只对 127.0.0.1:1（无监听端口）发包，
且守卫在位时该发包被 NoNetViolation 拦截在 Python 层——这正是 selftest 的
设计语义（无守卫时是 ConnectionRefusedError，有守卫时是我方异常）。
curl_cffi 侧全部用桩（替换 _load_curl_cffi / 计数桩 cc.get），0 次真实出网。

覆盖：
1. D8-08 验收：block_curl=True 时 prime_eastmoney_session(force=True) 返回 False
   且 curl_cffi 0 次调用（selftest 扩展）；restore 后原函数还原（幂等）。
2. block_curl=False（缺省）：_load_curl_cffi 不被触碰——既有装载点
   （drift_monitor / backtest_* 三处）行为逐项不变。
3. 守卫本体语义回归：connect/sendto 拦截、block_construction 构造即抛、
   restore 幂等、socket.create_connection 同封。
4. 接线断言（源码序）：build_panel_dlite 两个审计轨装载点均带 block_curl=True。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import netutil, no_net_guard  # noqa: E402


class _NetutilStateMixin(unittest.TestCase):
    """netutil cookie/计数状态隔离（prime 检查会临时改动，检完还原）。"""

    def setUp(self):
        self._saved = (netutil._em_cookie, netutil._em_cookie_at,
                       netutil._em_last_prime, netutil._curl_cffi_mod,
                       netutil._curl_cffi_checked)
        netutil._em_cookie = ""
        netutil._em_cookie_at = 0.0
        netutil._em_last_prime = 0.0
        netutil._curl_cffi_checked = False
        netutil._curl_cffi_mod = None

    def tearDown(self):
        (netutil._em_cookie, netutil._em_cookie_at, netutil._em_last_prime,
         netutil._curl_cffi_mod, netutil._curl_cffi_checked) = self._saved


class TestBlockCurl(_NetutilStateMixin):
    """D8-08 验收：守卫开启后 prime 返回 False 且 curl_cffi 0 次调用。"""

    def test_prime_returns_false_and_zero_curl_calls(self):
        cc_calls = {"n": 0}

        class _CountingCC:
            @staticmethod
            def get(url, **kw):
                cc_calls["n"] += 1
                raise AssertionError("守卫开启期间不得真实调用 curl_cffi")

        restore = no_net_guard.install("[test] block_curl 档", block_curl=True)
        try:
            with mock.patch.object(netutil, "_curl_cffi_mod", _CountingCC), \
                    mock.patch.object(netutil, "_curl_cffi_checked", True):
                # 守卫把 _load_curl_cffi 换成返回 None 的桩 → prime 静默降级
                self.assertIsNone(netutil._load_curl_cffi())
                self.assertFalse(netutil.prime_eastmoney_session(force=True))
            self.assertEqual(cc_calls["n"], 0, "curl_cffi 必须 0 次调用")
        finally:
            restore()

    def test_selftest_extension_reports_curl_blocked(self):
        ok, detail = no_net_guard.selftest("[test] block_curl selftest",
                                           block_curl=True)
        self.assertTrue(ok, detail)
        self.assertIn("'connect': 'blocked'", detail)
        self.assertIn("'sendto': 'blocked'", detail)
        self.assertIn("'curl_cffi': 'blocked'", detail)

    def test_restore_returns_original_loader(self):
        orig = netutil._load_curl_cffi
        restore = no_net_guard.install("[test]", block_curl=True)
        self.assertIsNot(netutil._load_curl_cffi, orig, "install 应替换加载器")
        restore()
        self.assertIs(netutil._load_curl_cffi, orig, "restore 必须还原原函数")
        restore()                      # 幂等：重复 restore 不得二次污染
        self.assertIs(netutil._load_curl_cffi, orig)

    def test_prime_cookie_state_not_polluted(self):
        """selftest 的 prime 检查不得改写进程 cookie 状态（长驻进程纪律）。"""
        netutil._em_cookie = "k=v"
        netutil._em_cookie_at = 123.0
        ok, _ = no_net_guard.selftest(block_curl=True)
        self.assertTrue(ok)
        self.assertEqual(netutil._em_cookie, "k=v")
        self.assertEqual(netutil._em_cookie_at, 123.0)


class TestDefaultUnaffected(_NetutilStateMixin):
    """缺省（block_curl=False）：既有装载点行为逐项不变。"""

    def test_loader_untouched_by_default_install(self):
        orig = netutil._load_curl_cffi
        restore = no_net_guard.install("[test] 缺省档")
        try:
            self.assertIs(netutil._load_curl_cffi, orig,
                          "缺省档不得触碰 curl 加载器（drift_monitor 等三处既有装载点）")
        finally:
            restore()

    def test_default_selftest_shape_unchanged(self):
        ok, detail = no_net_guard.selftest("[test] 缺省")
        self.assertTrue(ok, detail)
        self.assertNotIn("curl_cffi", detail, "缺省 selftest 不追加第三项（旧口径）")

    def test_socket_guard_still_works_with_default(self):
        restore = no_net_guard.install("[test]")
        try:
            import socket
            s = socket.socket()
            with self.assertRaises(no_net_guard.NoNetViolation):
                s.connect(("127.0.0.1", 1))
            with self.assertRaises(no_net_guard.NoNetViolation):
                socket.create_connection(("127.0.0.1", 1), timeout=0.1)
        finally:
            restore()

    def test_block_construction_still_works(self):
        restore = no_net_guard.install("[test] 审计轨", block_construction=True)
        try:
            import socket
            with self.assertRaises(no_net_guard.NoNetViolation):
                socket.socket()          # 构造即抛（最严口径不变）
        finally:
            restore()


class TestAuditTrackWiring(unittest.TestCase):
    """接线断言（源码序）：审计轨装载点均带 block_curl=True（D8-08 修法②）。"""

    def test_build_panel_dlite_both_points_enable_block_curl(self):
        src = (BASE_DIR / "experiments" / "forecast_lab" /
               "build_panel_dlite.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("block_construction=True, block_curl=True"), 2,
                         "两个审计轨装载点（_selftest 装载 + 主流程）都必须封 curl 旁路")

    def test_default_track_points_unchanged(self):
        """缺省轨三处（drift_monitor/backtest_*×2）保持 block_curl 缺省——
        它们不碰 netutil 东财路径，维持旧语义最小改动。"""
        for name in ("drift_monitor.py", "backtest_early_stopping_ab.py",
                     "backtest_pit1455_matrix.py"):
            src = (BASE_DIR / name).read_text(encoding="utf-8")
            self.assertIn("block_construction=False)", src, name)
            self.assertNotIn("block_curl", src,
                             f"{name} 属缺省轨，本轮不扩展（最小 diff 纪律）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
