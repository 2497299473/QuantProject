# -*- coding: utf-8 -*-
"""V3 P0-1/P0-2 离网自检（2026-09-08 夜一次性任务留痕）。

只测纯逻辑：scope 集合、增量 beg 计算、合并断言、payload 体积对比。
不联网、不写任何缓存。
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import pull_sector_klines as P  # noqa: E402

fails = []


def chk(name, cond, detail=''):
    print('%-46s %s %s' % (name, 'PASS' if cond else 'FAIL', detail))
    if not cond:
        fails.append(name)


# ---- P0-1 scope ----
prod = P.load_codes('prod')
chk('prod 恰好 4 码', len(prod) == 4, str(sorted(prod)))
chk('prod 成员正确', sorted(prod) == ['BK0428', 'BK0457', 'BK0669', 'BK1173'])
names = {k: (v['name'], v['type'], v.get('role')) for k, v in prod.items()}
chk('prod 名称/类型/角色', names == {
    'BK0457': ('电网设备', '行业', 'production_dependency'),
    'BK0428': ('电力', '行业', 'channel_probe'),
    'BK0669': ('生态农业', '概念', 'channel_probe'),
    'BK1173': ('锂矿概念', '概念', 'channel_probe')}, str(names))
chk('默认 scope=prod', P.load_codes() == prod)
full = P.load_codes('full')
chk('full 仍 65 码（回滚路径完好）', len(full) == 65, str(len(full)))
res = P.load_codes('research')
chk('research 只含 BK（读 t1_watchlist）',
    len(res) == 11 and all(k.startswith('BK') for k in res), str(len(res)))
try:
    P.load_codes('bogus')
    chk('未知 scope 抛错', False)
except ValueError:
    chk('未知 scope 抛错', True)

# ---- P0-2 plan_beg ----
cache = BASE / 'data' / 'sector_klines'
real = {b: json.loads((cache / (b + '.json')).read_text(encoding='utf-8'))
        for b in prod if (cache / (b + '.json')).exists()}
chk('4 码缓存均可读', len(real) == 4, str(sorted(real)))

for bk, rec in sorted(real.items()):
    last = rec['last']
    # 断言按各码真实 last 推导，不写死日期：
    #   last==today → skip；last<today → incremental 且 beg=last 次日
    # （BK0669 实测停在 2026-09-07，其余 3 码到 09-08，正是两种分支的活样本）
    nxt = (datetime.strptime(last, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y%m%d')
    beg, mode, why = P.plan_beg(rec, last, False, False)
    chk('%s last==today → skip' % bk, mode == 'skip', '%s %s' % (mode, why))
    beg2, mode2, _ = P.plan_beg(rec, '2026-09-09', False, False)
    chk('%s last<today → incremental beg=last+1' % bk,
        mode2 == 'incremental' and beg2 == nxt,
        'last=%s got=%s/%s want=%s' % (last, mode2, beg2, nxt))
    beg3, mode3, _ = P.plan_beg(rec, '2026-09-09', False, True)
    chk('%s 周一强制全量' % bk, mode3 == 'full' and beg3 == '20150101')
    beg4, mode4, _ = P.plan_beg(rec, '2026-09-09', True, False)
    chk('%s --force-full' % bk, mode4 == 'full' and beg4 == '20150101')

chk('无缓存 → full', P.plan_beg(None, '2026-09-09', False, False)[1] == 'full')
chk('缓存自不一致 → full',
    P.plan_beg({'last': '2026-09-05', 'first': 'x', 'klines': [['y', 1]],
                'bars': 1}, '2026-09-09', False, False)[1] == 'full')

# payload 体积：增量 beg 从 12 年窗口压到 1 天
u_full = P.build_url('BK0457', '20150101')
u_inc = P.build_url('BK0457', '20260909')
bar_full = len([r for r in real['BK0457']['klines']])
chk('URL 窗口 beg=20150101 → 20260909',
    'beg=20150101' in u_full and 'beg=20260909' in u_inc and 'end=20500101' in u_inc)
print('   全量窗口 2015-01-05→今 = %d bars 请求体；增量窗口 = 1~2 日历日'
      % bar_full)

# ---- 合并断言 ----
old = real['BK0457']['klines']
new_ok = [['2026-09-09', '1', '2', '3', '0.5', '100']]
m = P.merge_klines(old, new_ok)
chk('正常追加 1 根', len(m) == len(old) + 1 and m[:len(old)] == old)
chk('重叠同值不重复追加', len(P.merge_klines(old, [old[-1]])) == len(old))
try:
    P.merge_klines(old, [[old[-1][0], '9', '9', '9', '9', '9']])
    chk('重叠异值 → MergeRejected', False)
except P.MergeRejected:
    chk('重叠异值 → MergeRejected', True)
try:
    P.merge_klines(old, [['2026-09-11', '1', '2', '3', '4', '5'],
                         ['2026-09-10', '1', '2', '3', '4', '5']])
    chk('降序 → MergeRejected', False)
except P.MergeRejected:
    chk('降序 → MergeRejected', True)
try:
    P.merge_klines([], new_ok)
    chk('空前缀 → MergeRejected', False)
except P.MergeRejected:
    chk('空前缀 → MergeRejected', True)

# make_rec 新字段
rec = P.make_rec('BK0457', prod['BK0457'], m, '2026-09-09', 'incremental',
                 '20260909', prev=real['BK0457'])
chk('rec.pull_mode', rec['pull_mode'] == 'incremental')
chk('rec.history_verified_at 非空', bool(rec['history_verified_at']))
chk('rec.history_full_verified_at 沿用上一轮',
    rec['history_full_verified_at'] == (real['BK0457'].get('history_full_verified_at')
                                        or real['BK0457'].get('fetched_at')))
chk('rec 既有键未动（消费方只读 klines）',
    rec['code'] == 'BK0457' and rec['bars'] == len(m) and rec['first'] == '2015-01-05'
    and rec['covers_oos'] is True and rec['klines'] == m)
rec_f = P.make_rec('BK0457', prod['BK0457'], m, '2026-09-09', 'full', '20150101',
                   prev=real['BK0457'])
chk('full 模式刷新 history_full_verified_at',
    rec_f['history_full_verified_at'] == rec_f['history_verified_at'])

# 周一判定
chk('2026-09-07(周一) prod 触发全量', P.is_weekly_full_day('2026-09-07', 'prod'))
chk('2026-09-08(周二) 不触发', not P.is_weekly_full_day('2026-09-08', 'prod'))
chk('full scope 不做周一全量（省预算）', not P.is_weekly_full_day('2026-09-07', 'full'))

print('\n== selftest fails: %d %s ==' % (len(fails), fails))
sys.exit(1 if fails else 0)
