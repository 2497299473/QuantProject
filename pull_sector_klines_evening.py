#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板块 K 线 21:30 晚间补拉（2026-09-08 新增；同夜 V3 P0-1/P0-3 改造）。

背景：2026-09-05 / 09-08 两次东财 push2his 频控都在 16:00 撞车，缺口要拖到下一
交易日才补。本脚本在 21:30 再做一次幂等补拉，让 22:30 Shadow 当晚读到完整板块
K 线，缺口不过夜。

V3 改造（output/V3_data_layer_plan_20260908.md 第三节）
- P0-1：默认 --scope prod（4 码），不再每天为 61 个零消费者码烧东财请求预算；
  审计 md 首行写明 scope= 与 codes=。
- P0-2：拉取与增量合并逻辑一律复用 pull_sector_klines.fetch_and_store（同一套
  断言，不留第二份实现）。
- P0-3（诚实口径）：旧版第 125 行把来源**硬编码**成「Windows 计划任务
  QuantFund_KlineEvening」，任何手动运行都会自称计划任务（09-08 21:30 与 17:53
  两条同名记录即此缺陷的实证）。现改为 --trigger {scheduler,manual}，**默认
  manual**，只有 install_scheduled_tasks.ps1 显式传 scheduler；并附
  LastTaskResult / NextRunTime 快照让日志可自证。节标题带时间戳，避免同名小节
  使日志无法按时间寻址。

纪律（与主脚本一致）：
  - 非交易日直接退出（周末 + 法定休市，读 data/holidays.json 自判，不 import run）；
  - 单轮不重跑（频控防护：绝不循环重试东财）；FAIL 跳过不中断、不写半成品缓存；
  - 当日已拉（last==今天）→ skip，不重拉。

用法：
  .\\.venv\\Scripts\\python.exe -X utf8 pull_sector_klines_evening.py --trigger scheduler
  .\\.venv\\Scripts\\python.exe -X utf8 pull_sector_klines_evening.py            # 手动
"""
import argparse
import json
import subprocess
import time
from datetime import date
from pathlib import Path

from pull_sector_klines import (OOS_START, SLEEP, fetch_and_store,
                                is_weekly_full_day, load_codes)

BASE = Path(__file__).resolve().parent
CACHE = BASE / 'data' / 'sector_klines'
HOLIDAYS_JSON = BASE / 'data' / 'holidays.json'
TODAY = date.today().isoformat()
OUT_MD = BASE / 'output' / f'pull_sector_klines_evening_{date.today().strftime("%Y%m%d")}.md'
ZRUNS_MD = BASE / 'output' / 'zcode_runs' / (date.today().isoformat() + '.md')
TASK_NAME = 'QuantFund_KlineEvening'


def _is_trading_day() -> bool:
    """周一~五 且 非法定休市日。自包含读 holidays.json（不 import run，避免副作用）。"""
    today = date.today()
    if today.weekday() >= 5:
        return False
    try:
        data = json.loads(HOLIDAYS_JSON.read_text(encoding='utf-8'))
    except Exception:
        # 读不到节假日表时按工作日处理（宁可多拉，skip 逻辑会挡住重复）
        return True
    days = set()
    for year in data.get('years', {}).values():
        for dates in year.values():
            days.update(dates)
    return today.isoformat() not in days


def _task_snapshot() -> str:
    """读本机计划任务状态，让日志能自证来源。失败只降级，不抛。"""
    cmd = ['powershell', '-NoProfile', '-Command',
           '$i=Get-ScheduledTaskInfo -TaskName %s -ErrorAction SilentlyContinue;'
           'if($i){"LastRunTime={0}|LastTaskResult={1}|NextRunTime={2}"'
           '-f $i.LastRunTime,$i.LastTaskResult,$i.NextRunTime}' % TASK_NAME]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25,
                           creationflags=0x08000000)  # CREATE_NO_WINDOW
        out = (r.stdout or '').strip()
        return out if out else 'unavailable'
    except Exception as e:
        return 'unavailable(%s)' % type(e).__name__


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--scope', choices=('prod', 'research', 'full'), default='prod',
                    help='默认 prod（生产最小集 4 码）；full 为回滚路径')
    ap.add_argument('--trigger', choices=('scheduler', 'manual'), default='manual',
                    help='执行方式。计划任务必须显式传 scheduler；默认 manual')
    ap.add_argument('--force-full', action='store_true')
    args = ap.parse_args(argv)

    if not _is_trading_day():
        print('[evening] 非交易日（周末/休市），跳过')
        return
    codes = load_codes(args.scope)
    weekly_full = is_weekly_full_day(TODAY, args.scope)
    print('[evening] trigger=%s scope=%s codes=%d' % (
        args.trigger, args.scope, len(codes)), flush=True)

    lines = ['# 板块 K 线晚间补拉 %s scope=%s codes=%d' % (TODAY, args.scope, len(codes)),
             'scope=%s codes=%s trigger=%s force_full=%s monday_full=%s' % (
                 args.scope, ','.join(sorted(codes)), args.trigger,
                 args.force_full, weekly_full),
             '',
             '| BK | 名称 | 类型 | mode | beg | bars | 首根 | 末根 | 状态 |',
             '|---|---|---|---|---|---:|---|---|---|']
    ok = skip = fail = fallback_full = requests_sent = 0
    fail_codes = []
    for bk, info in sorted(codes.items()):
        d = fetch_and_store(bk, info, CACHE / (bk + '.json'), today=TODAY,
                            force_full=args.force_full, weekly_full=weekly_full)
        requests_sent += d['requests']
        if d['note'].startswith('FALLBACK_FULL'):
            fallback_full += 1
        if d['status'] == 'skip':
            skip += 1
            lines.append('| %s | %s | %s | skip | - | %d | %s | %s | skip(当日已拉) |'
                         % (bk, info['name'], info['type'], d['bars'],
                            d['first'], d['last']))
        elif d['status'] == 'fail':
            fail += 1
            fail_codes.append(bk)
            lines.append('| %s | %s | %s | %s | %s | - | - | - | FAIL %s |'
                         % (bk, info['name'], info['type'], d['mode'], d['beg'],
                            d['error']))
        else:
            ok += 1
            lines.append('| %s | %s | %s | %s | %s | %d | %s | %s | ok%s |'
                         % (bk, info['name'], info['type'], d['mode'], d['beg'],
                            d['bars'], d['first'], d['last'],
                            (' ' + d['note']) if d['note'] else ''))
        time.sleep(SLEEP)

    lines += ['',
              '== 汇总：ok=%d skip=%d fail=%d fallback_full=%d / %d =='
              % (ok, skip, fail, fallback_full, len(codes)),
              '== 东财请求数=%d ==' % requests_sent,
              'FAIL 码: ' + (','.join(fail_codes) if fail_codes else '无'),
              '== done ==']
    OUT_MD.write_text('\n'.join(lines), encoding='utf-8')
    print('written', OUT_MD, flush=True)

    # 定时任务留痕（AGENTS.md 约定）。来源标注来自 --trigger，不再硬编码。
    ZRUNS_MD.parent.mkdir(parents=True, exist_ok=True)
    now = time.strftime('%Y-%m-%d %H:%M:%S %z')
    hhmm = time.strftime('%H:%M')
    concl = ('全部补齐' if fail == 0
             else ('部分补齐，剩余 %d 个待下一交易日' % fail))
    block = ('## [%s 板块K线补拉]（晚间脚本直跑非 agent）\n'
             '- **执行时间**：%s\n'
             '- **执行方式**=%s（来源由 --trigger 决定，默认 manual）\n'
             '- **任务快照**：%s\n'
             '- **scope**：%s codes=%d（请求数 %d）\n'
             '- **结果**：ok=%d skip=%d fail=%d fallback_full=%d / %d\n'
             '- **FAIL 码**：%s\n'
             '- **一句话结论**：%s。\n' % (
                 hhmm, now, args.trigger, _task_snapshot(), args.scope, len(codes),
                 requests_sent, ok, skip, fail, fallback_full, len(codes),
                 ','.join(fail_codes) if fail_codes else '无', concl))
    with ZRUNS_MD.open('a', encoding='utf-8') as f:
        f.write('\n' + block)
    print('appended zcode_runs report (trigger=%s)' % args.trigger, flush=True)


if __name__ == '__main__':
    main()
