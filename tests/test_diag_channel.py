"""diag_20260908 离线测例（2026-10-04 拆雷，D8-05 同型）。

背景：10-02 01:35 事故——diag 无 argparse，`--help` 被当 BK 码拼成
secid=90.--help 真实发东财请求（「把检查当运行」第二例）。argparse 化后钉死：

1. `--help` 退出码 0、0 请求 0 浏览器（假 playwright 被调即失败）。
2. 位置参数语义不变：diag BK0457 → BK='BK0457'；无参数 → 缺省 'BK0428'。
3. 未知旗标（--date 等）→ argparse 退出码 2 拒绝。
4. 非法 BK 码（90.--help / BK42 / BKABCD / bk0428）→ 退出码 2，先于任何网络路径。
5. 源码断言：import argparse 在场、旧 sys.argv[1] 直读死亡。

零网络纪律：被测模块 import 时注入**假 playwright**（venv 故意不装），
假 sync_playwright 一旦被调用即抛 AssertionError；解析层退出绝不到 main()，
0 次真实浏览器、0 次网络请求、不碰 data/ 与 output/。
"""
import importlib.util
import io
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
DIAG_PATH = BASE_DIR / 'experiments' / 'channel_diag' / 'diag_20260908.py'


def _load_diag(argv):
    """注入假 playwright 后，以给定 argv exec diag 模块。

    模块级即执行参数解析（与旧版 sys.argv 直读一致），故 argv 在 exec 前打补丁。
    返回 (module, SystemExit.code or None, stdout, stderr)。
    """
    pw = types.ModuleType('playwright')
    api = types.ModuleType('playwright.sync_api')

    def _forbidden(*_a, **_k):
        raise AssertionError('sync_playwright 被调用——解析层不应触网/启浏览器')

    api.sync_playwright = _forbidden
    pw.sync_api = api
    saved = {k: sys.modules.get(k) for k in ('playwright', 'playwright.sync_api')}
    sys.modules['playwright'] = pw
    sys.modules['playwright.sync_api'] = api
    try:
        spec = importlib.util.spec_from_file_location(
            'diag_20260908_under_test', DIAG_PATH)
        mod = importlib.util.module_from_spec(spec)
        out, err = io.StringIO(), io.StringIO()
        code = None
        with mock.patch.object(sys, 'argv', ['diag_20260908.py'] + list(argv)):
            with redirect_stdout(out), redirect_stderr(err):
                try:
                    spec.loader.exec_module(mod)
                except SystemExit as e:
                    code = e.code
        return mod, code, out.getvalue(), err.getvalue()
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


class TestDiagArgparseSafety(unittest.TestCase):
    def test_help_exits_zero_without_requests(self):
        """--help：退出码 0 + 用法文本；假 playwright 未被触碰（触碰即 AssertionError）。"""
        _mod, code, out, _err = _load_diag(['--help'])
        self.assertEqual(code, 0)
        self.assertIn('usage', out)
        self.assertIn('BK0428', out)

    def test_no_arg_defaults_bk0428(self):
        """无参数：缺省 BK0428，行为与旧版 sys.argv 直读一致。"""
        mod, code, _out, _err = _load_diag([])
        self.assertIsNone(code)
        self.assertEqual(mod.BK, 'BK0428')
        self.assertIn('secid=90.BK0428', mod.API)
        self.assertIn('90.BK0428.html', mod.QUOTE)

    def test_positional_bk_unchanged(self):
        """diag BK0457：位置参数语义不变（cron 第④步用法）。"""
        mod, code, _out, _err = _load_diag(['BK0457'])
        self.assertIsNone(code)
        self.assertEqual(mod.BK, 'BK0457')
        self.assertIn('secid=90.BK0457', mod.API)

    def test_unknown_flag_rejected_exit2(self):
        """未知旗标：argparse 退出码 2（--date 是事故同型参数形态）。"""
        for flag in ('--date', '--helpx'):
            with self.subTest(flag=flag):
                _mod, code, _out, err = _load_diag([flag, '20260908'])
                self.assertEqual(code, 2)
                self.assertIn('unrecognized', err)

    def test_malformed_bk_rejected_exit2(self):
        """非法 BK 码：退出码 2；90.--help 即 10-02 01:35 事故字符串。"""
        for bad in ('90.--help', 'BK42', 'BKABCD', 'bk0428', 'BK0428X'):
            with self.subTest(bad=bad):
                _mod, code, _out, err = _load_diag([bad])
                self.assertEqual(code, 2)
                self.assertIn('非法 BK 码', err)

    def test_source_assertions(self):
        """源码断言：argparse 在场，旧 sys.argv[1] 直读死亡，BK 码守卫在场。"""
        src = DIAG_PATH.read_text(encoding='utf-8')
        self.assertIn('import argparse', src)
        self.assertIn('re.fullmatch', src)
        self.assertNotIn('sys.argv[1]', src)
        self.assertLess(src.index('def _parse_args'), src.index('BK = _parse_args'))


if __name__ == '__main__':
    unittest.main()
