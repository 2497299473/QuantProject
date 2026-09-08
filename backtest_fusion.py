#!/usr/bin/env python3
"""组合回测：实时涨跌估算 + 缠论穿透 + 三因子 → 加仓/减仓/不动 历史胜率裁决。

背景（2026-08-25 用户提出）：想用「持仓股票在指定两个时段的涨跌幅 + 缠论知识」
给出 加仓/减仓/不动 建议。本脚本先用数据裁决该组合的预测力，再决定是否实现。

诚实口径（重要，事先声明）：
1. 历史盘中 11:30/14:55 快照免费接口不可得 → 以【日线收盘】近似 14:55 时点
   （相差约 5 分钟，误差极小）；11:30 午盘时点无法历史回测，只能上线后积累样本
2. 防前视：t 日持仓 = 披露生效日 ≤ t 的最近季报快照（lookthrough.effective_snapshot）
3. 因果：个股指标（吻结构/背驰）逐日因果计算，只用 ≤t 数据
4. 缠论 = core/lookthrough 的吻结构 + MACD 背驰近似 composite（非全量形态学；
   全量缠论事件 POC 见 experiments/chanlun/backtest_leading.py，仅 2 股初筛）

判定标准（事先写死，跑完对照）：
A. est_chg 单信号：方向命中率 > 50% 且 pooled IC > 0
B. composite 单调：+1 桶超额 > 0 桶 > -1 桶
C. 组合决策单调：加仓桶超额 > 不动桶 > 减仓桶，且 加仓>0、减仓<0
D. 分段稳定：前后两半 C 的方向一致
A+B+C+D 全过 → 建议实现保守版「偏加/不动/偏减」参考档位；否则留在观察层。

用法：python3 backtest_fusion.py
"""
import json
import sys
from bisect import bisect_right
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from core import data_loader, lookthrough, stock_data
from core.signal_engine import macd_hist, factor_pool_rank_20d

FWD_LIST = (5, 10, 20)


def pearson(xs, ys) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy) if sx * sy else 0.0


def hit_rate(signs, fwds) -> float | None:
    pairs = [(s, f) for s, f in zip(signs, fwds) if s != 0]
    if not pairs:
        return None
    return sum(1 for s, f in pairs if (s > 0) == (f > 0)) / len(pairs) * 100


def main() -> int:
    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    fcfg = cfg["signal"]["factors"]
    lt_cfg = cfg["lookthrough"]
    wcfg = cfg["signal"]["weak_signal"]
    funds = cfg["fund_pool"]

    print("== [1] 拉取基金净值 + 历史持仓 ==")
    fund_data, stock_universe = {}, {}
    for code in funds:
        fund = data_loader.load_fund(code)
        history = lookthrough.holdings_history(code)
        for snap in history:
            for h in snap["holdings"]:
                stock_universe[h["code"]] = h["market"]
        fund_data[code] = (fund, history)
        print(f"  {code}: 净值 {len(fund['navs'])} 条（{fund['navs'][0][0]}~{fund['navs'][-1][0]}）"
              f"，持仓快照 {len(history)} 期")

    print(f"== [2] 拉取 {len(stock_universe)} 只个股K线（含缓存）==")
    series_map, close_map, fail = {}, {}, []
    for i, (scode, mkt) in enumerate(sorted(stock_universe.items()), 1):
        try:
            k = stock_data.fetch_stock_kline(scode, mkt)
            series_map[scode] = lookthrough.stock_signal_series(k["klines"], lt_cfg)
            # close_map: 每只股票 (dates, closes) 数组，用于算当日涨跌（停牌取最近一日）
            dates = [r[0] for r in k["klines"]]
            closes = [float(r[2]) for r in k["klines"]]
            close_map[scode] = (dates, closes)
        except Exception as e:
            fail.append(f"{scode}: {e}")
        if i % 20 == 0:
            print(f"  进度 {i}/{len(stock_universe)}")
    if fail:
        print(f"  [warn] {len(fail)} 只失败：{fail[:5]}")

    # 三因子逐日构建（复用 backtest_factors 的实现：f1 MACD柱 + f2 池内排名 + f3 距60日高）
    print("== [3] 构建三因子逐日序列 ==")
    per_fund_rows = {}
    for code, (fund, _h) in fund_data.items():
        navs = fund["navs"]
        hist_full = macd_hist([v for _, v in navs], fcfg["macd_hist_trend"]["ema_fast"],
                              fcfg["macd_hist_trend"]["ema_slow"], fcfg["macd_hist_trend"]["dea"])
        rows = {}
        for i in range(len(navs)):
            d, nav = navs[i]
            if i + 1 < wcfg["min_history"]:
                continue
            r20 = nav / navs[i - fcfg["pool_rank_20d"]["window"]][1] - 1 \
                if i >= fcfg["pool_rank_20d"]["window"] else None
            dd = nav / max(v for _, v in navs[i - fcfg["drawdown_from_60d_high"]["window"] + 1:i + 1]) - 1 \
                if i >= fcfg["drawdown_from_60d_high"]["window"] else None
            rows[d] = {"r20": r20, "macd": hist_full[i], "dd": dd}
        per_fund_rows[code] = rows
        print(f"  {code}: 三因子可用 {len(rows)} 天")

    print("== [4] 逐日重放（防前视）==")
    samples = []  # {fund, date, est_chg, composite, score, fwdN...}
    dd_cfg = fcfg["drawdown_from_60d_high"]
    all_dates = sorted({d for rows in per_fund_rows.values() for d in rows})
    by_fund_dates = {c: rows for c, rows in per_fund_rows.items()}
    for code, (fund, history) in fund_data.items():
        navs = fund["navs"]
        for i in range(len(navs)):
            d = navs[i][0]
            if d not in by_fund_dates[code]:
                continue
            snap = lookthrough.effective_snapshot(history, d)
            if not snap:
                continue
            agg = lookthrough.aggregate(snap, series_map, d, lt_cfg)
            if not agg:
                continue

            # est_chg：持仓个股最近日涨跌 × 权重（日线近似 14:55 时点）
            est_chg, wsum = 0.0, 0.0
            for h in snap["holdings"]:
                cm = close_map.get(h["code"])
                if not cm:
                    continue
                dates, closes = cm
                j = bisect_right(dates, d) - 1
                if j <= 0 or closes[j] <= 0:
                    continue
                chg = closes[j] / closes[j - 1] - 1 if closes[j - 1] > 0 else 0.0
                est_chg += h["pct"] * chg
                wsum += h["pct"]
            if wsum <= 0:
                continue
            est_chg /= wsum

            # 三因子总分（复用 backtest_factors 逻辑）
            r = by_fund_dates[code][d]
            f1 = 1 if r["macd"] > 0 else (-1 if r["macd"] < 0 else 0)
            pool_returns = {c: rows[d]["r20"] for c, rows in by_fund_dates.items()
                            if d in rows and rows[d]["r20"] is not None}
            f2 = factor_pool_rank_20d(pool_returns, code, fcfg["pool_rank_20d"])["score"] \
                if code in pool_returns else 0
            f3 = (1 if r["dd"] <= dd_cfg["pullback_threshold"]
                  else (-1 if r["dd"] >= dd_cfg["near_high_threshold"] else 0)) if r["dd"] is not None else 0
            score = f1 + f2 + f3

            row = {"fund": code, "date": d, "est_chg": est_chg,
                   "composite": agg["composite"], "score": score}
            for fwd in FWD_LIST:
                if i + fwd < len(navs):
                    row[f"fwd{fwd}"] = navs[i + fwd][1] / navs[i][1] - 1
            if all(f"fwd{f}" in row for f in FWD_LIST):
                samples.append(row)
        print(f"  {code}: 有效样本 {sum(1 for s in samples if s['fund'] == code)} 天")

    if not samples:
        print("[fail] 无样本"); return 1

    # ---- 超额基准：每基金自己的全体样本 fwd 均值 ----
    base = {}
    for c in funds:
        fwds = [s["fwd10"] for s in samples if s["fund"] == c]
        base[c] = sum(fwds) / len(fwds) if fwds else 0.0
    for s in samples:
        s["excess10"] = s["fwd10"] - base[s["fund"]]

    # ---- 组合决策（简单可解释）----
    for s in samples:
        if s["est_chg"] > 0 and s["composite"] >= 0 and s["score"] >= 0:
            s["decision"] = "加仓"
        elif s["est_chg"] < 0 and s["composite"] <= 0 and s["score"] <= 0:
            s["decision"] = "减仓"
        else:
            s["decision"] = "不动"

    pooled_by_date = sorted(samples, key=lambda s: s["date"])
    mid = pooled_by_date[len(pooled_by_date) // 2]["date"]

    def bucket_decision(rows, dec):
        sel = [s for s in rows if s["decision"] == dec]
        n = len(sel)
        if not n:
            return None
        return (n, sum(s["excess10"] for s in sel) / n * 100,
                sum(1 for s in sel if s["fwd10"] > 0) / n * 100)

    def bucket_comp(rows, c):
        sel = [s for s in rows if s["composite"] == c]
        n = len(sel)
        if not n:
            return None
        return (n, sum(s["excess10"] for s in sel) / n * 100,
                sum(1 for s in sel if s["fwd10"] > 0) / n * 100)

    # ---- 判定 ----
    est_hrs = {f: hit_rate([s["est_chg"] for s in samples],
                           [s[f"fwd{f}"] for s in samples]) for f in FWD_LIST}
    est_ic = pearson([s["est_chg"] for s in samples], [s["fwd10"] for s in samples])
    a_pass = (est_hrs[10] or 0) > 50 and est_ic > 0

    b1, b0, bm1 = bucket_comp(samples, 1), bucket_comp(samples, 0), bucket_comp(samples, -1)
    b_pass = b1 and b0 and bm1 and b1[1] > b0[1] > bm1[1]

    d_add, d_hold, d_cut = (bucket_decision(samples, x) for x in ("加仓", "不动", "减仓"))
    c_pass = d_add and d_hold and d_cut and d_add[1] > d_hold[1] > d_cut[1] \
        and d_add[1] > 0 and d_cut[1] < 0

    halves = {"前半": [s for s in samples if s["date"] < mid],
              "后半": [s for s in samples if s["date"] >= mid]}
    half_buckets = {name: {dec: bucket_decision(rows, dec) for dec in ("加仓", "不动", "减仓")}
                    for name, rows in halves.items()}
    d_pass = all(hb["加仓"] and hb["减仓"] and hb["加仓"][1] > hb["减仓"][1]
                 for hb in half_buckets.values())

    verdict = a_pass and b_pass and c_pass and d_pass

    verdict_line = ("✅ 通过 —— 建议实现保守版「偏加/不动/偏减」参考档位（措辞仍弱化，不构成指令）"
                    if verdict else
                    "❌ 未通过 —— 组合留在观察层，不输出操作档位；可先上线积累 11:30 盘中样本再复测")

    # ---- 报告 ----
    def fmt_b(b):
        return f"{b[0]} | {b[1]:+.2f}% | {b[2]:.0f}%" if b else "— | — | —"

    lines = [
        "# 组合回测报告：实时涨跌估算 + 缠论穿透 + 三因子 → 加仓/减仓/不动", "",
        f"> 生成：{datetime.now():%Y-%m-%d %H:%M} · 样本（基金日）{len(samples)} · "
        f"区间 {pooled_by_date[0]['date']} ~ {pooled_by_date[-1]['date']} · "
        f"基金 {len(funds)} 只 · 口径：超额=未来10日 - 基金全体样本均值", "",
        "## 〇、回测口径（诚实声明）", "",
        "1. **无历史盘中快照**：免费接口无 11:30/14:55 历史分时，以【日线收盘】近似 14:55 时点（差约 5 分钟）；**11:30 午盘时点无法历史回测**，需上线后积累样本",
        "2. 防前视：t 日持仓 = 披露生效日 ≤ t 的最近季报快照；个股指标因果计算",
        "3. 缠论 = 吻结构 + MACD 背驰近似 composite（-1/0/+1），非全量形态学",
        "",
        "## 一、单信号 A：实时涨跌估算 est_chg（日线近似）", "",
        "| 预测目标 | 方向命中率 | 样本 |", "|---|---:|---:|"]
    for f in FWD_LIST:
        lines.append(f"| 未来 {f} 日 | {est_hrs[f]:.1f}% | {len(samples)} |")
    lines += ["", f"est_chg vs fwd10 IC = {est_ic:+.3f}", "",
              f"**判定 A**（命中率>50% 且 IC>0）：{'✅' if a_pass else '❌'}", "",
              "## 二、缠论 composite 分桶（超额=未来10日-基准）", "",
              "| composite | 样本 | 超额 | 胜率 |", "|---:|---:|---:|---:|",
              f"| +1 | {fmt_b(b1)} |", f"| 0 | {fmt_b(b0)} |", f"| -1 | {fmt_b(bm1)} |", "",
              f"**判定 B**（+1 > 0 > -1 单调）：{'✅' if b_pass else '❌'}", "",
              "## 三、组合决策分桶（加仓=est>0&comp≥0&score≥0；减仓=对称）", "",
              "| 决策 | 样本 | 超额 | 胜率 |", "|---:|---:|---:|---:|",
              f"| 加仓 | {fmt_b(d_add)} |", f"| 不动 | {fmt_b(d_hold)} |", f"| 减仓 | {fmt_b(d_cut)} |", "",
              f"**判定 C**（加>不动>减 且 加>0、减<0）：{'✅' if c_pass else '❌'}", "",
              "## 四、时间分段（防时段依赖）", "",
              f"分段点：{mid}", "", "| 段 | 加仓超额 | 不动超额 | 减仓超额 |", "|---|---:|---:|---:|"]
    for name, hb in half_buckets.items():
        lines.append(f"| {name} | {fmt_b(hb['加仓'])} | {fmt_b(hb['不动'])} | {fmt_b(hb['减仓'])} |")
    lines += ["", f"**判定 D**（两半 加仓>减仓）：{'✅' if d_pass else '❌'}", "",
              "## 五、结论", "",
              f"**{verdict_line}**", "",
              "<sub>局限：①11:30 午盘时点未回测（无历史分时）；②持仓季报滞后 1~3 个月；"
              "③缠论为吻结构+背驰近似；④未含申赎费用；⑤前复权 K 线按当日口径回放（轻微幸存者偏差已知）。</sub>",
            ]
    report = "\n".join(lines)
    out = BASE_DIR / "output" / "backtest_fusion_report.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n[done] 报告 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
