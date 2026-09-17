#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试分层执行器（V4-A，2026-09-17）。

    .\\.venv\\Scripts\\python.exe run_tests.py --layer fast    # 日常开发
    .\\.venv\\Scripts\\python.exe run_tests.py --layer slow    # 提交前 / 夜间
    .\\.venv\\Scripts\\python.exe run_tests.py --all
    .\\.venv\\Scripts\\python.exe run_tests.py --list

为什么不用 ``pytest -m fast``：当前项目环境（含 ``.venv`` / ``.venv-lab``）
**未安装 pytest**，而 ``requirements.txt`` 只锁 numpy/scipy/scikit-learn 等运行时
依赖，不为测试工具引入新依赖。因此分层用 ``unittest`` 原生实现；``tests/layers.py``
是唯一分层事实来源，装了 pytest 时 ``tests/conftest.py`` 会把同一份清单映射成
marker，两种入口不会漂移。
"""
from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from tests.layers import FAST, SLOW, LAYERS, modules_for   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="分层跑测（unittest 原生）")
    ap.add_argument("--layer", choices=[FAST, SLOW],
                    help="只跑某一层")
    ap.add_argument("--all", action="store_true", help="跑全部测试文件")
    ap.add_argument("--list", action="store_true", help="只打印分层清单，不执行")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.list:
        for lyr in (FAST, SLOW):
            names = sorted(n for n, l in LAYERS.items() if l == lyr)
            print(f"\n[{lyr}] {len(names)} 个文件")
            for n in names:
                print(f"  - {n}")
        print(f"\n合计 {len(LAYERS)} 个文件")
        return 0

    if args.layer and not args.all:
        names = modules_for(args.layer)
    else:
        names = [f"tests.{n[:-3]}" for n in sorted(LAYERS)]

    print(f"[run_tests] {len(names)} 个测试文件"
          + (f"（layer={args.layer}）" if args.layer and not args.all else "（全部）"))
    suite = unittest.TestLoader().loadTestsFromNames(names)
    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
