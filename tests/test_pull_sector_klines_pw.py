"""pull_sector_klines_pw 离线测例（面 8 D8-05，2026-10-03）。

零网络纪律（铁律 8 路 c）：
- 本脚本 import playwright（venv 故意不装）→ 测试向 sys.modules 注入**假
  playwright 模块**后才 import 被测脚本；假 sync_playwright 记录 launch 次数，
  任何 evaluate 都是纯内存桩——**0 次真实浏览器启动、0 次网络请求**；
- 绝不运行脚本本体（含 --help 直接调 main(['--help'])，argparse 在解析层退出，
  这正是 D8-05 要钉住的安全语义）；
- BASE/CACHE/load_codes/SLEEP 全部重定向 tempdir/桩，不碰真实 data/ 与 output/。

覆盖（D8-05 验收口径）：
1. `--help` 退出码 0、0 次 playwright 启动（旧版 sys.argv[1] 直读会把 --help
   当日期字符串 → 全量 65 码真实拉取；同型事故 2026-10-02 01:35 实录）。
2. 旧裸位置参数路径死亡：main(['2026-09-04']) → argparse 报错退出码 2。
3. 显式 --date 路径行为与现版一致：TARGET 进报告文件名与记录 fetched_at，
   成功码照常写缓存。
4. 连败重启封顶 MAX_RESTARTS=2：达上限仍连败 → 整轮中止、报告记 aborted=True、
   launch 总数 = 1 + 2（09-08「自动重启 12 次全灭」事故形态封口）。
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
        self.reloads = 0

    def goto(self, url, **kw):
        return None

    def wait_for_timeout(self, ms):
        return None

    def reload(self, **kw):
        self.reloads += 1

    def evaluate(self, js, url):
        return dict(self._response)


class _FakeContext:
    def __init__(self, response):
        self._response = response
        self.closed = False

    def new_page(self):
        return _FakePage(self._response)

    def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self, response):
        self._response = response
        self.closed = False

    def new_context(self):
        return _FakeContext(self._response)

    def close(self):
        self.closed = True


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


_FAKE_RESPONSE = {"error": "stub: no network in tests"}   # 缺省：全失败


def _install_fake_playwright(response=None):
    resp = response if response is not None else dict(_FAKE_RESPONSE)
    mod = types.ModuleType("playwright")
    sub = types.ModuleType("playwright.sync_api")
    sub.sync_playwright = lambda: _FakeSyncCtx(resp)
    mod.sync_api = sub
    sys.modules["playwright"] = mod
    sys.modules["playwright.sync_api"] = sub


_install_fake_playwright()
import pull_sector_klines_pw as PW  # noqa: E402


def _ok_klines_response():
    """成功响应：2 根 K 线（首根早于 OOS_START → covers_oos=是）。"""
    import json
    ks = ["2019-12-31,1,2,3,4,5", "2026-09-04,2,3,4,5,6"]
    return {"status": 200, "text": json.dumps({"data": {"klines": ks}})}


class _PwHarness(unittest.TestCase):
    """公共夹具：tempdir BASE/CACHE + 桩 load_codes + SLEEP=0 + launch 计数复位。"""

    CODES = {"BK%04d" % i: {"name": "板块%d" % i, "type": "概念"}
             for i in range(1, 21)}     # 20 码：足够触发重启用尽（需 16 次迭代）

    def setUp(self):
        _install_fake_playwright()      # 每个测试重置响应与计数
        LAUNCH_COUNT["n"] = 0
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._orig = {k: getattr(PW, k) for k in ("BASE", "CACHE", "load_codes", "SLEEP")}
        PW.BASE = self.tmp
        PW.CACHE = self.tmp / "cache"
        PW.CACHE.mkdir()
        (self.tmp / "output").mkdir()          # 报告落点（tmp BASE 下需预建）
        PW.load_codes = lambda: {k: dict(v) for k, v in self.CODES.items()}
        PW.SLEEP = 0

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(PW, k, v)
        self._tmp.cleanup()

    def _set_response(self, resp):
        _install_fake_playwright(resp)
        # 重新绑定被测模块引用的 sync_playwright（from-import 已固化旧引用）
        PW.sync_playwright = sys.modules["playwright.sync_api"].sync_playwright


class TestHelpAndArgvDeath(_PwHarness):
    def test_help_exits_zero_without_launch(self):
        """验收：--help 退出码 0、0 次 playwright 启动。"""
        with self.assertRaises(SystemExit) as ctx:
            PW.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(LAUNCH_COUNT["n"], 0,
                         "--help 必须在解析层退出，不得触达浏览器/网络")

    def test_bare_positional_date_is_dead(self):
        """旧路径死亡：裸位置参数（旧 sys.argv[1] 用法）→ argparse 拒绝（码 2）。"""
        with self.assertRaises(SystemExit) as ctx:
            PW.main(["2026-09-04"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(LAUNCH_COUNT["n"], 0)

    def test_unknown_flag_rejected(self):
        """事故同型防线：任何未登记参数（如 --force-full）不得被当日期吞下。"""
        with self.assertRaises(SystemExit) as ctx:
            PW.main(["--force-full"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(LAUNCH_COUNT["n"], 0)


class TestExplicitDatePath(_PwHarness):
    def test_date_flag_drives_target_and_cache(self):
        """显式 --date 路径与现版一致：TARGET 进文件名与 fetched_at，缓存照常写。"""
        self._set_response(_ok_klines_response())
        PW.main(["--date", "2026-09-04"])
        out = self.tmp / "output" / "pull_sector_klines_pw_20260904.md"
        self.assertTrue(out.exists(), "报告文件名必须由 --date 驱动")
        import json
        rec = json.loads((PW.CACHE / "BK0001.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["fetched_at"], "2026-09-04")
        self.assertEqual(rec["last"], "2026-09-04")
        self.assertTrue(rec["covers_oos"])
        self.assertEqual(rec["bars"], 2)
        self.assertEqual(LAUNCH_COUNT["n"], 1, "正常轮只 launch 1 次")
        self.assertIn("ok", out.read_text(encoding="utf-8"))


class TestRestartCap(_PwHarness):
    def test_consecutive_fail_aborts_after_max_restarts(self):
        """验收：连败重启达 2 次后整轮中止且报告记 aborted=True。

        时序：5 连败 → 重启1 → 5 连败 → 重启2 → 5 连败 → 上限 → 中止。
        launch 总数 = 1(初始) + 2(重启) = 3；旧版无上限会一直重启到撞完全部码。
        """
        self._set_response({"error": "stub: throttled"})
        PW.main([])
        self.assertEqual(LAUNCH_COUNT["n"], 1 + PW.MAX_RESTARTS,
                         "重启用尽即停：launch 不得超过 1+MAX_RESTARTS")
        import re as _re
        from datetime import date as _date
        target = (_date.today() if _date.today().weekday() < 5
                  else PW._last_weekday()).isoformat()
        out = self.tmp / "output" / ("pull_sector_klines_pw_" + target.replace("-", "") + ".md")
        rep = out.read_text(encoding="utf-8")
        self.assertIn("aborted=True", rep)
        # 中止时未处理完 20 码：FAIL 行数 < 20（第 16 次迭代开头即 break）
        fail_rows = _re.findall(r"FAIL", rep)
        self.assertLess(len(fail_rows), 20, "整轮中止后不得继续逐码撞墙")
        self.assertGreaterEqual(len(fail_rows), 15)

    def test_mixed_success_never_restarts(self):
        """对照：全成功路径 0 次重启（封顶逻辑不误伤正常轮）。"""
        self._set_response(_ok_klines_response())
        PW.main([])
        self.assertEqual(LAUNCH_COUNT["n"], 1)
        from datetime import date as _date
        target = (_date.today() if _date.today().weekday() < 5
                  else PW._last_weekday()).isoformat()
        out = self.tmp / "output" / ("pull_sector_klines_pw_" + target.replace("-", "") + ".md")
        self.assertIn("aborted=False", out.read_text(encoding="utf-8"))


class TestTodoSkipUnaffected(_PwHarness):
    def test_cache_fresh_skips_without_launch(self):
        """既有语义保持：全部码当日已拉 → todo 空，0 launch、写空汇总报告。"""
        import json
        from datetime import date as _date
        target = (_date.today() if _date.today().weekday() < 5
                  else PW._last_weekday()).isoformat()
        for bk in self.CODES:
            (PW.CACHE / (bk + ".json")).write_text(
                json.dumps({"last": target}), encoding="utf-8")
        self._set_response({"error": "must not be called"})
        PW.main([])
        self.assertEqual(LAUNCH_COUNT["n"], 0)
        out = self.tmp / "output" / ("pull_sector_klines_pw_" + target.replace("-", "") + ".md")
        self.assertIn("ok=0 skip=20", out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
