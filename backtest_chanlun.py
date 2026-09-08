#!/usr/bin/env python3
"""缠论买卖点事件回测（裁决全量形态学事件是否携带可用的基金净值信息量）。

信号构造（与报告「缠论事件(30日)」同口径）：
- t 日对基金持仓（防前视快照）内每只个股，取 confirm_date ∈ (t-30天, t] 的
  买卖点事件：一/二/三类买 = +权重，一/二/三类卖 = -权重
- 净事件 net = Σ±权重 / Σ有效权重；net > 0.15 → +1，net < -0.15 → -1，否则 0
- 目标：基金净值未来 20 日收益，超额 = 收益 - 该基金全体样本均值（口径同三因子/穿透回测）

判定标准（与穿透回测一致，事先写死）：pooled +1 超额 > +0.3% 且 > 0 桶；
前后两半 +1 超额均 > 0；≥2 只基金单独 +1 超额 > 0。

用法：python3 backtest_chanlun.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from core import chanlun, data_loader, lookthrough, stock_data
from backtest_lookthrough import bucket_stats, pearson

FWD = 20
WINDOW_DAYS = 30
BUY = {"1buy", "2buy", "3buy"}
SELL = {"1sell", "2sell", "3sell"}


def main() -> int:
    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    funds = cfg["fund_pool"]

    print("== 净值 / 历史持仓 / 个股K线（含缓存）==")
    fund_data, stock_universe = {}, {}
    for code in funds:
        fund = data_loader.load_fund(code)
        history = lookthrough.holdings_history(code)
        for snap in history:
            for h in snap["holdings"]:
                stock_universe[h["code"]] = h["market"]
        fund_data[code] = (fund, history)

    events_map: dict[str, list[dict]] = {}
    for i, (scode, mkt) in enumerate(sorted(stock_universe.items()), 1):
        try:
            k = stock_data.fetch_stock_kline(scode, mkt)
            r = chanlun.analyze(k["klines"])
            events_map[scode] = r["events"]
        except Exception:
            events_map[scode] = []
        if i % 40 == 0:
            print(f"  形态学进度 {i}/{len(stock_universe)}")
    n_ev = sum(len(v) for v in events_map.values())
    print(f"  {len(stock_universe)} 只个股，共 {n_ev} 个买卖点事件")

    def _minus_days(d: str, days: int) -> str:
        t = datetime.strptime(d, "%Y-%m-%d")
        from datetime import timedelta
        return (t - timedelta(days=days)).strftime("%Y-%m-%d")

    print("== 逐日重放（防前视持仓 + 事件确认日过滤）==")
    per_fund = {}
    for code, (fund, history) in fund_data.items():
        navs = fund["navs"]
        rets_all = [navs[i + FWD][1] / navs[i][1] - 1 for i in range(len(navs) - FWD)]
        base = sum(rets_all) / len(rets_all) if rets_all else 0.0
        samples = []
        for i in range(len(navs) - FWD):
            d, nav = navs[i]
            snap = lookthrough.effective_snapshot(history, d)
            if not snap:
                continue
            wsum, net = 0.0, 0.0
            for h in snap["holdings"]:
                evs = events_map.get(h["code"], [])
                if not evs:
                    continue
                wsum += h["pct"]
                cutoff = _minus_days(d, WINDOW_DAYS)
                for e in evs:
                    if cutoff < e["confirm_date"] <= d:
                        net += h["pct"] * (1 if e["type"] in BUY else -1)
            if wsum == 0:
                continue
            net /= wsum
            signal = 1 if net > 0.15 else (-1 if net < -0.15 else 0)
            fwd = navs[i + FWD][1] / nav - 1
            samples.append({"date": d, "composite": signal, "net": net,
                            "fwd": fwd, "excess": fwd - base})
        per_fund[code] = {"samples": samples, "base20": base * 100}
        n1 = sum(1 for s in samples if s["composite"] == 1)
        print(f"  {code}: 有效样本 {len(samples)} 天，其中信号+1 {n1} 天，基准20日 {base*100:+.2f}%")

    pooled = [s for code in funds for s in per_fund[code]["samples"]]
    if not pooled:
        print("[fail] 无样本"); return 1
    pooled_by_date = sorted(pooled, key=lambda s: s["date"])
    mid = pooled_by_date[len(pooled_by_date) // 2]["date"]
    halves = {"前半": [s for s in pooled if s["date"] < mid],
              "后半": [s for s in pooled if s["date"] >= mid]}
    ic = pearson([s["net"] for s in pooled], [s["fwd"] for s in pooled])

    pooled_b = bucket_stats(pooled)
    fund_b = {code: bucket_stats(v["samples"]) for code, v in per_fund.items()}
    half_b = {name: bucket_stats(rows) for name, rows in halves.items()}

    c1 = pooled_b[1]["mean_excess"] is not None and pooled_b[1]["mean_excess"] > 0.3 \
        and pooled_b[1]["mean_excess"] > (pooled_b[0]["mean_excess"] or 0)
    c2 = all(hb[1]["mean_excess"] is not None and hb[1]["mean_excess"] > 0 for hb in half_b.values())
    c3 = sum(1 for code in funds
             if fund_b[code][1]["mean_excess"] is not None and fund_b[code][1]["mean_excess"] > 0) >= 2
    verdict = c1 and c2 and c3

    def fmt_b(b):
        return "\n".join(
            f"| {c:+d} | {b[c]['n']} | {b[c]['mean_excess']:+.2f}% | {b[c]['win']:.0f}% |"
            if b[c]["n"] else f"| {c:+d} | 0 | — | — |" for c in (-1, 0, 1))

    verdict_text = ("✅ 通过 —— 缠论事件具备信息量，可作为独立观察信号保留，"
                    "是否进引擎见 README 记录" if verdict else
                    "❌ 未通过 —— 全量形态学事件仅保留在报告观察层（缠论事件栏），不进打分")
    lines = [
        "# 缠论买卖点事件回测（全量形态学 → 基金净值未来20日）", "",
        f"> 生成：{datetime.now():%Y-%m-%d %H:%M} · 样本（基金日）{len(pooled)} · "
        f"回放区间 {pooled_by_date[0]['date']} ~ {pooled_by_date[-1]['date']} · "
        f"共 {n_ev} 个事件（30 日窗口·按披露权重聚合）", "",
        "## 一、pooled 全样本分桶", "",
        "| 事件信号 | 样本 | 20日超额 | 胜率 |", "|---:|---:|---:|---:|",
        fmt_b(pooled_b), "",
        f"连续净事件 IC = {ic:+.3f}", "",
        "## 二、时间分段", "",
        f"分段点：{mid}", "",
        "| 段 | 信号=+1 样本 | +1 超额 |", "|---|---:|---:|"]
    for name, hb in half_b.items():
        b1 = hb[1]
        lines.append(f"| {name} | {b1['n']} | {b1['mean_excess']:+.2f}% |" if b1["n"]
                     else f"| {name} | 0 | — |")
    lines += ["", "## 三、分基金", "",
              "| 基金 | 有效样本 | 基准20日 | +1样本 | +1超额 |", "|---|---:|---:|---:|---:|"]
    for code in funds:
        v, b = per_fund[code], fund_b[code]
        p1 = f"{b[1]['mean_excess']:+.2f}%" if b[1]["n"] else "—"
        lines.append(f"| {code} | {len(v['samples'])} | {v['base20']:+.2f}% | {b[1]['n']} | {p1} |")
    lines += ["", "## 四、判定（与穿透回测同一标准）", "",
              f"1. pooled +1 超额 > +0.3% 且 > 0桶：{'✅' if c1 else '❌'}"
              f"（{pooled_b[1]['mean_excess'] or 0:+.2f}% vs 0桶 {pooled_b[0]['mean_excess'] or 0:+.2f}%）",
              f"2. 前后两半 +1 超额均 > 0：{'✅' if c2 else '❌'}"
              f"（前 {half_b['前半'][1]['mean_excess'] or 0:+.2f}% / 后 {half_b['后半'][1]['mean_excess'] or 0:+.2f}%）",
              f"3. ≥2 只基金单独 +1 超额 > 0：{'✅' if c3 else '❌'}",
              "", f"## 结论：{verdict_text}", "",
              "<sub>形态学简化项：线段终结未实现缺口第二种情况；中枢 9 段不升级；无区间套。"
              "事件携带确认日期（因果消费），持仓为披露生效日快照（防前视）。</sub>",
            ]
    report = "\n".join(lines)
    out = BASE_DIR / "output" / "backtest_chanlun_report.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n[done] 报告 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
