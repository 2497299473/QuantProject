#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板块 K 线 21:30 晚间补拉（2026-09-08 新增，Windows 计划任务直跑）。

背景：2026-09-05 / 09-08 两次东财 push2his IP/接口级频控都在 16:00 撞车，
64/65 缺口要拖到下一交易日才补。本脚本在 21:30（频控窗口常已松动）再做一次
幂等补拉，让 22:30 Shadow 任务当晚就能读到当日完整板块 K 线，缺口不用过夜。

与主脚本 pull_sector_klines.py 的关系：
  - 共用同一缓存目录 data/sector_klines/ 与 skip 逻辑（last==今天 → skip）；
  - 输出独立审计文件 output/pull_sector_klines_evening_YYYYMMDD.md
    （不覆盖 16:00 的 pull_sector_klines_YYYYMMDD.md，两个时点各自留痕）；
  - 另追加一行精简报告到 output/zcode_runs/今日.md 小节 [21:30 板块K线补拉]，
    遵守 AGENTS.md「定时任务必须落 zcode_runs」约定。

纪律（与主脚本一致，铁律 ①）：
  - 非交易日直接退出 0（周末 + 法定休市，读 data/holidays.json 自判，不 import run）；
  - 单轮不重跑（频控防护：绝不循环重试东财）；FAIL 跳过不中断；
  - 只读 holidays.json + 板块映射 md，写 data/sector_klines/ 与 output/。

用法：
  .\\.venv\\Scripts\\python.exe -X utf8 pull_sector_klines_evening.py
"""
import json
import re
import time
from datetime import date
from pathlib import Path

from core import netutil
from pull_sector_klines import load_codes, OOS_START, SLEEP

BASE = Path(__file__).resolve().parent
CACHE = BASE / 'data' / 'sector_klines'
HOLIDAYS_JSON = BASE / 'data' / 'holidays.json'
OUT_MD = BASE / 'output' / f'pull_sector_klines_evening_{date.today().strftime("%Y%m%d")}.md'
ZRUNS_MD = BASE / 'output' / 'zcode_runs' / (date.today().isoformat() + '.md')


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


def main():
    if not _is_trading_day():
        print('[evening] 非交易日（周末/休市），跳过')
        return
    CACHE.mkdir(parents=True, exist_ok=True)
    TODAY = date.today().isoformat()
    codes = load_codes()
    print('[evening] codes:', len(codes))
    lines = ['# 板块 K 线晚间补拉 ' + TODAY, '',
             '| BK | 名称 | 类型 | bars | 首根 | 末根 | 状态 |',
             '|---|---|---|---:|---|---|---|']
    ok = skip = fail = 0
    fail_codes = []
    for bk, info in sorted(codes.items()):
        fp = CACHE / (bk + '.json')
        if fp.exists():
            try:
                old = json.loads(fp.read_text(encoding='utf-8'))
                if old.get('last') == TODAY:
                    skip += 1
                    lines.append('| %s | %s | %s | %d | %s | %s | skip(当日已拉) |'
                                 % (bk, info['name'], info['type'], old['bars'],
                                    old['first'], old['last']))
                    continue
            except Exception:
                pass
        url = ('http://push2his.eastmoney.com/api/qt/stock/kline/get'
               '?secid=90.' + bk + '&klt=101&fqt=1&beg=20150101&end=20500101'
               '&fields1=f1&fields2=f51,f52,f53,f54,f55,f56')
        try:
            j = netutil.http_get_json(url, timeout=15, retries=1)  # 晚间补拉不重试，防频控
            d = j.get('data') or {}
            kl = d.get('klines') or []
            arr = [s.split(',') for s in kl]
            first = arr[0][0] if arr else ''
            last = arr[-1][0] if arr else ''
            rec = {
                'code': bk, 'name': info['name'], 'type': info['type'],
                'secid': '90.' + bk, 'klt': 101, 'fqt': 1,
                'source': 'push2his.eastmoney.com', 'fetched_at': TODAY,
                'bars': len(arr), 'first': first, 'last': last,
                'fields': ['date', 'open', 'close', 'high', 'low', 'volume'],
                'covers_oos': bool(first and first <= OOS_START),
                'klines': arr,
            }
            fp.write_text(json.dumps(rec, ensure_ascii=False), encoding='utf-8')
            ok += 1
            lines.append('| %s | %s | %s | %d | %s | %s | ok |'
                         % (bk, info['name'], info['type'], len(arr), first, last))
        except Exception as e:
            fail += 1
            fail_codes.append(bk)
            lines.append('| %s | %s | %s | - | - | - | FAIL %s:%s |'
                         % (bk, info['name'], info['type'], type(e).__name__, str(e)[:40]))
        time.sleep(SLEEP)

    lines += ['', '== 汇总：ok=%d skip=%d fail=%d / %d ==' % (ok, skip, fail, len(codes)),
              'FAIL 码: ' + (','.join(fail_codes) if fail_codes else '无'),
              '== done ==']
    OUT_MD.write_text('\n'.join(lines), encoding='utf-8')
    print('written', OUT_MD)

    # zcode_runs 精简报告（AGENTS.md 约定）
    ZRUNS_MD.parent.mkdir(parents=True, exist_ok=True)
    now = time.strftime('%Y-%m-%d %H:%M:%S %z')
    concl = ('全部补齐' if fail == 0 else
             ('部分补齐，剩余 %d 个待下一交易日' % fail))
    block = ('## [21:30 板块K线补拉]（晚间计划任务，脚本直跑非 agent）\n'
             '- **执行时间**：%s（+08:00），Windows 计划任务 QuantFund_KlineEvening\n'
             '- **结果**：ok=%d skip=%d fail=%d / %d\n'
             '- **FAIL 码**：%s\n'
             '- **一句话结论**：%s。\n' % (
                 now, ok, skip, fail, len(codes),
                 ','.join(fail_codes) if fail_codes else '无', concl))
    with ZRUNS_MD.open('a', encoding='utf-8') as f:
        f.write('\n' + block)
    print('appended zcode_runs report')


if __name__ == '__main__':
    main()
