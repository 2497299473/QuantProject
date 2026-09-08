#!/usr/bin/env python3
"""行业指数周线相关性探测（2026-08-23）
腾讯日K仅640根(2023-12起)→改周线突破长度限制
候选：通信/半导体/芯片/人工智能ETF + 创业板指
周K收盘(周五) vs 基金同日净值，算周收益分时段相关性
决定「行业指数版」是否值得跑的最后拼图
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

def fetch_week(symbol, name):
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},week,,,800,qfq"
    r = requests.get(url, headers=H, timeout=20)
    d = r.json()["data"][symbol]
    k = d.get("qfqweek") or d.get("week") or []
    print(f"  {name} {symbol}: {len(k)} 周 ({k[0][0]} ~ {k[-1][0]})")
    return {row[0]: float(row[2]) for row in k}  # 周末收盘

def pearson_week(proxy_week, d0, d1):
    """周K收盘(周五) vs 基金同日净值，周收益相关性"""
    dates = sorted(set(fund) & set(proxy_week))
    dates = [d for d in dates if d0 <= d <= d1]
    fr, pr = [], []
    for i in range(1, len(dates)):
        f0, f1 = fund[dates[i - 1]], fund[dates[i]]
        p0, p1 = proxy_week[dates[i - 1]], proxy_week[dates[i]]
        fr.append((f1 - f0) / f0)
        pr.append((p1 - p0) / p0)
    if len(fr) < 30:
        return None, len(fr)
    n = len(fr)
    mf, mp = sum(fr) / n, sum(pr) / n
    num = sum((a - mf) * (b - mp) for a, b in zip(fr, pr))
    den = (sum((a - mf) ** 2 for a in fr) * sum((b - mp) ** 2 for b in pr)) ** 0.5
    return (num / den, n) if den else (None, n)

candidates = [
    ("sh515880", "通信ETF"),
    ("sh512480", "半导体ETF"),
    ("sz159995", "芯片ETF"),
    ("sh515070", "人工智能ETF"),
    ("sz399006", "创业板指"),
]
periods = [
    ("2016-01-01", "2020-12-31", "2016-2020"),
    ("2021-01-01", "2023-12-31", "2021-2023"),
    ("2024-01-01", "2026-12-31", "2024-2026"),
]
print("\n周收益相关性（代理周K vs 基金净值）")
print(f"{'指数':<14}{'2016-2020':>14}{'2021-2023':>14}{'2024-2026':>14}")
for sym, lab in candidates:
    wk = fetch_week(sym, lab)
    cells = []
    for d0, d1, _ in periods:
        r, n = pearson_week(wk, d0, d1)
        cells.append(f"{r:+.3f}({n})" if r is not None else "   --")
    print(f"{lab:<14}{cells[0]:>14}{cells[1]:>14}{cells[2]:>14}")
