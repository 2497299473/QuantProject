# -*- coding: utf-8 -*-
"""Phase B-2 bars ±1 归因取证（只读，不改任何数据）。

假设：bars 减少 1 来自 ETF 侧 _TTL_HOURS=12 缓存过期后重拉，腾讯返回窗口的
最老一根被裁掉（属输入深度诊断量，非冻结口径；只影响序列首端，r1d/r5d/r20d
与 MA20 只用末尾 21 根，故冻结指标逐位相同）。
本脚本用文件 mtime + 首根日期来证实或否证，不接受口头归因。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

SNAP = BASE / 'data' / 'market_context' / '2026-09-08.json'
STOCK = BASE / 'data' / 'stock_klines'
SECTOR = BASE / 'data' / 'sector_klines'

snap = json.loads(SNAP.read_text(encoding='utf-8'))
snap_mtime = datetime.fromtimestamp(SNAP.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')
print('snapshot 2026-09-08.json mtime =', snap_mtime)

theme_of_code = {}
for t, d in (snap.get('themes') or {}).items():
    theme_of_code[d.get('code')] = (t, d.get('data_source'), d.get('bars'),
                                    d.get('as_of'), d.get('proxy_switch'))

print('\n%-10s %-8s %-14s %-11s %-11s %8s  %s' % (
    'code', 'src', 'theme', 'snap_bars', 'cache_last', 'cache_bar', 'cache_mtime'))
rows = []
for code, (t, src, bars, as_of, sw) in sorted(theme_of_code.items()):
    for d in (STOCK, SECTOR):
        p = d / (code + '.json')
        if p.exists():
            break
    else:
        print('%-10s MISSING CACHE for %s' % (code, t))
        continue
    rec = json.loads(p.read_text(encoding='utf-8'))
    kl = rec.get('klines') or []
    mt = datetime.fromtimestamp(p.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')
    rows.append((code, src, t, bars, kl[-1][0] if kl else '?', len(kl), mt,
                kl[0][0] if kl else '?'))
    print('%-10s %-8s %-14s %-11s %-11s %8d  %s  first=%s' % rows[-1])

print('\n== 快照 mtime 之后被改写过（即当晚 TTL 过期重拉）的缓存 ==')
late = [r for r in rows if r[6] > snap_mtime]
for code, src, t, bars, clast, cbar, mt, first in late:
    print('  %-10s %-8s %-14s mtime=%s cache_bars=%d snap_bars=%s first=%s'
          % (code, src, t, mt, cbar, bars, first))
print('  共 %d 个' % len(late))
