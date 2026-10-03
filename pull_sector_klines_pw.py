# -*- coding: utf-8 -*-
"""Playwright 页面内 fetch 拉取东财板块 K 线（指纹拦截期兜底通道，v2 固化版）。

背景（2026-09-05 凌晨实测）：
- 东财对 push2his kline/get 做客户端指纹级拦截（TLS/JA3/h2 层），
  curl/curl_cffi 四指纹/编号子域名/ut token/cookie 重放全部连接层被掐；
- 真实 Chromium 页面内 fetch 曾验证可通（00:13），后因高频请求触发频控失效；
- 本脚本与原版 pull_sector_klines.py 输出格式完全兼容（同一缓存目录/schema）。

用法（D8-05 起 argparse 化，裸位置参数已死亡）：
- python3 pull_sector_klines_pw.py                      # TARGET=今天(工作日)或上一交易日
- python3 pull_sector_klines_pw.py --date 2026-09-04    # 显式指定目标日期（补历史缺口）
- python3 pull_sector_klines_pw.py --help               # 安全退出（码 0，0 次 playwright 启动）
- python3 pull_sector_klines_pw.py                      # 裸跑 → 拒绝（码 3，0 网络）

纪律：限速 >=2s；FAIL 跳过不中断；缓存 last==TARGET 则 skip；
连续 5 失败重启浏览器（D8-05：封顶 MAX_RESTARTS=2，达上限仍连败 → 判定 IP 被掐
整轮中止，报告记 aborted=True —— 09-08「自动重启 12 次全灭」事故形态的代码封口）；
每 10 码 reload 刷新连接池。

D8-05（面 8，2026-10-03）：旧版 sys.argv[1] 直读日期 —— `--help` 会被当成 TARGET
字符串（old.get('last') != '--help' 恒真）触发全量 65 码真实拉取，违反铁律 8
「--help 属零副作用自检」前提；同型事故已实际发生一次（2026-10-02 01:35，
diag 脚本 secid=90.--help 真实发请求）。argparse 化后未知参数直接报错退出，
--help 在解析层安全退出（码 0，不触达 sync_playwright）。

D8-04（面 8，Summer 2026-10-03 裁决选项 1）：授权闸门——本脚本跑在全局 Python 下
（不能 import 项目模块），闸门消息本地自持；任何执行（含 --date 路径）都须
--authorized 显式授权；裸跑拒绝退出码 3、0 次浏览器启动、0 网络请求。
铁律 1 明令禁盲跑 pw（09-05/09-08 事故），本闸门把「盲跑」从纪律约束升为代码强制。
"""
import argparse
import json
import re
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
MAX_RESTARTS = 2      # D8-05：连败重启封顶（旧版无上限，65 码理论最多 13 轮重启）
FIELDS = ['date', 'open', 'close', 'high', 'low', 'volume']

# D8-04（选项 1）授权闸门提示（本地自持：全局 Python 运行环境不能 import 项目模块）
AUTH_REQUIRED_MSG = (
    '[拒绝] 东财拉取需显式授权（铁律 7/8，面 8 D8-04 选项 1，2026-10-03）：\n'
    '  pw 兑底是人工通道（铁律 1 禁盲跑）：加 --authorized 并确认已过铁律 7 四项征兆闸门\n'
    '  （上游已跑完/无频控征兆/无并行拉取/在时间盒内）且获 Summer 批准。\n'
    '裸跑一律拒绝：退出码 3，本次未启动浏览器、未发出任何网络请求。')


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


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Playwright 页面内 fetch 拉取东财板块 K 线（指纹拦截期兑底通道）')
    ap.add_argument('--date', default=None,
                    help='目标日期 YYYY-MM-DD（补历史缺口）；缺省=今天(工作日)或上一交易日。'
                         'D8-05：旧裸位置参数已死亡（--help 曾被当日期触发全量拉取）')
    ap.add_argument('--authorized', action='store_true',
                    help='D8-04 授权旗标：pw 兑底必须人工显式授权（铁律 7 四闸门 + 批准）')
    args = ap.parse_args(argv)

    # D8-04 授权闸门：先于任何网络路径（load_codes/sync_playwright 均未触达）。
    if not args.authorized:
        print(AUTH_REQUIRED_MSG, flush=True)
        raise SystemExit(3)
    TARGET = args.date or (date.today() if date.today().weekday() < 5
                           else _last_weekday()).isoformat()
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
    restarts = 0          # D8-05：重启计数（封顶 MAX_RESTARTS）
    aborted = False       # 重启用尽仍连败 → 整轮中止（报告记 aborted=True）
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
                    if restarts >= MAX_RESTARTS:
                        # D8-05：封顶——铁律 1 禁盲跑 pw（拿被掐的 IP 反复撞）；
                        # 09-08 实证：自动重启 12 次全灭。达上限即整轮中止。
                        print('!! 连败 >=5 且重启已用尽（%d/%d 次），判定 IP 被掐，'
                              '整轮中止（铁律 1：不再撞墙，等 21:30 晚间补拉或下一交易日）'
                              % (restarts, MAX_RESTARTS), flush=True)
                        aborted = True
                        break
                    restarts += 1
                    print('[warn] THROTTLE 嫌疑：连续失败 >=5，重启浏览器（%d/%d）——'
                          '重启后仍连败将按铁律 1 中止整轮'
                          % (restarts, MAX_RESTARTS), flush=True)
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

    lines += ['', '== 汇总：ok=%d skip=%d fail=%d / %d（aborted=%s）=='
              % (ok, len(codes) - len(todo), fail, len(codes), aborted), '== done ==']
    OUT_MD.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('written', OUT_MD, flush=True)


if __name__ == '__main__':
    main()
