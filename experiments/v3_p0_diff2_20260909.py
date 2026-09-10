# -*- coding: utf-8 -*-
"""V3 P0 Phase B-2 修正版：冻结指标必须比对**快照的真实结构层级**。

为什么要有这份文件（09-09 晨自查发现，属验收夹具缺陷，非生产代码缺陷）：
旧夹具 v3_p0_diff_20260908.py 比较的是 snap["quality"]，但快照里**不存在**
"quality" 键 —— spread/breadth/regime 在顶层，质量块叫
"market_context_quality"。于是九项聚合指标全部以 None==None 判成 SAME，
是「空对空」的伪通过。theme 层的 13/13 逐项比对是真实发生的（旧夹具那部分有效）。
本文件把层级改对，并显式打印 old 侧非 None 的项数，杜绝再次伪通过。

冻结口径（v0.1）：
  顶层   spread_5d / spread_original_5d / spread_relaxed_5d
  顶层   breadth_all_above_ma20 / breadth_original_above_ma20 / breadth_relaxed_above_ma20
  顶层   regime / ok / as_of
  quality market_context_quality.coverage / .usable_for_oos / .errors
          / .themes_switched / .themes_total / .themes_original / .themes_relaxed
  theme  themes.*.r1d / r5d / r20d / above_ma20 / as_of / code / proxy_switch
允许差异（非漂移，单独列出）：
  · P0-0 新增质量字段在旧快照缺失 → 记 ADDED
  · themes.*.bars 允许 ±1（输入深度诊断量，非冻结口径，须归因）
"""
import hashlib
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

SNAP_DIR = BASE / 'data' / 'market_context'
HIST = SNAP_DIR / 'history.jsonl'

FROZEN_TOP = ('spread_5d', 'spread_original_5d', 'spread_relaxed_5d',
              'breadth_all_above_ma20', 'breadth_original_above_ma20',
              'breadth_relaxed_above_ma20', 'regime', 'ok', 'as_of')
FROZEN_Q = ('coverage', 'usable_for_oos', 'errors', 'themes_switched',
            'themes_total', 'themes_original', 'themes_relaxed',
            'oos_quality', 'themes_bk_source')
FROZEN_THEME = ('r1d', 'r5d', 'r20d', 'above_ma20')
NEW_Q_ALLOWED = {'max_lag_trading_days', 'stale_themes', 'data_ref_last',
                 'calendar_gap_days', 'usable_for_oos_reason'}


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    snaps = sorted(p for p in SNAP_DIR.glob('*.json'))
    if not snaps:
        print('NO SNAPSHOTS')
        return 1
    target = snaps[-1]
    as_of = target.stem
    print('as_of_date =', as_of, '| baseline =', target.name)

    hist_before = (sha(HIST), HIST.read_bytes().decode('utf-8').count('\n'))
    snap_before = sha(target)
    dir_before = {p.name: sha(p) for p in sorted(SNAP_DIR.iterdir())}

    from core import market_context as mc          # noqa: E402
    new = mc.compute(slot='post', save=False, as_of_date=as_of)
    old = json.loads(target.read_text(encoding='utf-8'))

    hist_after = (sha(HIST), HIST.read_bytes().decode('utf-8').count('\n'))
    snap_after = sha(target)
    dir_after = {p.name: sha(p) for p in sorted(SNAP_DIR.iterdir())}

    bad, added, bars_diff = [], [], []

    # ---- 顶层冻结指标（层级修正）----
    n_real = sum(1 for k in FROZEN_TOP if old.get(k) is not None)
    print('\n-- 顶层冻结指标（old 侧非 None：%d/%d，证明比对非空）--'
          % (n_real, len(FROZEN_TOP)))
    for k in FROZEN_TOP:
        o, n = old.get(k), new.get(k)
        same = o == n
        print('  %-30s old=%-22r new=%-22r %s' % (k, o, n, 'SAME' if same else 'CHANGED'))
        if not same:
            bad.append('top.%s: %r -> %r' % (k, o, n))
        if k in ('spread_5d', 'breadth_all_above_ma20', 'regime') and o is None:
            bad.append('top.%s old 侧为 None —— 夹具层级又错了' % k)

    # ---- 质量块 ----
    oq, nq = old.get('market_context_quality') or {}, new.get('market_context_quality') or {}
    if not oq:
        bad.append('market_context_quality 在旧快照缺失 —— 夹具层级又错了')
    print('\n-- market_context_quality --')
    for k in FROZEN_Q:
        o, n = oq.get(k), nq.get(k)
        same = o == n
        print('  %-26s old=%-12r new=%-12r %s' % (k, o, n, 'SAME' if same else 'CHANGED'))
        if not same:
            bad.append('quality.%s: %r -> %r' % (k, o, n))
    for k in sorted(set(nq) - set(oq)):
        added.append('quality.%s = %r (P0-0 新增，旧快照无此键)' % (k, nq.get(k)))

    # ---- theme 层 ----
    o_t, n_t = old.get('themes') or {}, new.get('themes') or {}
    if set(o_t) != set(n_t):
        bad.append('themes 集合不同: %s' % sorted(set(o_t) ^ set(n_t)))
    print('\n-- themes (%d/%d) --' % (len(o_t), len(n_t)))
    for t in sorted(set(o_t) & set(n_t)):
        for k in FROZEN_THEME + ('as_of', 'code', 'proxy_switch', 'gate', 'data_source'):
            if o_t[t].get(k) != n_t[t].get(k):
                bad.append('theme %s.%s: %r -> %r'
                           % (t, k, o_t[t].get(k), n_t[t].get(k)))
        if o_t[t].get('bars') != n_t[t].get('bars'):
            bars_diff.append('theme %s.bars: %s -> %s (delta=%s)'
                             % (t, o_t[t].get('bars'), n_t[t].get('bars'),
                                (n_t[t].get('bars') or 0) - (o_t[t].get('bars') or 0)))
    print('  r1d/r5d/r20d/above_ma20/as_of/code/proxy_switch/gate/data_source 逐项比对完成')

    print('\n-- bars 差异（允许 ±1，须归因）--')
    for b in bars_diff or ['  无']:
        print('  ', b)
    print('\n-- 允许项（非漂移）--')
    for a in added or ['  无']:
        print('  ', a)

    print('\n-- 不变性 --')
    print('  history.jsonl :', hist_before, hist_after,
          'SAME' if hist_before == hist_after else 'CHANGED!!')
    print('  snapshot sha256:', 'UNCHANGED' if snap_before == snap_after else 'REWRITTEN!!')
    print('  目录内所有文件 :', 'UNCHANGED' if dir_before == dir_after else 'CHANGED!!')
    for nm in sorted(set(dir_before) | set(dir_after)):
        if dir_before.get(nm) != dir_after.get(nm):
            print('    DIFF', nm)

    ok = (not bad) and hist_before == hist_after and snap_before == snap_after \
        and dir_before == dir_after
    print('\n== Phase B-2(修正版):', 'PASS(冻结指标零变化)' if ok else 'FAIL', '==')
    for b in bad:
        print('  VIOLATION:', b)
    return 0 if ok else 2


if __name__ == '__main__':
    sys.exit(main())
