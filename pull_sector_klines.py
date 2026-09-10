#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板块 K 线批量拉取缓存（2026-09-02 创建；2026-09-08 夜 V3 P0-1/P0-2 改造）。

B 组纪律：限速 >=2s；FAIL 跳过不中断；当日不重复拉（last==今天则 skip）。

V3 改造（依据 output/V3_data_layer_plan_20260908.md 第三节，Summer 2026-09-08 拍板）
- P0-1 抓取范围：新增 --scope prod|research|full，**默认 prod（4 码）**。
  实测事实：65 码里只有 BK0457 被生产链路（core/market_context.py 读
  data/sector_klines/BK0457.json）消费，其余 61 码零消费者 → 每日全量 65 码
  是在为不存在的依赖烧东财请求预算。prod 集**写死为下方常量**，不从映射表解析，
  避免下次改表把抓取面悄悄放大。
  · BK0457 电网设备 = 唯一生产依赖
  · BK0428 电力 / BK0669 生态农业 / BK1173 锂矿概念 = 东财**通道健康探针**
    （用 3 次额外请求判断封禁是整体还是单点，在 BK0457 陈旧前预警）
  那 61 码的 staleness 不得进任何生产告警。
- P0-2 增量拉取：缓存存在且 last < today 时只请求 beg=last+1 天，旧 klines 作
  前缀、仅追加 date>last 的 bar。三条校验（重叠区逐位相同 / 严格升序无重复 /
  旧序列是新序列逐位相同前缀），任一不通过 → 该码回退全量并在报告记
  FALLBACK_FULL（防板块指数编制或历史修订被静默吃掉）。
  每周一（或 --force-full）对 prod 全码强制全量 2015→今覆盖校验。
- 数据完整性：空 payload 一律记 FAIL 且**不写缓存**（旧版会写出 bars=0 的半成品）。

本模块同时是共享逻辑的唯一出处（load_codes / plan_beg / merge_klines /
fetch_and_store / make_rec），evening 与 burst 脚本一律 import 复用，避免三份
断言各自漂移。

缓存：data/sector_klines/{BK}.json，klines 为 [date,open,close,high,low,volume] 数组。
产物：output/pull_sector_klines_YYYYMMDD.md（一天一份；首行写明 scope= 与 codes=，
      否则审计时无法区分「没拉」和「不需要拉」）。

用法：
  .\\.venv\\Scripts\\python.exe -X utf8 pull_sector_klines.py                 # prod 4 码
  .\\.venv\\Scripts\\python.exe -X utf8 pull_sector_klines.py --scope full    # 回滚路径
"""
import argparse
import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from core import netutil

BASE = Path(__file__).resolve().parent
OUT_DIR = BASE / 'data' / 'sector_klines'
OUT_MD = BASE / 'output' / f'pull_sector_klines_{date.today().strftime("%Y%m%d")}.md'
MAP_MD = BASE / 'output' / 'sector_name_mapping_20260902.md'
T1_JSON = BASE / 'data' / 't1_watchlist.json'
TODAY = date.today().isoformat()
OOS_START = '2020-01-01'
SLEEP = 2.0
SOURCE_DEFAULT = 'push2his.eastmoney.com'

# ---------------------------------------------------------------- P0-1 抓取范围
FULL_BEG = '20150101'          # 全量窗口起点（≈12 年）
END = '20500101'
SCOPES = ('prod', 'research', 'full')
TYPE_BY_EN = {'industry': '行业', 'concept': '概念'}

# 生产最小集：写死常量（**不要**改成从 output/sector_name_mapping_*.md 解析）。
# 名称/类型与映射表 20260902 版逐项核对过；改这里等于改生产抓取面，需 Summer 拍板。
PROD_CODES = {
    'BK0457': {'user': '电网设备', 'name': '电网设备', 'type': '行业',
               'role': 'production_dependency'},
    'BK0428': {'user': '电力', 'name': '电力', 'type': '行业',
               'role': 'channel_probe'},
    'BK0669': {'user': '农业', 'name': '生态农业', 'type': '概念',
               'role': 'channel_probe'},
    'BK1173': {'user': '锂矿', 'name': '锂矿概念', 'type': '概念',
               'role': 'channel_probe'},
}


def load_codes(scope: str = 'prod') -> dict:
    """按 scope 返回 {BK: {user,name,type[,role]}}。默认 prod。"""
    if scope == 'prod':
        return {k: dict(v) for k, v in PROD_CODES.items()}
    if scope == 'research':
        out = {}
        for it in json.loads(T1_JSON.read_text(encoding='utf-8')).get('watchlist', []):
            code = it.get('code', '')
            if not re.fullmatch(r'BK\d{4}', code):
                continue                       # 跳过 etf_proxy（腾讯链，非本脚本职责）
            out[code] = {'user': it.get('name', code), 'name': it.get('name', code),
                         'type': TYPE_BY_EN.get(it.get('type', ''), it.get('type', ''))}
        return out
    if scope == 'full':
        text = MAP_MD.read_text(encoding='utf-8')
        rows = re.findall(
            r'\|\s*([^|]+?)\s*\|\s*(BK\d{4})\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|',
            text)
        seen = {}
        for user, bk, name, typ in rows:
            if bk not in seen:
                seen[bk] = {'user': user, 'name': name, 'type': typ}
        return seen
    raise ValueError(f'unknown scope: {scope}')


# ---------------------------------------------------------------- P0-2 请求构造
def build_url(bk: str, beg: str = FULL_BEG, scheme: str = 'http') -> str:
    """beg 为 YYYYMMDD。增量时传 last 的次日，把 12 年窗口压到 1~2 个日历日。

    scheme：主链路用 http（东财 TLS 指纹过滤，见下方注释）；burst 在浏览器内跑，
    页面是 https，必须走 https 否则被 mixed-content 拦。"""
    return ('%s://push2his.eastmoney.com/api/qt/stock/kline/get'
            '?secid=90.' + bk + '&klt=101&fqt=1&beg=' + str(beg) + '&end=' + END +
            '&fields1=f1&fields2=f51,f52,f53,f54,f55,f56') % scheme


def next_day_compact(iso: str) -> str:
    return (datetime.strptime(iso, '%Y-%m-%d').date()
            + timedelta(days=1)).strftime('%Y%m%d')


def plan_beg(old: dict | None, today: str, force_full: bool,
             weekly_full: bool) -> tuple[str, str, str]:
    """决定请求起点。返回 (beg_compact, mode, why)；mode ∈ full|incremental|skip。"""
    if force_full:
        return FULL_BEG, 'full', 'force_full'
    if weekly_full:
        return FULL_BEG, 'full', 'monday_full'
    if not old:
        return FULL_BEG, 'full', 'no_cache'
    kl = old.get('klines') or []
    last = old.get('last') or ''
    first = old.get('first') or ''
    if not kl or not last:
        return FULL_BEG, 'full', 'cache_incomplete'
    if first != kl[0][0] or last != kl[-1][0]:
        return FULL_BEG, 'full', 'cache_selfinconsistent'
    if len(kl) != old.get('bars', len(kl)):
        return FULL_BEG, 'full', 'cache_bars_mismatch'
    if str(last) >= str(today):
        return '', 'skip', 'cache_last_ge_target'
    return next_day_compact(last), 'incremental', 'cache_last_' + last


class MergeRejected(Exception):
    """增量合并断言不通过 → 调用方回退全量并记 FALLBACK_FULL。"""


def merge_klines(old_arr: list, new_arr: list) -> list:
    """旧 klines 为前缀，只追加 date > last 的 bar；按 date 去重。

    校验（任一失败抛 MergeRejected）：
      1. 重叠区逐位相同 —— 增量窗口本不该含旧日期；若服务端无视 beg 返回了
         date<=last 的 bar 且值不同，即为「历史被静默修订」的真实探测器。
      2. 合并后 date 严格升序、无重复。
      3. 旧序列是新序列的逐位相同前缀（防合并逻辑自己把历史排序/去重改掉）。
    """
    if not old_arr:
        raise MergeRejected('empty_old_sequence')
    last = old_arr[-1][0]
    by_date = {r[0]: r for r in old_arr}
    for r in new_arr:
        if r[0] <= last and by_date.get(r[0]) != r:
            raise MergeRejected('overlap_mismatch@' + str(r[0]))
    merged = list(old_arr) + [r for r in new_arr if r[0] > last]
    dates = [r[0] for r in merged]
    for a, b in zip(dates, dates[1:]):
        if a >= b:
            raise MergeRejected('not_strictly_ascending:%s>=%s' % (a, b))
    if merged[:len(old_arr)] != old_arr:
        raise MergeRejected('prefix_not_identical')
    return merged


def make_rec(bk: str, info: dict, arr: list, today: str, pull_mode: str,
             pull_beg: str, prev: dict | None = None,
             source: str = SOURCE_DEFAULT) -> dict:
    """统一的缓存记录结构。新增字段仅追加，不动既有键（消费方只读 klines）。"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    prev = prev or {}
    first, last = arr[0][0], arr[-1][0]
    # history_full_verified_at：最后一次「2015→今全量覆盖校验」的时间。
    # 增量只验前缀与重叠区，不能冒充全量校验，故沿用上一轮的值。
    full_verified = (now if pull_mode == 'full'
                     else (prev.get('history_full_verified_at')
                           or prev.get('fetched_at') or ''))
    return {
        'code': bk, 'name': info['name'], 'type': info['type'],
        'secid': '90.' + bk, 'klt': 101, 'fqt': 1,
        'source': source, 'fetched_at': today,
        'bars': len(arr), 'first': first, 'last': last,
        'fields': ['date', 'open', 'close', 'high', 'low', 'volume'],
        'covers_oos': bool(first and first <= OOS_START),
        'pull_mode': pull_mode,
        'pull_beg': pull_beg,
        'history_verified_at': now,
        'history_full_verified_at': full_verified,
        'klines': arr,
    }


def is_weekly_full_day(today: str, scope: str) -> bool:
    """每周一对 prod 全码强制全量（BK0457 历史前缀完整性优先于省流量）。"""
    if scope != 'prod':
        return False
    try:
        return datetime.strptime(today, '%Y-%m-%d').weekday() == 0
    except ValueError:
        return False


def load_old(fp: Path) -> dict | None:
    try:
        return json.loads(fp.read_text(encoding='utf-8'))
    except Exception:
        return None


def fetch_one(bk: str, beg: str, scheme: str = 'http', *, timeout: int = 15,
              retries: int = 2) -> list:
    """默认（原生 netutil）传输：拉一个窗口并解析成 arr。空 payload 抛错。"""
    j = netutil.http_get_json(build_url(bk, beg, scheme), timeout=timeout,
                              retries=retries)
    arr = [s.split(',') for s in ((j.get('data') or {}).get('klines') or [])]
    if not arr:
        raise RuntimeError('empty_payload(beg=%s)' % beg)
    return arr


def fetch_and_store(bk: str, info: dict, fp: Path, *, today: str,
                    fetch=None, scheme: str = 'http', write: bool = True,
                    force_full: bool = False, weekly_full: bool = False,
                    source: str = SOURCE_DEFAULT, timeout: int = 15,
                    retries: int = 2) -> dict:
    """单码拉取 + 增量合并 + 断言 + 落盘。主/evening/burst 三处共用。

    fetch: 可选回调 fetch(bk, beg) -> arr；缺失时用 fetch_one（原生 HTTP）。
           burst 传浏览器内 fetch 的包装。
    返回 detail：status(ok|skip|fail) / mode / beg / bars / first / last /
    covers_oos / appended / note / error / requests。
    失败不写缓存；断言不过自动回退全量（多算 1 次请求，报告记 FALLBACK_FULL）。
    """
    fetch = fetch or (lambda b, bg: fetch_one(b, bg, scheme, timeout=timeout,
                                              retries=retries))
    old = load_old(fp)
    if old and old.get('last') == today:
        return {'status': 'skip', 'mode': 'skip', 'beg': '', 'why': 'already_today',
                'bars': old.get('bars', 0), 'first': old.get('first', ''),
                'last': old.get('last', ''), 'covers_oos': bool(old.get('covers_oos')),
                'appended': 0, 'note': '', 'error': '', 'requests': 0}
    beg, mode, why = plan_beg(old, today, force_full, weekly_full)
    if mode == 'skip':
        return {'status': 'skip', 'mode': 'skip', 'beg': '', 'why': why,
                'bars': (old or {}).get('bars', 0),
                'first': (old or {}).get('first', ''),
                'last': (old or {}).get('last', ''),
                'covers_oos': bool((old or {}).get('covers_oos')),
                'appended': 0, 'note': '', 'error': '', 'requests': 0}

    requests = 1
    note = ''
    try:
        arr = fetch(bk, beg)
        if mode == 'incremental':
            try:
                merged = merge_klines(old['klines'], arr)
            except MergeRejected as e:
                note = 'FALLBACK_FULL(%s)' % e
                beg, mode = FULL_BEG, 'full'
                requests += 1
                arr = fetch(bk, beg)
                merged = arr
        else:
            merged = arr
        rec = make_rec(bk, info, merged, today, mode, beg, prev=old, source=source)
        if write:
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(json.dumps(rec, ensure_ascii=False), encoding='utf-8')
        return {'status': 'ok', 'mode': mode, 'beg': beg, 'bars': rec['bars'],
                'first': rec['first'], 'last': rec['last'],
                'covers_oos': rec['covers_oos'],
                'appended': len(merged) - len((old or {}).get('klines') or []),
                'note': note, 'error': '', 'requests': requests}
    except Exception as e:
        # 拉取失败按 skip 不中断；不写缓存（不产生半成品）
        return {'status': 'fail', 'mode': mode, 'beg': beg, 'bars': 0,
                'first': '', 'last': '', 'covers_oos': False, 'appended': 0,
                'note': note, 'error': '%s:%s' % (type(e).__name__, str(e)[:60]),
                'requests': requests}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--scope', choices=SCOPES, default='prod',
                    help='prod=生产最小集 4 码（默认）；research=t1_watchlist；full=旧 65 码')
    ap.add_argument('--force-full', action='store_true',
                    help='所有码强制全量 2015→今（回退/对照用）')
    ap.add_argument('--date', default=None, help='覆盖 today（测试/补历史用）')
    args = ap.parse_args(argv)

    today = args.date or TODAY
    codes = load_codes(args.scope)
    weekly_full = is_weekly_full_day(today, args.scope)
    print('scope:', args.scope, 'codes:', len(codes),
          'force_full:', args.force_full, 'monday_full:', weekly_full, flush=True)

    lines = ['# 板块 K 线拉取缓存 %s scope=%s codes=%d' % (today, args.scope, len(codes)),
             'scope=%s codes=%s force_full=%s monday_full=%s' % (
                 args.scope, ','.join(sorted(codes)), args.force_full, weekly_full),
             '',
             '| BK | 名称 | 类型 | mode | beg | bars | 首根 | 末根 | OOS | 状态 |',
             '|---|---|---|---|---|---:|---|---|---|---|']
    ok = skip = fail = fallback_full = requests_sent = 0
    fail_codes = []
    for i, (bk, info) in enumerate(sorted(codes.items()), 1):
        d = fetch_and_store(bk, info, OUT_DIR / (bk + '.json'), today=today,
                            force_full=args.force_full, weekly_full=weekly_full)
        requests_sent += d['requests']
        if d['note'].startswith('FALLBACK_FULL'):
            fallback_full += 1
        oos = '是' if d['covers_oos'] else '否'
        if d['status'] == 'skip':
            skip += 1
            lines.append('| %s | %s | %s | skip | - | %d | %s | %s | %s | skip(当日已拉) |'
                         % (bk, info['name'], info['type'], d['bars'],
                            d['first'], d['last'], oos))
            print('[skip]', bk, info['name'], d['last'] or '-', d['why'], flush=True)
            continue
        if d['status'] == 'fail':
            fail += 1
            fail_codes.append(bk)
            lines.append('| %s | %s | %s | %s | %s | - | - | - | - | FAIL %s |'
                         % (bk, info['name'], info['type'], d['mode'], d['beg'],
                            d['error']))
            print('[FAIL]', bk, info['name'], d['error'], flush=True)
            time.sleep(SLEEP)
            continue
        ok += 1
        lines.append('| %s | %s | %s | %s | %s | %d | %s | %s | %s | ok%s |'
                     % (bk, info['name'], info['type'], d['mode'], d['beg'],
                        d['bars'], d['first'], d['last'], oos,
                        (' ' + d['note']) if d['note'] else ''))
        print('[ok %d/%d]' % (i, len(codes)), bk, info['name'], d['mode'], d['beg'],
              'appended=%d' % d['appended'], d['first'], d['last'], d['note'], flush=True)
        time.sleep(SLEEP)

    lines += ['',
              '== 汇总：ok=%d skip=%d fail=%d fallback_full=%d / %d =='
              % (ok, skip, fail, fallback_full, len(codes)),
              '== 东财请求数=%d（scope=%s；改造前 full 口径每轮 %d）=='
              % (requests_sent, args.scope, len(load_codes('full'))),
              'FAIL 码: ' + (','.join(fail_codes) if fail_codes else '无'),
              '== done ==']
    OUT_MD.write_text('\n'.join(lines), encoding='utf-8')
    print('written', OUT_MD, flush=True)


if __name__ == '__main__':
    main()
