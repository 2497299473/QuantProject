"""pull_sector_klines / evening 离线测例（面 8 D8-01/D8-02/D8-09，2026-10-03）。

零网络纪律（铁律 8 路 c）——这两个脚本的调用链会真发东财请求，因此：
- **绝不运行脚本本体**（含 `--help`：本族 --help 会变业务参数真实发请求）；
- main() 一律以 `fetch_and_store` 打桩替换（真实 fetch_one → netutil 路径不可达）；
- fetch_and_store 的传输回调以 `fetch=` 注入（构造 ThrottleSuspected 等异常）；
- 所有落盘路径（OUT_MD / ZRUNS_MD / 缓存 fp / HOLIDAYS_JSON）重定向到 tempdir；
- netutil 只用到纯计数器 API（wire_attempts / reset / _wire_bump），不发包。

覆盖：
1. D8-01 is_trading_day：周末 / 法定休市 / 正常工作日 / 表缺失 fail-open；
   主脚本 main() 休市日 → 0 次 fetch_and_store 调用 + 报告「休市跳过」+ 请求数=0。
2. D8-01 单一来源：evening 本地 `_is_trading_day` 定义已删除（旧路径死亡），改为 import。
3. D8-02 ThrottleSuspected → fetch_and_store detail 标 throttled=True；
   main()/evening main() 遇 throttled 整轮中止（后续码不再调用）+ 报告 aborted=THROTTLE。
4. D8-09 报告汇总含 wire_attempts=（真实发包口径），且算术为 delta（终点-起点）。
"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import pull_sector_klines as P  # noqa: E402
import pull_sector_klines_evening as E  # noqa: E402
from core import netutil  # noqa: E402

_HOLIDAYS = {
    "years": {
        "2026": {
            "国庆": ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06",
                     "2026-10-07"],
        }
    }
}


def _ok_detail(bk, today, requests=1):
    return {'status': 'ok', 'mode': 'incremental', 'beg': '20261008', 'bars': 10,
            'first': '2015-01-05', 'last': today, 'covers_oos': True,
            'appended': 1, 'note': '', 'error': '', 'requests': requests}


class _MainHarness(unittest.TestCase):
    """公共夹具：tempdir 落盘 + fetch_and_store 打桩 + wire 计数复位。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        netutil.reset_wire_attempts()
        self._orig = {k: getattr(P, k) for k in
                      ('OUT_MD', 'OUT_DIR', 'HOLIDAYS_JSON', 'fetch_and_store', 'SLEEP')}
        P.OUT_MD = self.tmp / 'pull_main.md'
        P.OUT_DIR = self.tmp / 'sector_klines'
        P.HOLIDAYS_JSON = self.tmp / 'holidays.json'
        P.HOLIDAYS_JSON.write_text(
            __import__('json').dumps(_HOLIDAYS, ensure_ascii=False), encoding='utf-8')
        P.SLEEP = 0
        self.calls = []

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(P, k, v)
        netutil.reset_wire_attempts()
        self._tmp.cleanup()

    def _stub_store(self, detail_fn):
        def fake(bk, info, fp, **kw):
            self.calls.append((bk, kw.get('today')))
            return detail_fn(bk, kw.get('today'))
        P.fetch_and_store = fake

    def _report(self):
        return P.OUT_MD.read_text(encoding='utf-8')


class TestIsTradingDay(unittest.TestCase):
    """D8-01：休市判定纯函数（tempdir holidays.json，不碰真实 data/）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._orig = P.HOLIDAYS_JSON
        P.HOLIDAYS_JSON = self.tmp / 'holidays.json'
        P.HOLIDAYS_JSON.write_text(
            __import__('json').dumps(_HOLIDAYS, ensure_ascii=False), encoding='utf-8')

    def tearDown(self):
        P.HOLIDAYS_JSON = self._orig
        self._tmp.cleanup()

    def test_weekend_is_not_trading(self):
        self.assertFalse(P.is_trading_day(date(2026, 10, 3)))    # 周六
        self.assertFalse(P.is_trading_day(date(2026, 10, 4)))    # 周日

    def test_legal_holiday_is_not_trading(self):
        for d in ("2026-10-01", "2026-10-05", "2026-10-07"):
            y, m, dd = (int(x) for x in d.split('-'))
            self.assertFalse(P.is_trading_day(date(y, m, dd)), d)

    def test_normal_weekday_is_trading(self):
        self.assertTrue(P.is_trading_day(date(2026, 10, 8)))     # 周四，非休市
        self.assertTrue(P.is_trading_day(date(2026, 9, 30)))     # 周三，节前最后交易日

    def test_missing_table_fails_open_to_workday(self):
        """表读不到 → 按工作日处理（既有口径：宁可多拉，skip 逻辑挡重复）。"""
        P.HOLIDAYS_JSON = self.tmp / 'nonexistent.json'
        self.assertTrue(P.is_trading_day(date(2026, 10, 8)))

    def test_default_arg_uses_today(self):
        self.assertIsInstance(P.is_trading_day(), bool)


class TestMainHolidayGuard(_MainHarness):
    """D8-01 验收：休市日 main() → 0 请求 + 报告「休市跳过」。"""

    def test_holiday_skips_with_zero_fetch(self):
        self._stub_store(lambda bk, today: _ok_detail(bk, today))
        P.main(['--date', '2026-10-05'])            # 国庆休市（周一）
        self.assertEqual(self.calls, [], "休市日必须 0 次 fetch_and_store 调用")
        rep = self._report()
        self.assertIn('休市跳过', rep)
        self.assertIn('东财请求数=0', rep)
        self.assertIn('scope=prod', rep)             # 审计可区分「没拉」与「不需要拉」
        self.assertIn('codes=', rep)

    def test_trading_day_proceeds(self):
        self._stub_store(lambda bk, today: _ok_detail(bk, today))
        P.main(['--date', '2026-10-08'])            # 周四，正常交易日
        self.assertEqual(len(self.calls), 4, "prod 4 码应逐个调用")
        self.assertNotIn('休市跳过', self._report())

    def test_weekend_skips(self):
        self._stub_store(lambda bk, today: _ok_detail(bk, today))
        P.main(['--date', '2026-10-03'])            # 周六
        self.assertEqual(self.calls, [])
        self.assertIn('休市跳过', self._report())


class TestThrottleAbort(_MainHarness):
    """D8-02 验收：熔断 → 整轮中止（不再逐码撞被掐接口）+ 报告 aborted=THROTTLE。"""

    def test_throttled_first_code_aborts_round(self):
        def detail(bk, today):
            if bk == 'BK0428':                      # sorted 后首个 prod 码
                return {'status': 'fail', 'mode': 'full', 'beg': '20150101', 'bars': 0,
                        'first': '', 'last': '', 'covers_oos': False, 'appended': 0,
                        'note': '', 'error': 'ThrottleSuspected:连续 8 次',
                        'throttled': True, 'requests': 1}
            return _ok_detail(bk, today)
        self._stub_store(detail)
        P.main(['--date', '2026-10-08'])
        self.assertEqual(len(self.calls), 1, "熔断后必须整轮中止（不得继续下一码）")
        rep = self._report()
        self.assertIn('aborted=THROTTLE', rep)
        self.assertIn('FAIL', rep)

    def test_plain_fail_continues_next_code(self):
        """对照：普通 FAIL（非熔断）保持旧语义——跳过不中断。"""
        def detail(bk, today):
            if bk == 'BK0428':
                return {'status': 'fail', 'mode': 'full', 'beg': '20150101', 'bars': 0,
                        'first': '', 'last': '', 'covers_oos': False, 'appended': 0,
                        'note': '', 'error': 'RuntimeError:empty_payload',
                        'requests': 1}
            return _ok_detail(bk, today)
        self._stub_store(detail)
        P.main(['--date', '2026-10-08'])
        self.assertEqual(len(self.calls), 4, "普通 FAIL 不得中止整轮")
        self.assertNotIn('aborted=THROTTLE', self._report())

    def test_fetch_and_store_marks_throttled_flag(self):
        """传输回调抛 ThrottleSuspected → detail['throttled']=True（离线注入，不碰真实 netutil 路径）。"""
        fp = self.tmp / 'sector_klines' / 'BK0457.json'

        def boom(bk, beg):
            raise netutil.ThrottleSuspected("连续 8 次连接层失败")

        d = P.fetch_and_store('BK0457', {'name': '电网设备', 'type': '行业'}, fp,
                              today='2026-10-08', fetch=boom)
        self.assertEqual(d['status'], 'fail')
        self.assertTrue(d.get('throttled'), "熔断异常必须被标记，供整轮中止判别")
        self.assertIn('ThrottleSuspected', d['error'])
        self.assertFalse(fp.exists(), "失败不得写半成品缓存")

    def test_fetch_and_store_plain_error_not_throttled(self):
        fp = self.tmp / 'sector_klines' / 'BK0457.json'

        def boom(bk, beg):
            raise RuntimeError('empty_payload(beg=20261008)')

        d = P.fetch_and_store('BK0457', {'name': 'x', 'type': '行业'}, fp,
                              today='2026-10-08', fetch=boom)
        self.assertEqual(d['status'], 'fail')
        self.assertFalse(d.get('throttled'), "普通失败不得误标熔断（否则整轮误中止）")


class TestWireAttemptsInReport(_MainHarness):
    """D8-09 验收：报告汇总含 wire_attempts=，且为真实发包 delta（非逻辑请求数）。"""

    def test_report_contains_wire_attempts_delta(self):
        def detail(bk, today):
            for _ in range(3):                      # 模拟每码 3 次真实发包（重试/failover）
                netutil._wire_bump()
            return _ok_detail(bk, today, requests=1)
        self._stub_store(detail)
        P.main(['--date', '2026-10-08'])
        rep = self._report()
        self.assertIn('wire_attempts=', rep)
        self.assertIn('wire_attempts=12', rep)       # 4 码 × 3 次真实发包
        self.assertIn('东财请求数=4', rep)            # 逻辑口径仍是 4（两口径并存不混淆）

    def test_wire_delta_not_absolute(self):
        """起点非零时报告的是 delta（进程级计数器不被误当本轮绝对值）。"""
        for _ in range(100):
            netutil._wire_bump()

        def detail(bk, today):
            netutil._wire_bump()
            return _ok_detail(bk, today)
        self._stub_store(detail)
        P.main(['--date', '2026-10-08'])
        self.assertIn('wire_attempts=4', self._report())
        self.assertNotIn('wire_attempts=104', self._report())


class TestEveningSingleSource(unittest.TestCase):
    """D8-01 验收：evening 本地 _is_trading_day 已删除、改为 import（旧路径死亡）。"""

    def test_local_definition_removed(self):
        src = (BASE_DIR / 'pull_sector_klines_evening.py').read_text(encoding='utf-8')
        self.assertNotIn('def _is_trading_day', src,
                         "evening 不得再持有本地实现（单一来源 = pull_sector_klines）")
        self.assertFalse(hasattr(E, '_is_trading_day'),
                         "旧属性路径必须死亡（防旧调用点静默复活）")

    def test_imports_shared_implementation(self):
        self.assertIs(E.is_trading_day, P.is_trading_day,
                      "evening 必须复用主脚本同一判定（口径不得分叉）")

    def test_main_uses_shared_guard(self):
        src = (BASE_DIR / 'pull_sector_klines_evening.py').read_text(encoding='utf-8')
        self.assertIn('if not is_trading_day():', src)


class TestEveningThrottleAbort(unittest.TestCase):
    """D8-02：evening 同样整轮中止（单轮不重跑纪律叠加）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        netutil.reset_wire_attempts()
        self._orig = {k: getattr(E, k) for k in
                      ('OUT_MD', 'ZRUNS_MD', 'CACHE', 'fetch_and_store', 'SLEEP',
                       '_task_snapshot', 'is_trading_day')}
        E.OUT_MD = self.tmp / 'evening.md'
        E.ZRUNS_MD = self.tmp / 'daily_runs.md'
        E.CACHE = self.tmp / 'sector_klines'
        E.SLEEP = 0
        E._task_snapshot = lambda: 'unavailable'
        E.is_trading_day = lambda *a, **k: True      # 绕开真实日历（周末跑测试）
        self.calls = []

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(E, k, v)
        netutil.reset_wire_attempts()
        self._tmp.cleanup()

    def _stub_store(self, detail_fn):
        def fake(bk, info, fp, **kw):
            self.calls.append(bk)
            return detail_fn(bk, kw.get('today'))
        E.fetch_and_store = fake

    def test_throttled_aborts_round_and_marks_report(self):
        def detail(bk, today):
            return {'status': 'fail', 'mode': 'full', 'beg': '20150101', 'bars': 0,
                    'first': '', 'last': '', 'covers_oos': False, 'appended': 0,
                    'note': '', 'error': 'ThrottleSuspected:x', 'throttled': True,
                    'requests': 1}
        self._stub_store(detail)
        E.main(['--trigger', 'manual'])
        self.assertEqual(len(self.calls), 1, "熔断后整轮中止")
        self.assertIn('aborted=THROTTLE', E.OUT_MD.read_text(encoding='utf-8'))
        zr = E.ZRUNS_MD.read_text(encoding='utf-8')
        self.assertIn('熔断中止', zr)                 # daily_runs 留痕可判读
        self.assertIn('wire_attempts=', zr)

    def test_normal_path_unaffected(self):
        self._stub_store(lambda bk, today: _ok_detail(bk, today))
        E.main(['--trigger', 'scheduler'])
        self.assertEqual(len(self.calls), 4)
        rep = E.OUT_MD.read_text(encoding='utf-8')
        self.assertNotIn('aborted=', rep)
        self.assertIn('wire_attempts=', rep)
        self.assertIn('- **执行方式**=scheduler', E.ZRUNS_MD.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
