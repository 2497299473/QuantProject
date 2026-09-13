#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板块 K 线批量拉取缓存（2026-09-02）。

B 组纪律：限速 >=2s；FAIL 跳过不中断；当日不重复拉（last==今天则 skip）。
来源清单：output/sector_name_mapping_20260902.md（精确+模糊匹配的全部 BK）。
缓存：data/sector_klines/{BK}.json，klines 为 [date,open,close,high,low,volume] 数组。
产物：output/pull_sector_klines_YYYYMMDD.md（按当天动态生成，一天一份，供数据源审计）
"""
import json
import re
import time
from datetime import date
from pathlib import Path

from core import netutil

BASE = Path(__file__).resolve().parent
OUT_DIR = BASE / 'data' / 'sector_klines'
OUT_MD = BASE / 'output' / f'pull_sector_klines_{date.today().strftime("%Y%m%d")}.md'
MAP_MD = BASE / 'output' / 'sector_name_mapping_20260902.md'
TODAY = date.today().isoformat()
OOS_START = '2020-01-01'
SLEEP = 2.0


def load_codes():
    text = MAP_MD.read_text(encoding='utf-8')
    rows = re.findall(
        r'\|\s*([^|]+?)\s*\|\s*(BK\d{4})\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|',
        text)
    seen = {}
    for user, bk, name, typ in rows:
        if bk not in seen:
            seen[bk] = {'user': user, 'name': name, 'type': typ}
    return seen


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    codes = load_codes()
    print('codes:', len(codes))
    lines = ['# 板块 K 线拉取缓存 ' + TODAY, '',
             '| BK | 名称 | 类型 | bars | 首根 | 末根 | OOS | 状态 |',
             '|---|---|---|---:|---|---|---|---|']
    ok = skip = fail = 0
    for i, (bk, info) in enumerate(sorted(codes.items()), 1):
        fp = OUT_DIR / (bk + '.json')
        if fp.exists():
            try:
                old = json.loads(fp.read_text(encoding='utf-8'))
                if old.get('last') == TODAY:
                    lines.append('| %s | %s | %s | %d | %s | %s | %s | skip(当日已拉) |'
                                 % (bk, info['name'], info['type'], old['bars'],
                                    old['first'], old['last'],
                                    '是' if old['first'] <= OOS_START else '否'))
                    skip += 1
                    print('[skip]', bk, info['name'])
                    continue
            except Exception:
                pass
        # 2026-09-02 晚：东财服务端启用 TLS 指纹过滤，HTTPS 全节点（v4/v6）握手成功
        # 但请求即被掐断（Python/curl 同样被拦）；HTTP 明文通道实测 200 全通，
        # 数据与 14:27 本地缓存逐项一致，故降级 HTTP。
        url = ('http://push2his.eastmoney.com/api/qt/stock/kline/get'
               '?secid=90.' + bk + '&klt=101&fqt=1&beg=20150101&end=20500101'
               '&fields1=f1&fields2=f51,f52,f53,f54,f55,f56')
        try:
            j = netutil.http_get_json(url, timeout=15, retries=2)
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
            lines.append('| %s | %s | %s | %d | %s | %s | %s | ok |'
                         % (bk, info['name'], info['type'], len(arr), first, last,
                            '是' if rec['covers_oos'] else '否'))
            ok += 1
            print('[ok %d/%d]' % (i, len(codes)), bk, info['name'],
                  len(arr), first, last)
        except Exception as e:
            lines.append('| %s | %s | %s | - | - | - | - | FAIL %s:%s |'
                         % (bk, info['name'], info['type'], type(e).__name__,
                            str(e)[:60]))
            fail += 1
            print('[FAIL]', bk, info['name'], type(e).__name__, str(e)[:80])
        time.sleep(SLEEP)
    lines += ['', '== 汇总：ok=%d skip=%d fail=%d / %d ==' % (ok, skip, fail, len(codes)),
              '== done ==']
    OUT_MD.write_text('\n'.join(lines), encoding='utf-8')
    print('written', OUT_MD)


if __name__ == '__main__':
    main()
