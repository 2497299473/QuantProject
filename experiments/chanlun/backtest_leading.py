#!/usr/bin/env python3
"""缠论结构信号 vs 基金净值 领先性回测（B方案 Phase 2，v2 修复事件对齐）

与 v1 差异：
  - 买卖点事件不再「当日匹配」，改为：walk-forward 滚动窗口内产生的事件按事件日期
    去重收集，再以「≥事件日期的最近净值日」为起点统计前向净值收益。
  - 结构状态分组 / 双股共振 / 相关性逻辑不变。

局限（同 v1）：持仓滞后未建模；线段/中枢/背驰为 POC 简化规则；未含申赎成本。
本脚本是领先性初筛，非完整策略回测。
"""
import json
import os
import sys
from bisect import bisect_left
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chanlun_engine import (
    fetch_kline, merge_klines, find_fractals, build_strokes,
    build_zhongshu, calc_macd, find_buy_sell_points,
)

ROLLING = 300
HORIZONS = (5, 10, 20)
STOCK_CODES = ["300308", "300502"]
STOCK_NAMES = {"300308": "中际旭创", "300502": "新易盛"}
FUND_CODE = "002112"
PROJ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NAV_PATH = os.path.join(PROJ, "data", "klines", FUND_CODE + ".json")


def load_nav():
    with open(NAV_PATH, "r", encoding="utf-8") as f:
        d = json.load(f)
    by_date = {}
    for it in d["nav_list"]:
        by_date[it["date"]] = it.get("growth_pct", 0.0)
    return by_date


def fwd_ret(nav_dates, nav_growth, t_date, horizon):
    """以 ≥t_date 的最近净值日为起点，horizon 个净值日后复合收益；不足返回 None"""
    i0 = bisect_left(nav_dates, t_date)
    if i0 >= len(nav_dates):
        return None
    i1 = i0 + horizon
    if i1 >= len(nav_dates):
        return None
    ret = 1.0
    for j in range(i0 + 1, i1 + 1):
        ret *= 1.0 + nav_growth[nav_dates[j]] / 100.0
    return ret - 1.0


def structure_state(win):
    """返回 (state, 窗口内事件列表[(date,type)...])"""
    merged = merge_klines(win)
    fracs = find_fractals(merged)
    strokes = build_strokes(fracs)
    zs = build_zhongshu(strokes)
    state = 0
    events = []
    if zs:
        last = zs[-1]
        close = win[-1]["close"]
        state = 1 if close > last.ZG else (-1 if close < last.ZD else 0)
        dif, dea, hist = calc_macd(win)
        pts = find_buy_sell_points(strokes, zs, klines=win, hist=hist)
        # 只算「窗口内较新确认」的事件（事件日距窗口末端 ≤15 个交易日），
        # 避免同一事件在后续几十个窗口被重复观测
        t_date = win[-1]["date"]
        tail = {k["date"] for k in win[-15:]}
        events = [(p.date, p.type) for p in pts if p.date in tail]
    return state, events


def main():
    nav_growth = load_nav()
    nav_dates = sorted(nav_growth.keys())
    print(f"净值数据: {FUND_CODE} {len(nav_dates)} 条 ({nav_dates[0]} ~ {nav_dates[-1]})")

    samples = []          # (code, t_date, state)
    event_seen = set()    # (code, date, type) 去重
    event_list = []       # (date, type)
    for code in STOCK_CODES:
        klines = fetch_kline(code, datalen=ROLLING + 700)
        print(f"K线: {code} {STOCK_NAMES[code]} {len(klines)} 根 "
              f"({klines[0]['date']} ~ {klines[-1]['date']})")
        for idx in range(ROLLING, len(klines)):
            t_date = klines[idx]["date"]
            if t_date not in nav_growth:
                continue
            win = klines[idx - ROLLING + 1: idx + 1]
            state, events = structure_state(win)
            samples.append((code, t_date, state))
            for d, e in events:
                key = (code, d, e)
                if key not in event_seen:
                    event_seen.add(key)
                    event_list.append((d, e))
    print(f"回测样本: {len(samples)} 个交易日状态观测 | 事件: {len(event_list)} 个\n")

    stat = defaultdict(lambda: defaultdict(list))
    for code, t_date, state in samples:
        for h in HORIZONS:
            r = fwd_ret(nav_dates, nav_growth, t_date, h)
            if r is not None:
                stat[h][state].append(r)

    print("=== [1] 结构状态分组（跨股票合并，均值%/胜率%） ===")
    labels = {1: "上方>ZG", 0: "内部", -1: "下方<ZD"}
    print(f"{'状态':10s} | {'fwd5 均/胜':>14s} | {'fwd10 均/胜':>15s} | {'fwd20 均/胜':>15s}")
    for state in (1, 0, -1):
        parts = [f"{labels[state]:8s}"]
        for h in HORIZONS:
            rs = stat[h].get(state, [])
            if rs:
                avg = sum(rs) / len(rs) * 100
                win = sum(1 for r in rs if r > 0) / len(rs) * 100
                parts.append(f"{avg:+.2f}%/{win:.0f}%")
            else:
                parts.append("--/--")
        print(" | ".join(parts))
    parts = ["基准    "]
    for h in HORIZONS:
        rs = [r for s in stat[h].values() for r in s]
        if rs:
            avg = sum(rs) / len(rs) * 100
            win = sum(1 for r in rs if r > 0) / len(rs) * 100
            parts.append(f"{avg:+.2f}%/{win:.0f}%")
    print(" | ".join(parts))

    print("\n=== [2] 买卖点事件后前向收益（按事件日起算，均值%/胜率%） ===")
    evt = defaultdict(list)
    for d, e in event_list:
        evt[e].append(d)
    for e in ("1买", "2买", "3买", "1卖", "2卖", "3卖"):
        row = [f"{e:4s}"]
        for h in HORIZONS:
            rs = [fwd_ret(nav_dates, nav_growth, d, h) for d in evt.get(e, [])]
            rs = [r for r in rs if r is not None]
            if rs:
                avg = sum(rs) / len(rs) * 100
                win = sum(1 for r in rs if r > 0) / len(rs) * 100
                row.append(f"{avg:+.2f}/{win:.0f}%")
            else:
                row.append("  --")
        row.append(f"N={len(evt.get(e, []))}")
        print(" | ".join(row))

    by_date = defaultdict(list)
    for code, t_date, state in samples:
        by_date[t_date].append(state)
    comp = [(d, sum(v)) for d, v in sorted(by_date.items()) if len(v) == 2]
    print("\n=== [3] 双股复合状态（共振，两股状态和） ===")
    print(f"{'状态':14s} | {'fwd5':>9s} | {'fwd10':>9s} | {'fwd20':>9s} | N")
    for tag, cond in (("共振偏多(和>0)", lambda s: s > 0),
                      ("中性(和=0)", lambda s: s == 0),
                      ("共振偏空(和<0)", lambda s: s < 0)):
        row = [f"{tag:12s}"]
        ns = 0
        for h in HORIZONS:
            rs = [fwd_ret(nav_dates, nav_growth, d, h) for d, s in comp if cond(s)]
            rs = [r for r in rs if r is not None]
            ns = len(rs) if h == 5 else ns
            row.append(f"{sum(rs)/len(rs)*100:+.2f}" if rs else "  --")
        row.append(str(ns))
        print(" | ".join(row))

    print("\n=== [4] 相关性 state vs fwdN（Pearson） ===")
    for h in HORIZONS:
        pairs = []
        for code, t_date, state in samples:
            r = fwd_ret(nav_dates, nav_growth, t_date, h)
            if r is not None:
                pairs.append((state, r))
        if len(pairs) > 2:
            xs = [p[0] for p in pairs]
            ys = [p[1] for p in pairs]
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            cov = sum((x - mx) * (y - my) for x, y in pairs)
            vx = sum((x - mx) ** 2 for x in xs)
            vy = sum((y - my) ** 2 for y in ys)
            r = cov / (vx * vy) ** 0.5 if vx * vy > 0 else 0.0
            print(f"  fwd{h}: r={r:+.3f} (N={len(pairs)})")

    print("\n[注] ①持仓滞后未建模 ②线段/背驰为简化规则 ③未含申赎成本。初筛结果，非策略回测。")


if __name__ == "__main__":
    main()
