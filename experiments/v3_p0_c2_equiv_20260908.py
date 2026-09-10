# -*- coding: utf-8 -*-
"""V3 P0 Phase C-2 离线等价证明 + evening 脚本 P0-1/P0-3 行为测试。

背景：今晚 prod 4 码里 3 码「当日已拉」→ skip，唯一需增量的 BK0669 被东财
连接层掐断（封禁持续中），**实时**增量对照拿不到。但「增量合并后 bars 是否与
全量一致」本质是纯函数性质，可用真实缓存序列离线证明：
  全量序列 = 缓存里那次全量拉取的产物；增量场景 = 把该序列的前缀当作旧缓存、
  把 date>last 的尾部当作服务端增量应答，喂进 merge_klines 看能否还原全量序列。
  这对 4 个码的真实历史（含 BK0669 的 2839 根）逐个成立即可判 C-2 等价通过。
测试 2（含防篡改负例）：服务端「无视 beg 返回全窗」不得重复追加；服务端修订
历史必须 MergeRejected → 回退全量 FALLBACK_FULL。
测试 3：evening 脚本首行 scope=/codes=、来源标注随 --trigger 变化、且不再有
硬编码「Windows 计划任务」。全部走临时目录 + 打桩 fetch_and_store，不联网、
不写 output/zcode_runs/（AGENTS.md：OpenSquilla 执行不入 ZCode 日志）。
"""
import json
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import pull_sector_klines as P  # noqa: E402

CACHE = BASE / 'data' / 'sector_klines'
fails = []


def chk(name, cond, detail=''):
    print('%-58s %s %s' % (name, 'PASS' if cond else 'FAIL', detail))
    if not cond:
        fails.append(name)


print('== T1 增量合并 == 全量序列（真实 4 码缓存）==')
tot_inc_bars = 0
for bk in sorted(P.PROD_CODES):
    fp = CACHE / (bk + '.json')
    if not fp.exists():
        chk('%s 有缓存' % bk, False)
        continue
    full = json.loads(fp.read_text(encoding='utf-8'))['klines']
    for cut in (1, 5, 21):
        old = full[:-cut]
        last = old[-1][0]
        inc = [r for r in full if r[0] > last]          # 服务端按 beg=last+1 的应答
        merged = P.merge_klines(old, inc)
        chk('%s 截断%d根 增量合并还原全量' % (bk, cut),
            merged == full and len(merged) == len(full),
            'last=%s inc_bars=%d bars=%d' % (last, len(inc), len(merged)))
        if cut == 1:
            tot_inc_bars += len(inc)

print('\n== T2 防篡改负例 ==')
full = json.loads((CACHE / 'BK0457.json').read_text(encoding='utf-8'))['klines']
old = full[:-1]
chk('服务端无视 beg 返回全窗 → 不重复追加',
    P.merge_klines(old, full) == full)
# 09-09 晨修正：原用例把修订值写进「本地旧缓存」rev 再喂 date>last 的增量应答，
# 二者无重叠区，按构造**不可判定** —— merge_klines 不该、也无法抛错（它比对的是
# merged 前缀 vs 传入的 old，篡改值本身就在 old 里）。这不是代码缺陷，是测试设计错误。
# 真正可观测的历史修订签名 = 服务端回传了 date<=last 的 bar 且值不同（overlap_mismatch），
# 已由上一条与下一条覆盖；深层历史修订由「每周一 prod 强制全量 + --force-full」兜底。
revsrv = [list(r) for r in full[-4:]]                     # 服务端回传含旧日期的窗口
# 必须篡改 date<=last 的那根（old=full[:-3] → last=full[-4][0]，即 revsrv[0]）。
# 09-09 晨第一次改错成 revsrv[1]（日期 > last，属合法新增 bar，本就不该抛错）。
revsrv[0][2] = '0.001'                                   # 重叠区内历史收盘价被修订
try:
    P.merge_klines(full[:-3], revsrv)
    chk('服务端回传 date<=last 且异值 → MergeRejected(回退全量)', False)
except P.MergeRejected as e:
    chk('服务端回传 date<=last 且异值 → MergeRejected(回退全量)', True, str(e))
local_rev = [list(r) for r in full[:-1]]
local_rev[-3][2] = '0.001'
chk('本地缓存被改（无重叠区）→ 不抛错，属已知局限',
    P.merge_klines(local_rev, [full[-1]]) == local_rev + [full[-1]])
chk('该局限的兜底：周一 prod 强制全量覆盖校验',
    P.is_weekly_full_day('2026-09-07', 'prod')
    and P.plan_beg({'klines': local_rev, 'last': local_rev[-1][0],
                    'first': local_rev[0][0], 'bars': len(local_rev)},
                   '2026-09-09', False, True)[1] == 'full')
try:
    P.merge_klines(old, [list(old[-1])[:-1] + ['999']])
    chk('重叠区异值 → MergeRejected', False)
except P.MergeRejected:
    chk('重叠区异值 → MergeRejected', True)

print('\n== T3 payload 窗口对比（beg 12年 → 1天）==')
u_full = P.build_url('BK0457', P.FULL_BEG)
u_inc = P.build_url('BK0457', P.next_day_compact(full[-2][0]))
print('  全量:', u_full[:78], '...')
print('  增量:', u_inc[:78], '...')
win_full = 'beg=%s end=%s' % (P.FULL_BEG, P.END)
chk('全量窗口≈12年 / 增量窗口=1~2日历日',
    'beg=20150101' in u_full and 'beg=%s' % P.next_day_compact(full[-2][0]) in u_inc)
print('  全量应答 bars=%d → 增量应答 bars=%d（降幅 %.2f%%）'
      % (len(full), tot_inc_bars, 100.0 * (1 - tot_inc_bars / len(full))))

print('\n== T4 evening 脚本：scope 首行 + trigger 来源标注（打桩不联网）==')
import pull_sector_klines_evening as E  # noqa: E402

src = (BASE / 'pull_sector_klines_evening.py').read_text(encoding='utf-8')
chk('源码已无硬编码「Windows 计划任务 QuantFund_KlineEvening」',
    '，Windows 计划任务 QuantFund_KlineEvening' not in src)
chk('源码 --trigger 默认 manual', "default='manual'" in src)

tmp = Path(tempfile.mkdtemp(prefix='v3p0_evening_'))
calls = []


def fake_store(bk, info, fp, **kw):
    calls.append((bk, kw.get('today'), kw.get('force_full'), kw.get('weekly_full')))
    return {'status': 'ok', 'mode': 'incremental', 'beg': '20260909', 'bars': 1,
            'first': '2015-01-05', 'last': kw.get('today'), 'covers_oos': True,
            'appended': 1, 'note': '', 'error': '', 'requests': 1}


for trig in ('manual', 'scheduler'):
    calls.clear()
    md = tmp / ('evening_%s.md' % trig)
    zr = tmp / ('zruns_%s.md' % trig)
    E.OUT_MD, E.ZRUNS_MD = md, zr
    E.fetch_and_store = fake_store
    E._task_snapshot = lambda: 'LastRunTime=09/08/2026 21:30:00|LastTaskResult=0'
    E.SLEEP = 0
    E.main(['--trigger', trig])
    head = md.read_text(encoding='utf-8').splitlines()
    blk = zr.read_text(encoding='utf-8')
    chk('%s: md 首两行含 scope= 与 codes=' % trig,
        head[0].startswith('# 板块 K 线晚间补拉') and 'scope=' in head[0]
        and 'scope=%s' % 'prod' in head[1] and 'codes=BK0428' in head[1], head[1][:58])
    chk('%s: evening 也走 prod 4 码' % trig, len(calls) == 4, str(sorted(c[0] for c in calls)))
    chk('%s: 日志标注 执行方式=%s' % (trig, trig),
        ('- **执行方式**=%s' % trig) in blk, )
    chk('%s: 附 LastTaskResult/NextRunTime 快照' % trig, 'LastTaskResult=0' in blk)
    chk('%s: 节标题带时间戳可寻址' % trig, blk.count('## [') == 1)

print('\n== T5 主脚本 skip 语义不写盘（当日已拉不重拉）==')
before = (CACHE / 'BK0457.json').read_bytes()
d = P.fetch_and_store('BK0457', P.PROD_CODES['BK0457'], CACHE / 'BK0457.json',
                      today=full[-1][0])
chk('last==today → skip 且不落请求', d['status'] == 'skip' and d['requests'] == 0,
    d['why'])
chk('skip 不改动缓存文件', (CACHE / 'BK0457.json').read_bytes() == before)
d2 = P.fetch_and_store('BK0457', P.PROD_CODES['BK0457'], tmp / 'x.json',
                       today='2026-09-09', fetch=lambda b, g: (_ for _ in ()).throw(
                           RuntimeError('net down')), write=True)
chk('拉取失败 → fail 且不写半成品', d2['status'] == 'fail'
    and not (tmp / 'x.json').exists(), d2['error'][:40])

print('\n== fails: %d %s ==' % (len(fails), fails))
sys.exit(1 if fails else 0)
