#!/usr/bin/env python3
"""行业代理质量验证（2026-08-23）
通信ETF(515880, 最贴近光模块/CPO 的行业代理) vs 002112 日收益分时段相关性
决定「行业指数版」是否值得跑的前置闸门
"""
import json
import requests
from pathlib import Path

ROOT = Path("/home/summer/quant_test")
H = {"Referer": "https://finance.eastmoney.com/", "User-Agent": "Mozilla/5.0"}

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
    mf, mp = sum(fr) / n, sum(pr) / n
    num = sum((a - mf) * (b - mp) for a, b in zip(fr, pr))
    den = (sum((a - mf) ** 2 for a in fr) * sum((b - mp) ** 2 for b in pr)) ** 0.5
    return (num / den, n) if den else (None, n)

comm = fetch_daily("sh515880", "通信ETF")

periods = [
    ("2019-09-01", "2021-12-31", "2019-2021"),
    ("2022-01-01", "2023-12-31", "2022-2023"),
    ("2024-01-01", "2026-12-31", "2024-2026"),
    ("2019-09-01", "2026-12-31", "全程"),
]
print("\n通信ETF vs 002112 分时段日收益相关性")
print(f"{'时段':<14}{'r':>10}{'N':>8}")
for d0, d1, lab in periods:
    r, n = pearson_ret(comm, d0, d1)
    s = "  --" if r is None else f"{r:+.3f}"
    print(f"{lab:<14}{s:>10}{n:>8}")
