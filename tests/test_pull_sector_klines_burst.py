"""pull_sector_klines_burst 离线测例（面 8 D8-04，2026-10-03）。

零网络纪律（铁律 8 路 c）：
- burst 跑在全局 Python（venv 无 playwright）→ 测试向 sys.modules 注入假
  playwright 模块后才 import 被测脚本；假 sync_playwright 记录 launch 次数，
  0 次真实浏览器、0 次网络请求；
- 绝不运行脚本本体（含 --help）；
- BASE/CACHE/load_codes/SLEEP 全部重定向 tempdir/桩，不碰真实 data/ 与 output/。

覆盖（D8-04 选项 1 验收口径）：
1. 裸跑（无 --authorized）拒绝退出码 3、0 次浏览器启动（闸门先于 sync_playwright）。
2. --authorized 放行后行为与旧版一致（todo 为空时写空汇总报告、0 launch）。
3. 假 playwright 路径拉取成功照常写缓存（闸门不改变拉取语义）。
"""
import sys
import tempfile
import types
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# ---- 假 playwright 注入（必须在 import 被测脚本之前） ----
LAUNCH_COUNT = {"n": 0}


class _FakePage:
    def __init__(self, response):
        self._response = response

    def goto(self, url, **kw):
        return None

    def wait_for_timeout(self, ms):
        return None

    def evaluate(self, js, url):
        return dict(self._response)


class _FakeContext:
    def __init__(self, response):
        self._response = response

    def new_page(self):
        return _FakePage(self._response)

    def route(self, pattern, handler):
        return None

    def close(self):
        return None


class _FakeBrowser:
    def __init__(self, response):
        self._response = response

    def new_context(self):
        return _FakeContext(self._response)

    def close(self):
        return None


class _FakeChromium:
    def __init__(self, response):
        self._response = response

    def launch(self, headless=True):
        LAUNCH_COUNT["n"] += 1
        return _FakeBrowser(self._response)


class _FakePlaywright:
    def __init__(self, response):
        self.chromium = _FakeChromium(response)


class _FakeSyncCtx:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return _FakePlaywright(self._response)

    def __exit__(self, *a):
        return False


def _install_fake_playwright(response=None):
    resp = response if response is not None else {"error": "stub"}
    mod = types.ModuleType("playwright")
    sub = types.ModuleType("playwright.sync_api")
    sub.sync_playwright = lambda: _FakeSyncCtx(resp)
    mod.sync_api = sub
    sys.modules["playwright"] = mod
    sys.modules["playwright.sync_api"] = sub


_install_fake_playwright()
import pull_sector_klines_burst as B  # noqa: E402


def _ok_response():
    import json
    ks = ["2015-01-05,1,2,3,4,5", "2026-10-02,2,3,4,5,6"]
    return {"status": 200, "text": json.dumps({"data": {"klines": ks}})}


class _BurstHarness(unittest.TestCase):
    """公共夹具：tempdir CACHE + 桩 load_codes + SLEEP_BURST=0。"""

    CODES = {"BK%04d" % i: {"name": "板块%d" % i, "type": "概念"}
             for i in range(1, 5)}     # 4 码（prod 口径）

    def setUp(self):
        _install_fake_playwright()
        LAUNCH_COUNT["n"] = 0
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._orig = {k: getattr(B, k) for k in
                      ("BASE", "CACHE", "load_codes", "SLEEP_BURST")}
        B.BASE = self.tmp
        B.CACHE = self.tmp / "cache"
        B.CACHE.mkdir()
        (self.tmp / "output").mkdir()
        B.load_codes = lambda *a, **k: {k: dict(v) for k, v in self.CODES.items()}
        B.SLEEP_BURST = 0

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(B, k, v)
        self._tmp.cleanup()

    def _set_response(self, resp):
        _install_fake_playwright(resp)
        B.sync_playwright = sys.modules["playwright.sync_api"].sync_playwright


class TestAuthGate(_BurstHarness):
    """D8-04（选项 1）验收：裸跑拒绝、旗标放行。"""

    def test_bare_run_rejected_exit3_zero_launch(self):
        """裸跑 → 退出码 3、0 次浏览器启动（闸门先于 load_codes/sync_playwright）。"""
        with self.assertRaises(SystemExit) as ctx:
            B.main([])
        self.assertEqual(ctx.exception.code, 3)
        self.assertEqual(LAUNCH_COUNT["n"], 0,
                         "授权闸门拒绝时不得启动浏览器")

    def test_authorized_run_proceeds(self):
        """"--authorized 放行：todo 全空（缓存 last==target）→ 0 launch + 空汇总报告。"""
        import json
        from datetime import date as _date
        target = (_date.today() if _date.today().weekday() < 5
                  else B._last_weekday()).isoformat()
        for bk in self.CODES:
            (B.CACHE / (bk + ".json")).write_text(
                json.dumps({"last": target}), encoding="utf-8")
        self._set_response({"error": "must not be called"})
        B.main(["--authorized"])
        self.assertEqual(LAUNCH_COUNT["n"], 0,
                         "todo 为空时不启动浏览器（既有语义不变）")
        out = self.tmp / "output" / ("pull_sector_klines_burst_"
                                     + target.replace("-", "") + ".md")
        self.assertTrue(out.exists(), "授权路径照常写报告")
        self.assertIn("ok=0 skip=4", out.read_text(encoding="utf-8"))


class TestAuthorizedFetch(_BurstHarness):
    """闸门不改变拉取语义：--authorized + 假 playwright 成功路径照常写缓存。"""

    def test_authorized_fetch_writes_cache(self):
        self._set_response(_ok_response())
        B.main(["--authorized"])
        self.assertEqual(LAUNCH_COUNT["n"], 1, "todo 非空 → 1 次 launch（正常轮）")
        import json
        rec = json.loads((B.CACHE / "BK0001.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["last"], "2026-10-02")
        self.assertEqual(rec["bars"], 2)
        self.assertIn("playwright-burst", rec["source"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
