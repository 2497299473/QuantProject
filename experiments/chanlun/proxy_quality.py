#!/usr/bin/env python3
"""验证代理标的 vs 002112 的日收益相关性（分时段）
标的：创业板指(399006) 宽基对照 + 重仓股等权(300308+300502) 持仓映射对照
目的：判断「指数/个股代理K线」的映射质量，为周线扩展回测提供入场券（2026-08-23）
"""
import json
import requests
from pathlib import Path

ROOT = Path("/home/summer/quant_test")
H = {"Referer": "https://finance.eastmoney.com/", "User-Agent": "Mozilla/5.0"}

# 1. 基金净值
with open(ROOT / "data/klines/002112.json", encoding="utf-8") as f:
    nav_list = json.load(f)["nav_list"]
fund = {x["date"]: x["nav"] for x in nav_list}
print(f"002112 净值: {len(nav_list)} 条 ({nav_list[0]['date']} ~ {nav_list[-1]['date']})")

def fetch_daily(symbol, name):
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,1200,qfq"
    r = requests.get(url, headers=H, timeout=20)
    d = r.json()["data"][symbol]
    k = d.get("qfqday") or d.get("day") or []
    print(f"  {name} {symbol}: {len(k)} 根 ({k[0][0]} ~ {k[-1][0]})")
    return {row[0]: float(row[2]) for row in k}  # date -> close

def pearson_ret(proxy_close, d0, d1):
    """对齐交易日，算日收益 Pearson r（净值 close≈nav）"""
    dates = sorted(set(fund) & set(proxy_close))
    dates = [d for d in dates if d0 <= d <= d1]
    fr, pr = [], []
    for i in range(1, len(dates)):
        f0, f1 = fund[dates[i - 1]], fund[dates[i]]
        p0, p1 = proxy_close[dates[i - 1]], proxy_close[dates[i]]
        fr.append((f1 - f0) / f0)
        pr.append((p1 - p0) / p0)
    if len(fr) < 30:
        return None, len(fr)
    n = len(fr)
    mf = sum(fr) / n
    mp = sum(pr) / n
    num = sum((a - mf) * (b - mp) for a, b in zip(fr, pr))
    den = (sum((a - mf) ** 2 for a in fr) * sum((b - mp) ** 2 for b in pr)) ** 0.5
    return (num / den, n) if den else (None, n)

cyb = fetch_daily("sz399006", "创业板指")
zx = fetch_daily("sz300308", "中际旭创")
xy = fetch_daily("sz300502", "新易盛")
all_dates = sorted(set(zx) & set(xy))
equal = {d: (zx[d] + xy[d]) / 2 for d in all_dates}  # 重仓等权合成

periods = [
    ("2016-01-01", "2020-12-31"),
    ("2021-01-01", "2023-12-31"),
    ("2024-01-01", "2026-12-31"),
    ("2016-01-01", "2026-12-31"),
]
print("\n时段相关性（日收益 Pearson r / 样本数）")
print(f"{'时段':<26}{'创业板指':>16}{'重仓等权':>16}")
for d0, d1 in periods:
    r1, n1 = pearson_ret(cyb, d0, d1)
    r2, n2 = pearson_ret(equal, d0, d1)
    s1 = "  --" if r1 is None else f"{r1:+.3f}({n1})"
    s2 = "  --" if r2 is None else f"{r2:+.3f}({n2})"
    print(f"{d0 + ' ~ ' + d1:<26}{s1:>16}{s2:>16}")
