# -*- coding: utf-8 -*-
"""板块 K 线严格预算补拉（2026-09-08 晚新增，配合手机热点等新出口 IP 使用）。

为什么不用 pull_sector_klines_pw.py：
1. 连续失败自动重启浏览器（一轮实测重启 11 次）——IP 已被掐时这是继续撞墙；
2. 每 10 码 reload 板块页——每次重载带 ~10 个东财子请求，65 码实际打东财 ~200 次；
3. 移动 CGNAT 共享池的频控预算是全池共享的，必须按「总请求数」严格控预算。

本脚本的纪律（全部硬编码）：
- 路由拦截只放行「首个板块页 document + 显式 kline fetch」，其余子请求一律 abort；
- 全程不 reload、不中途重启；每 CHUNK(12) 码才新建一次浏览器会话；
- 连续失败 >= MAX_CONSEC(15) → 整轮中止（判定 IP 被掐，继续只会加重）；
- 限速 SLEEP=2.5s，retries=0（页面内 fetch 本身无重试）。

用法（全局 Python，venv 无 playwright）：
  D:\\Python\\python.exe -X utf8 pull_sector_klines_burst.py            # 拉全部缺口码
  D:\\Python\\python.exe -X utf8 pull_sector_klines_burst.py --limit 15 # 小批量试水
报告：output/pull_sector_klines_burst_YYYYMMDD.md（独立文件，不覆盖 pw 报告）
"""
import argparse
import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent
CACHE = BASE / 'data' / 'sector_klines'
MAP_MD = BASE / 'output' / 'sector_name_mapping_20260902.md'
OOS_START = '2020-01-01'
SLEEP = 2.5
CHUNK = 12
MAX_CONSEC = 15
FIELDS = ['date', 'open', 'close', 'high', 'low', 'volume']
SEED_BK = 'BK0428'

JS_FETCH = '''async (url) => {
    try {
        const resp = await fetch(url);
        return {status: resp.status, text: await resp.text()};
    } catch (e) {
        return {error: String(e)};
    }
}'''


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
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None)
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()

    target = args.date or (date.today() if date.today().weekday() < 5
                           else _last_weekday()).isoformat()
    out_md = BASE / 'output' / ('pull_sector_klines_burst_'
                                + target.replace('-', '') + '.md')

    codes = load_codes()
    todo = []
    for bk, info in sorted(codes.items()):
        fp = CACHE / (bk + '.json')
        if fp.exists():
            try:
                if json.loads(fp.read_text(encoding='utf-8')).get('last') == target:
                    continue
            except Exception:
                pass
        todo.append((bk, info))
    if args.limit > 0:
        todo = todo[:args.limit]
    print('burst target=%s todo=%d (cache fresh=%d)'
          % (target, len(todo), len(codes) - len(todo)), flush=True)
    if not todo:
        out_md.write_text('# burst 无需补拉 %s\n\n== 汇总：ok=0 fail=0 ==\n' % target,
                          encoding='utf-8')
        return

    ok = fail = 0
    consec = 0
    aborted = False
    lines = ['# 板块 K 线严格预算补拉 ' + target + '（burst 模式：拦子请求/不reload/连败即停）',
             '', '| BK | 名称 | 类型 | bars | 首根 | 末根 | 状态 |',
             '|---|---|---|---:|---|---|---|']

    with sync_playwright() as p:
        sess = 0
        for i, (bk, info) in enumerate(todo, 1):
            if sess == 0 or (i - 1) % CHUNK == 0:      # 新浏览器会话（全轮最多几次）
                sess += 1
                print('-- session', sess, flush=True)
                ctx = b = pg = None
                try:
                    b = p.chromium.launch(headless=True)
                    ctx = b.new_context()
                    # 硬预算：只放行 kline 接口与首屏 document，其余全拦
                    ctx.route('**/*', lambda route: (
                        route.continue_()
                        if 'push2his.eastmoney.com' in route.request.url
                        or route.request.resource_type == 'document'
                        else route.abort()))
                    pg = ctx.new_page()
                    pg.goto('https://quote.eastmoney.com/bk/90.' + SEED_BK + '.html',
                            wait_until='domcontentloaded', timeout=30000)
                    pg.wait_for_timeout(2500)
                except Exception as e:
                    print('session launch FAIL', str(e)[:90], flush=True)
                    aborted = True
                    break

            url = ('https://push2his.eastmoney.com/api/qt/stock/kline/get'
                   '?secid=90.%s&klt=101&fqt=1&beg=20150101&end=20500101'
                   '&fields1=f1&fields2=f51,f52,f53,f54,f55,f56' % bk)
            try:
                res = pg.evaluate(JS_FETCH, url)
                if res.get('error'):
                    raise RuntimeError(res['error'][:60])
                data = json.loads(res.get('text') or '{}')
                ks = (data.get('data') or {}).get('klines')
                if res.get('status') != 200 or not ks:
                    raise RuntimeError('http=%s empty' % res.get('status'))
                rows = [k.split(',') for k in ks]
                rec = {'code': bk, 'name': info['name'], 'type': info['type'],
                       'secid': '90.' + bk, 'klt': 101, 'fqt': 1,
                       'source': 'push2his.eastmoney.com(playwright-burst)',
                       'fetched_at': target, 'bars': len(rows),
                       'first': rows[0][0], 'last': rows[-1][0], 'fields': FIELDS,
                       'covers_oos': rows[0][0] <= OOS_START, 'klines': rows}
                (CACHE / (bk + '.json')).write_text(
                    json.dumps(rec, ensure_ascii=False), encoding='utf-8')
                ok += 1
                consec = 0
                lines.append('| %s | %s | %s | %d | %s | %s | ok |'
                             % (bk, info['name'], info['type'], len(rows),
                                rows[0][0], rows[-1][0]))
                print('[%d/%d] %s ok bars=%d' % (i, len(todo), bk, len(rows)), flush=True)
            except Exception as e:
                fail += 1
                consec += 1
                lines.append('| %s | %s | %s | - | - | - | FAIL %s |'
                             % (bk, info['name'], info['type'], str(e)[:40]))
                print('[%d/%d] %s FAIL %s (consec=%d)'
                      % (i, len(todo), bk, str(e)[:50], consec), flush=True)
                if consec >= MAX_CONSEC:
                    print('!! 连续失败 %d 次，判定 IP 被掐，整轮中止（不再撞墙）' % consec,
                          flush=True)
                    aborted = True
                    break
            time.sleep(SLEEP)

        try:
            if ctx: ctx.close()
            if b: b.close()
        except Exception:
            pass

    lines += ['', '== 汇总：ok=%d fail=%d / 本轮 %d（aborted=%s）=='
              % (ok, fail, len(todo), aborted), '== done ==']
    out_md.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('written', out_md, flush=True)


if __name__ == '__main__':
    main()
