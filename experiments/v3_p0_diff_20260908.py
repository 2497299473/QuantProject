# -*- coding: utf-8 -*-
"""V3 P0 Phase B 离线验收：compute 重算 vs 已存快照逐字段 diff。

as_of_date 取「最近有快照的日期」（当前 = 2026-09-08），save=False 不落盘。
冻结指标必须零变化：r1d/r5d/r20d/above_ma20、spread 三口径、breadth 三口径、
regime、coverage、usable_for_oos。bars 允许 ±1（需人工归因，写进报告）。
附带校验：history.jsonl 行数与 sha256 运行前后不变、快照文件未被改写。
"""
import hashlib
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

FROZEN_THEME_KEYS = ('r1d', 'r5d', 'r20d', 'above_ma20')
FROZEN_QUALITY_KEYS = ('spread', 'spread_original', 'spread_relaxed',
                       'breadth_all_above_ma20', 'breadth_original_above_ma20',
                       'breadth_relaxed_above_ma20', 'regime', 'coverage',
                       'usable_for_oos')

SNAP_DIR = BASE / 'data' / 'market_context'
HIST = SNAP_DIR / 'history.jsonl'


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    snaps = sorted(p for p in SNAP_DIR.glob('*.json'))
    if not snaps:
        print('NO SNAPSHOTS')
        return 1
    as_of = snaps[-1].stem                      # 最近有快照的日期
    print('as_of_date =', as_of, '| baseline snapshot =', snaps[-1].name)

    hist_before = (sha(HIST), HIST.read_text(encoding='utf-8').count('\n')) \
        if HIST.exists() else ('missing', 0)
    snap_before = sha(snaps[-1])

    from core import market_context as mc       # noqa: E402
    new = mc.compute(slot='post', save=False, as_of_date=as_of)
    old = json.loads(snaps[-1].read_text(encoding='utf-8'))

    hist_after = (sha(HIST), HIST.read_text(encoding='utf-8').count('\n')) \
        if HIST.exists() else ('missing', 0)
    snap_after = sha(snaps[-1])

    diffs, bars_diffs, bad = [], [], []
    o_t, n_t = old.get('themes') or {}, new.get('themes') or {}
    if set(o_t) != set(n_t):
        bad.append('themes 集合不同 %s vs %s'
                   % (sorted(set(o_t) ^ set(n_t)), ''))
    for t in sorted(set(o_t) & set(n_t)):
        for k in FROZEN_THEME_KEYS:
            if o_t[t].get(k) != n_t[t].get(k):
                bad.append('theme %s.%s: %r -> %r'
                           % (t, k, o_t[t].get(k), n_t[t].get(k)))
        if o_t[t].get('as_of') != n_t[t].get('as_of'):
            bad.append('theme %s.as_of: %r -> %r'
                       % (t, o_t[t].get('as_of'), n_t[t].get('as_of')))
        if o_t[t].get('code') != n_t[t].get('code'):
            bad.append('theme %s.code(代理): %r -> %r'
                       % (t, o_t[t].get('code'), n_t[t].get('code')))
        if o_t[t].get('bars') != n_t[t].get('bars'):
            bars_diffs.append('theme %s.bars: %r -> %r (delta=%s)'
                              % (t, o_t[t].get('bars'), n_t[t].get('bars'),
                                 (n_t[t].get('bars') or 0) - (o_t[t].get('bars') or 0)))
        if o_t[t].get('proxy_switch') != n_t[t].get('proxy_switch'):
            bad.append('theme %s.proxy_switch: %r -> %r'
                       % (t, o_t[t].get('proxy_switch'), n_t[t].get('proxy_switch')))

    o_q, n_q = old.get('quality') or {}, new.get('quality') or {}
    for k in FROZEN_QUALITY_KEYS:
        if o_q.get(k) != n_q.get(k):
            bad.append('quality.%s: %r -> %r' % (k, o_q.get(k), n_q.get(k)))
        else:
            diffs.append('quality.%s 相同 = %r' % (k, o_q.get(k)))

    print('\n-- 冻结指标逐项 --')
    for k in FROZEN_QUALITY_KEYS:
        print('  %-32s old=%-12r new=%-12r %s'
              % (k, o_q.get(k), n_q.get(k),
                 'SAME' if o_q.get(k) == n_q.get(k) else 'CHANGED'))
    print('  themes 数 %d/%d · 代理选取/as_of/' % (len(o_t), len(n_t)),
          '、'.join(FROZEN_THEME_KEYS), '零漂移' if not bad else '见违规')

    print('\n-- bars 差异（允许 ±1，需归因）--')
    if bars_diffs:
        for b in bars_diffs:
            print('  ', b)
    else:
        print('   无（bars 也逐位相同）')

    print('\n-- 不变性 --')
    print('  history.jsonl before/after:', hist_before, hist_after,
          'SAME' if hist_before == hist_after else 'CHANGED')
    print('  snapshot file sha256      :',
          'UNCHANGED' if snap_before == snap_after else 'REWRITTEN!!')

    ok = (not bad) and hist_before == hist_after and snap_before == snap_after
    print('\n== Phase B diff:', 'PASS(冻结指标零变化)' if ok else 'FAIL', '==')
    if bad:
        for b in bad[:40]:
            print('  VIOLATION:', b)
    return 0 if ok else 2


if __name__ == '__main__':
    sys.exit(main())
