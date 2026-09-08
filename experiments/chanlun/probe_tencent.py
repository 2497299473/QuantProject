#!/usr/bin/env python3
"""探测腾讯 gtimg 不同周期 × 前复权的数据深度与价格合理性
目的：验证「周线跑缠论覆盖10年牛熊」是否可行（2026-08-23）
"""
import requests

H = {'Referer': 'https://finance.eastmoney.com/', 'User-Agent': 'Mozilla/5.0'}

def probe(symbol, label):
    print(f"===== {label} ({symbol}) =====")
    for period in ['day', 'week', 'month']:
        url = f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},{period},,,1200,qfq'
        try:
            r = requests.get(url, headers=H, timeout=20)
            d = r.json().get('data', {}).get(symbol, {})
            k = d.get(f'qfq{period}') or d.get(period) or []
            if k:
                # 复权合理性检查：首根收盘价是否为正且数量级合理
                first_close = float(k[0][2])
                last_close = float(k[-1][2])
                print(f"  {period:5s} N={len(k):4d}  {k[0][0]} ~ {k[-1][0]}  "
                      f"首收={first_close:.2f} 末收={last_close:.2f}  "
                      f"{'OK' if first_close > 0 else '!! 复权失真(<=0)'}")
            else:
                print(f"  {period:5s} N=0  keys={list(d.keys())}")
        except Exception as e:
            print(f"  {period:5s} ERR {type(e).__name__}: {str(e)[:70]}")
    print()

# 中际旭创(002112第一重仓) + 新易盛(第二重仓) + 创业板指(300xxx宽基代理)
probe('sz300308', '中际旭创')
probe('sz300502', '新易盛')
probe('sz399006', '创业板指')
