# -*- coding: utf-8 -*-
"""板块 K 线严格预算补拉（2026-09-08 晚新增；同夜 V3 P0-1/P0-2 改造）。

为什么不用 pull_sector_klines_pw.py：
1. 连续失败自动重启浏览器（一轮实测重启 11 次）——IP 已被掐时这是继续撞墙；
2. 每 10 码 reload 板块页——每次重载带 ~10 个东财子请求，65 码实际打东财 ~200 次；
3. 移动 CGNAT 共享池的频控预算是全池共享的，必须按「总请求数」严格控预算。

V3 改造（output/V3_data_layer_plan_20260908.md 第三节）
- P0-1：默认 --scope prod（4 码），与主/evening 脚本同语义；full 保留为回滚路径。
- P0-2：增量窗口 + 合并断言复用 pull_sector_klines.fetch_and_store（单一实现）。
  浏览器内 fetch 通过回调注入，scheme 必须 https（页面是 https，否则 mixed-content）。
- 报告 md 首行写明 scope= 与 codes=。

本脚本的纪律（全部硬编码，未改动）：
- 路由拦截只放行「首个板块页 document + 显式 kline fetch」，其余子请求一律 abort；
- 全程不 reload、不中途重启；每 CHUNK(12) 码才新建一次浏览器会话；
- 连续失败 >= MAX_CONSEC(15) → 整轮中止（判定 IP 被掐，继续只会加重）；
- 限速 SLEEP=2.5s，回调内不再重试；FAIL 不写缓存（不产生半成品）。

用法（全局 Python，venv 无 playwright）：
  D:\\Python\\python.exe -X utf8 pull_sector_klines_burst.py                    # prod 4 码
  D:\\Python\\python.exe -X utf8 pull_sector_klines_burst.py --scope full       # 回滚路径
  D:\\Python\\python.exe -X utf8 pull_sector_klines_burst.py --limit 15         # 小批量试水
报告：output/pull_sector_klines_burst_YYYYMMDD.md（独立文件，不覆盖 pw 报告）
"""
import argparse
import time
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

from pull_sector_klines import (OUT_DIR as _MAIN_CACHE, SCOPES,  # noqa: F401
                                fetch_and_store, is_weekly_full_day,
                                load_codes)

BASE = Path(__file__).resolve().parent
CACHE = BASE / 'data' / 'sector_klines'
CHUNK = 12
MAX_CONSEC = 15
SEED_BK = 'BK0428'
SOURCE_BURST = 'push2his.eastmoney.com(playwright-burst)'

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


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--scope', choices=SCOPES, default='prod',
                    help='prod=生产最小集 4 码（默认）；research=t1_watchlist；full=旧 65 码')
    ap.add_argument('--date', default=None)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--force-full', action='store_true')
    args = ap.parse_args(argv)

    target = args.date or (date.today() if date.today().weekday() < 5
                           else _last_weekday()).isoformat()
    out_md = BASE / 'output' / ('pull_sector_klines_burst_'
                                + target.replace('-', '') + '.md')
    weekly_full = is_weekly_full_day(target, args.scope)

    codes = load_codes(args.scope)
    todo = []
    for bk, info in sorted(codes.items()):
        from pull_sector_klines import load_old          # 同一实现，避免口径分叉
        old = load_old(CACHE / (bk + '.json'))
        if old and old.get('last') == target:
            continue
        todo.append((bk, info))
    if args.limit > 0:
        todo = todo[:args.limit]
    print('burst target=%s scope=%s todo=%d (cache fresh=%d) monday_full=%s'
          % (target, args.scope, len(todo), len(codes) - len(todo), weekly_full),
          flush=True)
    head = ('# 板块 K 线严格预算补拉 %s（burst 模式：拦子请求/不reload/连败即停）\n'
            'scope=%s codes=%s limit=%d force_full=%s monday_full=%s\n'
            % (target, args.scope, ','.join(sorted(codes)), args.limit,
               args.force_full, weekly_full))
    if not todo:
        out_md.write_text(head + '\n== 汇总：ok=0 skip=%d fail=0 / 本轮 0 ==\n'
                          % len(codes), encoding='utf-8')
        return

    ok = fail = skip = requests_sent = 0
    consec = 0
    aborted = False
    state = {'pg': None}

    def _fetch(bk, beg):
        """浏览器内 fetch：走 https，解析成 arr；空/异常一律抛出（不写半成品）。"""
        import json as _json
        res = state['pg'].evaluate(JS_FETCH, _url(bk, beg))
        if res.get('error'):
            raise RuntimeError(res['error'][:60])
        data = _json.loads(res.get('text') or '{}')
        ks = (data.get('data') or {}).get('klines')
        if res.get('status') != 200 or not ks:
            raise RuntimeError('http=%s empty(beg=%s)' % (res.get('status'), beg))
        return [k.split(',') for k in ks]

    def _url(bk, beg):
        from pull_sector_klines import build_url
        return build_url(bk, beg, 'https')

    lines = [head, '', '| BK | 名称 | 类型 | mode | beg | bars | 首根 | 末根 | 状态 |',
             '|---|---|---|---|---|---:|---|---|---|']

    with sync_playwright() as p:
        sess = 0
        b = ctx = pg = None
        for i, (bk, info) in enumerate(todo, 1):
            if sess == 0 or (i - 1) % CHUNK == 0:      # 新浏览器会话（全轮最多几次）
                sess += 1
                print('-- session', sess, flush=True)
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
                    state['pg'] = pg
                except Exception as e:
                    print('session launch FAIL', str(e)[:90], flush=True)
                    aborted = True
                    break

            d = fetch_and_store(bk, info, CACHE / (bk + '.json'), today=target,
                                fetch=_fetch, force_full=args.force_full,
                                weekly_full=weekly_full, source=SOURCE_BURST,
                                timeout=15, retries=0)
            requests_sent += d['requests']
            if d['status'] == 'skip':
                skip += 1
                consec = 0
                lines.append('| %s | %s | %s | skip | - | %d | %s | %s | skip(当日已拉) |'
                             % (bk, info['name'], info['type'], d['bars'],
                                d['first'], d['last']))
                print('[%d/%d] %s skip' % (i, len(todo), bk), flush=True)
                continue
            if d['status'] == 'fail':
                fail += 1
                consec += 1
                lines.append('| %s | %s | %s | %s | %s | - | - | - | FAIL %s |'
                             % (bk, info['name'], info['type'], d['mode'], d['beg'],
                                d['error']))
                print('[%d/%d] %s FAIL %s (consec=%d)'
                      % (i, len(todo), bk, d['error'][:50], consec), flush=True)
                if consec >= MAX_CONSEC:
                    print('!! 连续失败 %d 次，判定 IP 被掐，整轮中止（不再撞墙）' % consec,
                          flush=True)
                    aborted = True
                    break
                time.sleep(SLEEP_BURST)
                continue
            ok += 1
            consec = 0
            lines.append('| %s | %s | %s | %s | %s | %d | %s | %s | ok%s |'
                         % (bk, info['name'], info['type'], d['mode'], d['beg'],
                            d['bars'], d['first'], d['last'],
                            (' ' + d['note']) if d['note'] else ''))
            print('[%d/%d] %s ok mode=%s bars=%d %s'
                  % (i, len(todo), bk, d['mode'], d['bars'], d['note']), flush=True)
            time.sleep(SLEEP_BURST)

        try:
            if ctx:
                ctx.close()
            if b:
                b.close()
        except Exception:
            pass

    lines += ['', '== 汇总：ok=%d skip=%d fail=%d / 本轮 %d（aborted=%s）=='
              % (ok, skip, fail, len(todo), aborted),
              '== 东财请求数=%d ==' % requests_sent, '== done ==']
    out_md.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('written', out_md, flush=True)


SLEEP_BURST = 2.5      # burst 专用限速（比主脚本 2.0 更保守），保持原纪律


if __name__ == '__main__':
    main()
