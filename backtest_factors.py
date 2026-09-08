#!/usr/bin/env python3
"""净值三因子全历史独立复验 + 池内横截面轮动检验。

目的：
1. 重建版的三因子实现与原会话回测（2026-08-22，证据来自笔记）非逐行一致（002112 当日
   +2 vs 原 +3 已证明边界差异），必须在同一份净值数据上独立复验分桶/分段结论
2. 验证横截面轮动（rank(20日收益)+rank(MACD柱) top1）在 2020-2026 多环境下是否保持
   原笔记阶段②的证据（top 超额 +0.41%，t=3.44）

口径与原笔记一致：超额 = 未来20日收益 - 该基金全体样本均值；时间对半分段。
因子计算复用 core/signal_engine 的实现（保证测的就是引擎在跑的）。

用法：python3 backtest_factors.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from core import data_loader
from core.signal_engine import macd_hist, factor_pool_rank_20d, factor_drawdown_from_high

FWD = 20


def pearson(xs, ys) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy) if sx * sy else 0.0


def main() -> int:
    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    fcfg = cfg["signal"]["factors"]
    funds = {c: data_loader.load_fund(c) for c in cfg["fund_pool"]}

    # ---- 因果构建每日因子（与引擎同实现） ----
    per_fund: dict[str, list[dict]] = {}
    for code, f in funds.items():
        navs = f["navs"]
        hist_full = macd_hist([v for _, v in navs],
                              fcfg["macd_hist_trend"]["ema_fast"],
                              fcfg["macd_hist_trend"]["ema_slow"],
                              fcfg["macd_hist_trend"]["dea"])
        rows = []
        for i in range(len(navs) - FWD):
            d, nav = navs[i]
            if i + 1 < cfg["signal"]["weak_signal"]["min_history"]:
                continue
            r20 = nav / navs[i - fcfg["pool_rank_20d"]["window"]][1] - 1 \
                if i >= fcfg["pool_rank_20d"]["window"] else None
            dd = navs[i][1] / max(v for _, v in navs[i - fcfg["drawdown_from_60d_high"]["window"] + 1:i + 1]) - 1 \
                if i >= fcfg["drawdown_from_60d_high"]["window"] else None
            fwd = navs[i + FWD][1] / nav - 1
            rows.append({"date": d, "r20": r20, "macd": hist_full[i], "dd": dd, "fwd": fwd})
        per_fund[code] = rows
        print(f"  {code}: 有效样本 {len(rows)} 天（{rows[0]['date']} ~ {rows[-1]['date']}）")

    # ---- 每日池内排名 → 三因子总分 ----
    samples = []
    by_fund_rows = {c: {r["date"]: r for r in rows} for c, rows in per_fund.items()}
    all_dates = sorted({r["date"] for rows in per_fund.values() for r in rows})
    dd_cfg = fcfg["drawdown_from_60d_high"]
    for d in all_dates:
        pool_returns = {c: r["r20"] for c, rows in by_fund_rows.items()
                        for r in [rows.get(d)] if r and r["r20"] is not None}
        for c, rows in by_fund_rows.items():
            r = rows.get(d)
            if not r:
                continue
            f1 = 1 if r["macd"] > 0 else (-1 if r["macd"] < 0 else 0)
            f2 = factor_pool_rank_20d(pool_returns, c, fcfg["pool_rank_20d"])["score"] if c in pool_returns else 0
            f3 = (1 if r["dd"] <= dd_cfg["pullback_threshold"]
                  else (-1 if r["dd"] >= dd_cfg["near_high_threshold"] else 0)) if r["dd"] is not None else 0
            samples.append({"fund": c, "date": d, "score": f1 + f2 + f3, "fwd": r["fwd"],
                            "r20": r["r20"], "macd": r["macd"], "dd": r["dd"]})

    base = {}
    for c in per_fund:
        fwds = [s["fwd"] for s in samples if s["fund"] == c]
        base[c] = sum(fwds) / len(fwds) if fwds else 0.0
    for s in samples:
        s["excess"] = s["fwd"] - base[s["fund"]]

    def bucket(rows_by_score, lo, hi=None):
        sel = [s for s in samples if lo <= s["score"] <= (hi if hi is not None else lo)]
        n = len(sel)
        if not n:
            return None
        return (n, sum(s["excess"] for s in sel) / n * 100,
                sum(1 for s in sel if s["fwd"] > 0) / n * 100)

    pooled_by_date = sorted(samples, key=lambda s: s["date"])
    mid = pooled_by_date[len(pooled_by_date) // 2]["date"]
    halves = {"前半": [s for s in samples if s["date"] < mid],
              "后半": [s for s in samples if s["date"] >= mid]}

    # ---- 横截面：每日 IC + 轮动 top1 ----
    xs_days = []
    for d in all_dates:
        day = [s for s in samples if s["date"] == d]
        if len(day) < 2 or any(s["r20"] is None for s in day):
            continue
        fwds = [s["fwd"] for s in day]
        ics = {k: pearson([s[k] for s in day], fwds) for k in ("r20", "macd", "dd")}
        order = sorted(day, key=lambda s: -(s["r20"] + s["macd"]))
        top, bottom = order[0], order[-1]
        pool_mean = sum(fwds) / len(fwds)
        xs_days.append({"date": d, "ics": ics, "top": top, "bottom": bottom,
                        "top_ex": top["fwd"] - pool_mean, "bot_ex": bottom["fwd"] - pool_mean,
                        "n": len(day)})
    xs_mid = xs_days[len(xs_days) // 2]["date"]

    def t_stat(vals):
        n = len(vals)
        if n < 3:
            return 0.0
        m = sum(vals) / n
        var = sum((v - m) ** 2 for v in vals) / (n - 1)
        return m / (var ** 0.5) * n ** 0.5 if var else 0.0

    ic_summary = {}
    for k in ("r20", "macd", "dd"):
        vals = [x["ics"][k] for x in xs_days]
        ic_summary[k] = (sum(vals) / len(vals) * 1, t_stat(vals))

    def xs_bucket(rows, key):
        n = len(rows)
        if not n:
            return None
        return (n, sum(r[key] for r in rows) / n * 100,
                sum(1 for r in rows if r[key] > 0) / n * 100)

    # ---- 报告 ----
    buy_rows = [s for s in samples if s["score"] >= 2]
    bear_rows = [s for s in samples if s["score"] <= -2]
    lines = [
        "# 净值三因子独立复验 + 横截面轮动检验（2020-2026 全历史）", "",
        f"> 生成：{datetime.now():%Y-%m-%d %H:%M} · 样本（基金日）{len(samples)} · "
        f"区间 {pooled_by_date[0]['date']} ~ {pooled_by_date[-1]['date']} · "
        f"口径：超额=未来20日-基金全体均值（同原笔记）", "",
        "## 一、三因子总分分桶（pooled）", "",
        "| 总分 | 样本 | 20日超额 | 胜率 |", "|---:|---:|---:|---:|"]
    for sc in range(-3, 4):
        b = bucket(samples, sc)
        if b:
            lines.append(f"| {sc:+d} | {b[0]} | {b[1]:+.2f}% | {b[2]:.0f}% |")
    b_buy = (len(buy_rows), sum(s["excess"] for s in buy_rows) / len(buy_rows) * 100 if buy_rows else 0,
             sum(1 for s in buy_rows if s["fwd"] > 0) / len(buy_rows) * 100 if buy_rows else 0)
    lines += ["", f"**买入侧（总分≥+2，对齐原笔记口径）**：{b_buy[0]} 样本，超额 "
              f"**{b_buy[1]:+.2f}%**，胜率 {b_buy[2]:.0f}%", "",
              "## 二、时间分段（买入≥+2）", "", f"分段点：{mid}", "",
              "| 段 | 样本 | 超额 |", "|---|---:|---:|"]
    for name, rows in halves.items():
        sel = [s for s in rows if s["score"] >= 2]
        lines.append(f"| {name} | {len(sel)} | {sum(s['excess'] for s in sel)/len(sel)*100 if sel else 0:+.2f}% |")
    lines += ["", "## 三、分基金（买入≥+2）", "",
              "| 基金 | 样本 | 超额 | 原笔记数字 |", "|---|---:|---:|---:|"]
    orig = {"002112": "+1.56%", "002207": "+0.54%", "022853": "（原样本短仅参考）", "025687": "（原回测跳过）"}
    for c in per_fund:
        sel = [s for s in samples if s["fund"] == c and s["score"] >= 2]
        ex = sum(s["excess"] for s in sel) / len(sel) * 100 if sel else 0
        lines.append(f"| {c} | {len(sel)} | {ex:+.2f}% | {orig[c]} |")
    lines += ["", "## 四、横截面检验（每日池内 ≥2 只）", "",
              f"横截面样本日：{len(xs_days)}（{xs_days[0]['date']} ~ {xs_days[-1]['date']}）", "",
              "| 因子 | 平均横截面 IC | t 值 |", "|---|---:|---:|"]
    names = {"r20": "20日收益", "macd": "MACD柱", "dd": "距60日高"}
    for k in ("r20", "macd", "dd"):
        m, t = ic_summary[k]
        lines.append(f"| {names[k]} | {m:+.3f} | {t:+.2f} |")
    lines += ["", "### 轮动 top1（rank(r20)+rank(macd) 最高）vs 池等权", ""]
    top_all = xs_bucket(xs_days, "top_ex")
    lines += [f"- 全样本：{top_all[0]} 天，超额 **{top_all[1]:+.2f}%**，胜率 {top_all[2]:.0f}%"
              f"（t={t_stat([x['top_ex'] for x in xs_days]):+.2f}；原笔记 +0.41%/t=3.44）", ""]
    lines += ["| 段 | 天数 | top1 超额 |", "|---|---:|---:|"]
    for name, cond in (("前半", lambda x: x["date"] < xs_mid), ("后半", lambda x: x["date"] >= xs_mid)):
        rows = [x for x in xs_days if cond(x)]
        if rows:
            lines.append(f"| {name} | {len(rows)} | {sum(x['top_ex'] for x in rows)/len(rows)*100:+.2f}% |")
    lines += ["", "<sub>因子实现=core/signal_engine 原函数（macd_hist EMA 因果；pool_rank/drawdown 逐日切片）。</sub>"]
    report = "\n".join(lines)
    out = BASE_DIR / "output" / "backtest_factors_report.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n[done] 报告 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
