#!/usr/bin/env python3
"""决定性探测（2026-08-23）：
1. 个股周线/月线 后复权(hfq) 能否修复前复权的负价失真（长历史缠论用）
2. 宽基/行业指数 周线可用性（代理候选）
"""
import requests

H = {"Referer": "https://finance.eastmoney.com/", "User-Agent": "Mozilla/5.0"}

def probe(symbol, label, fq):
    for period in ["week", "month"]:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},{period},,,1200,{fq}"
        try:
            r = requests.get(url, headers=H, timeout=20)
            d = r.json().get("data", {}).get(symbol, {})
            k = d.get(f"{fq}{period}") or d.get(period) or []
            if k:
                fc, lc = float(k[0][2]), float(k[-1][2])
                ok = "OK" if fc > 0 else "!! <=0"
                print(f"  {label} {period:5s}[{fq}] N={len(k):4d} {k[0][0]}~{k[-1][0]} 首收={fc:.2f} 末收={lc:.2f} {ok}")
            else:
                print(f"  {label} {period:5s}[{fq}] N=0 keys={list(d.keys())}")
        except Exception as e:
            print(f"  {label} {period:5s}[{fq}] ERR {type(e).__name__}: {str(e)[:60]}")

print("===== 个股 后复权(hfq) 修复测试 =====")
probe("sz300308", "中际旭创", "hfq")
probe("sz300502", "新易盛", "hfq")

print("\n===== 宽基/行业 周线候选 =====")
for sym, lab, fq in [
    ("sz399006", "创业板指", "qfq"),
    ("sh000300", "沪深300", "qfq"),
    ("sh000905", "中证500", "qfq"),
    ("sz399673", "创业板50", "qfq"),
    ("sh515880", "通信ETF", "qfq"),
]:
    probe(sym, lab, fq)
