# -*- coding: utf-8 -*-
"""Playwright 页面内 fetch 拉取东财板块 K 线（指纹拦截期兜底通道，v2 固化版）。

背景（2026-09-05 凌晨实测）：
- 东财对 push2his kline/get 做客户端指纹级拦截（TLS/JA3/h2 层），
  curl/curl_cffi 四指纹/编号子域名/ut token/cookie 重放全部连接层被掐；
- 真实 Chromium 页面内 fetch 曾验证可通（00:13），后因高频请求触发频控失效；
- 本脚本与原版 pull_sector_klines.py 输出格式完全兼容（同一缓存目录/schema）。

用法：
- python3 pull_sector_klines_pw.py           # TARGET=今天(工作日)或上一交易日
- python3 pull_sector_klines_pw.py 2026-09-04  # 显式指定目标日期（补历史缺口）

纪律：限速 >=2s；FAIL 跳过不中断；缓存 last==TARGET 则 skip；
连续 5 失败重启浏览器；每 10 码 reload 刷新连接池。
"""
import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

# 2026-09-08 迁移 Windows：改为相对自身文件定位项目根，跨盘/跨环境通用
# （旧写法写死 /home/summer/QuantV1 与 UNC，迁移后必然失效）
BASE = Path(__file__).resolve().parent
CACHE = BASE / 'data' / 'sector_klines'
MAP_MD = BASE / 'output' / 'sector_name_mapping_20260902.md'
OOS_START = '2020-01-01'
SLEEP = 2.0
FIELDS = ['date', 'open', 'close', 'high', 'low', 'volume']


def _last_weekday():
    d = date.today() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def load_codes():
    rows = re.findall(
        r'\|\s*([^|]+?)\s*\|\s*(BK\d{4})\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|',
        MAP_MD.read_text(encoding='utf-8'))
    seen = {}
    for _, bk, name, typ in rows:
        seen.setdefault(bk, {'name': name, 'type': typ})
    return seen


def main():
    TARGET = (date.today() if date.today().weekday() < 5 else _last_weekday()).isoformat()
    if len(sys.argv) > 1:
        TARGET = sys.argv[1]
    OUT_MD = BASE / 'output' / ('pull_sector_klines_pw_' + TARGET.replace('-', '') + '.md')

    codes = load_codes()
    todo = []
    for bk, info in sorted(codes.items()):
        fp = CACHE / (bk + '.json')
        need = True
        if fp.exists():
            try:
                old = json.loads(fp.read_text(encoding='utf-8'))
                need = old.get('last') != TARGET
            except Exception:
                need = True
        if need:
            todo.append((bk, info))
    print('target:', TARGET, '| todo:', len(todo), 'of', len(codes), flush=True)
    if not todo:
        OUT_MD.write_text(
            '# 板块 K 线补拉（Playwright 页面内 fetch 通道）目标 ' + TARGET + '\n\n'
            '== 汇总：ok=0 skip=%d fail=0 / %d ==\n== done ==\n' % (len(codes), len(codes)),
            encoding='utf-8')
        print('nothing to do, written', OUT_MD, flush=True)
        return

    lines = ['# 板块 K 线补拉（Playwright 页面内 fetch 通道）目标 ' + TARGET, '',
             '| BK | 名称 | 类型 | bars | 首根 | 末根 | OOS | 状态 |',
             '|---|---|---|---:|---|---|---|---|']
    ok = fail = 0
    consec_fail = 0
    JS_FETCH = '''async (url) => {
        try {
            const resp = await fetch(url, {credentials: 'include'});
            return {status: resp.status, text: await resp.text()};
        } catch (e) {
            return {error: String(e)};
        }
    }'''

    def launch(p):
        b = p.chromium.launch(headless=True)
        ctx = b.new_context()
        pg = ctx.new_page()
        pg.goto('https://quote.eastmoney.com/bk/90.BK0428.html',
                wait_until='domcontentloaded', timeout=30000)
        pg.wait_for_timeout(3000)
        return b, ctx, pg

    with sync_playwright() as p:
        b, ctx, pg = launch(p)
        try:
            for i, (bk, info) in enumerate(todo, 1):
                if i > 1 and (i - 1) % 10 == 0:
                    try:
                        pg.reload(wait_until='domcontentloaded', timeout=30000)
                        pg.wait_for_timeout(2000)
                    except Exception:
                        pass
                if consec_fail >= 5:
                    print('consecutive fail >=5, restart browser', flush=True)
                    try:
                        ctx.close()
                        b.close()
                    except Exception:
                        pass
                    consec_fail = 0
                    b, ctx, pg = launch(p)

                url = ('https://push2his.eastmoney.com/api/qt/stock/kline/get'
                       '?secid=90.%s&klt=101&fqt=1&beg=20150101&end=20500101'
                       '&fields1=f1&fields2=f51,f52,f53,f54,f55,f56' % bk)
                try:
                    res = pg.evaluate(JS_FETCH, url)
                    if res.get('error'):
                        raise RuntimeError(res['error'][:80])
                    data = json.loads(res.get('text') or '{}')
                    ks = (data.get('data') or {}).get('klines')
                    if res.get('status') != 200 or not ks:
                        raise RuntimeError('http=%s empty' % res.get('status'))
                    rows = [k.split(',') for k in ks]
                    first, last = rows[0][0], rows[-1][0]
                    rec = {'code': bk, 'name': info['name'], 'type': info['type'],
                           'secid': '90.' + bk, 'klt': 101, 'fqt': 1,
                           'source': 'push2his.eastmoney.com(playwright-page-fetch)',
                           'fetched_at': TARGET, 'bars': len(rows),
                           'first': first, 'last': last, 'fields': FIELDS,
                           'covers_oos': first <= OOS_START, 'klines': rows}
                    (CACHE / (bk + '.json')).write_text(
                        json.dumps(rec, ensure_ascii=False), encoding='utf-8')
                    ok += 1
                    consec_fail = 0
                    lines.append('| %s | %s | %s | %d | %s | %s | %s | ok |'
                                 % (bk, info['name'], info['type'], len(rows), first, last,
                                    '是' if first <= OOS_START else '否'))
                    print('[%d/%d] %s ok bars=%d last=%s' % (i, len(todo), bk, len(rows), last),
                          flush=True)
                except Exception as e:
                    fail += 1
                    consec_fail += 1
                    lines.append('| %s | %s | %s | - | - | - | - | FAIL %s |'
                                 % (bk, info['name'], info['type'], str(e)[:40]))
                    print('[%d/%d] %s FAIL %s' % (i, len(todo), bk, str(e)[:60]), flush=True)
                time.sleep(SLEEP)
        finally:
            try:
                b.close()
            except Exception:
                pass

    lines += ['', '== 汇总：ok=%d skip=%d fail=%d / %d =='
              % (ok, len(codes) - len(todo), fail, len(codes)), '== done ==']
    OUT_MD.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('written', OUT_MD, flush=True)


if __name__ == '__main__':
    main()
