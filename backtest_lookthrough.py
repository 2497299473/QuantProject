#!/usr/bin/env python3
"""穿透信号历史回测（第二步：裁决「重仓股缠论简化信号」是否可进引擎）。

诚实回测三原则：
1. 防前视：t 日的持仓 = 生效日（披露滞后）≤ t 的最近一期季报快照
2. 因果：个股指标逐根 K 线增量计算，任意 t 日状态只依赖 ≤t 数据（单测已验证）
3. 超额基准：每只基金自己的全体样本未来 20 日收益均值（与三因子回测口径一致）

判定标准（事先写死，跑完对照）：
- pooled：composite=+1 桶超额 > +0.3%，且 > composite=0 桶
- 时间分段：pooled 前后两半的 +1 桶超额均 > 0（防时段依赖）
- 稳健性：≥2 只基金单独 +1 桶超额 > 0
三者全过 → 曾建议 engine_factor_enabled=true；否则留在观察层。
（2026-08-25 裁决：composite 无增量/负贡献，已永久移除该开关，本脚本留档）

用法：python3 backtest_lookthrough.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from core import data_loader, lookthrough, stock_data

FWD = 20  # 未来 20 个净值日


def pearson(xs, ys) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy) if sx * sy else 0.0


def bucket_stats(samples: list[dict]) -> dict:
    """samples: [{composite, excess}] → {-1/0/+1: {n, mean_excess, win}}"""
    out = {}
    for c in (-1, 0, 1):
        rows = [s for s in samples if s["composite"] == c]
        n = len(rows)
        out[c] = {
            "n": n,
            "mean_excess": sum(r["excess"] for r in rows) / n * 100 if n else None,
            "win": (sum(1 for r in rows if r["fwd"] > 0) / n * 100) if n else None,
        }
    return out


def main() -> int:
    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    lt_cfg = cfg["lookthrough"]
    funds = cfg["fund_pool"]

    print("== 拉取基金净值与历史持仓 ==")
    all_samples, per_fund = [], {}
    stock_universe: dict[str, str] = {}  # code -> market
    fund_data = {}
    for code in funds:
        fund = data_loader.load_fund(code)
        history = lookthrough.holdings_history(code)
        for snap in history:
            for h in snap["holdings"]:
                stock_universe[h["code"]] = h["market"]
        fund_data[code] = (fund, history)
        print(f"  {code}: 净值 {len(fund['navs'])} 条，持仓快照 {len(history)} 期")

    print(f"== 拉取 {len(stock_universe)} 只个股K线（含缓存）==")
    series_map: dict[str, dict] = {}
    fail = []
    for i, (scode, mkt) in enumerate(sorted(stock_universe.items()), 1):
        try:
            k = stock_data.fetch_stock_kline(scode, mkt)
            series_map[scode] = lookthrough.stock_signal_series(k["klines"], lt_cfg)
        except Exception as e:
            fail.append(f"{scode}: {e}")
        if i % 20 == 0:
            print(f"  进度 {i}/{len(stock_universe)}")
    if fail:
        print(f"  [warn] {len(fail)} 只失败：{fail[:5]}")

    print("== 逐日重放（防前视：仅用当时已生效的持仓快照）==")
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
            agg = lookthrough.aggregate(snap, series_map, d, lt_cfg)
            if not agg:
                continue
            fwd = navs[i + FWD][1] / nav - 1
            samples.append({"date": d, "composite": agg["composite"], "fwd": fwd,
                            "excess": fwd - base, "structure": agg["structure"]})
        per_fund[code] = {"samples": samples, "base20": base * 100}
        print(f"  {code}: 有效样本 {len(samples)} 天，基准20日 {base*100:+.2f}%")

    # ---------------- 汇总 ----------------
    pooled = [s for code in funds for s in per_fund[code]["samples"]]
    if not pooled:
        print("[fail] 无样本"); return 1
    pooled_by_date = sorted(pooled, key=lambda s: s["date"])
    mid = pooled_by_date[len(pooled_by_date) // 2]["date"]
    halves = {
        "前半": [s for s in pooled if s["date"] < mid],
        "后半": [s for s in pooled if s["date"] >= mid],
    }
    ic = pearson([s["composite"] for s in pooled], [s["fwd"] for s in pooled])
    ic_struct = pearson([s["structure"] for s in pooled], [s["fwd"] for s in pooled])

    pooled_b = bucket_stats(pooled)
    fund_b = {code: bucket_stats(v["samples"]) for code, v in per_fund.items()}
    half_b = {name: bucket_stats(rows) for name, rows in halves.items()}

    c1_pass = pooled_b[1]["mean_excess"] is not None and pooled_b[1]["mean_excess"] > 0.3 \
        and pooled_b[1]["mean_excess"] > (pooled_b[0]["mean_excess"] or 0)
    c2_pass = all(hb[1]["mean_excess"] is not None and hb[1]["mean_excess"] > 0
                  for hb in half_b.values())
    c3_pass = sum(1 for code in funds
                  if fund_b[code][1]["mean_excess"] is not None and fund_b[code][1]["mean_excess"] > 0) >= 2
    verdict = c1_pass and c2_pass and c3_pass

    # ---------------- 报告 ----------------
    def fmt_b(b):
        return "\n".join(
            f"| {c:+d} | {b[c]['n']} | {b[c]['mean_excess']:+.2f}% | {b[c]['win']:.0f}% |"
            if b[c]["n"] else f"| {c:+d} | 0 | — | — |" for c in (-1, 0, 1))

    lines = [
        "# 穿透信号回测报告（重仓股缠论简化 → 基金净值未来20日）", "",
        f"> 生成：{datetime.now():%Y-%m-%d %H:%M} · 样本（基金日）{len(pooled)} · "
        f"回放区间 {pooled_by_date[0]['date']} ~ {pooled_by_date[-1]['date']}", "",
        "## 一、pooled 全样本分桶", "",
        "| composite | 样本 | 20日超额 | 胜率 |", "|---:|---:|---:|---:|",
        fmt_b(pooled_b), "",
        f"时序 IC(composite vs fwd) = {ic:+.3f} · 连续结构分 IC = {ic_struct:+.3f}", "",
        "## 二、时间分段（防时段依赖）", "",
        f"分段点：{mid}", "",
        "| 段 | composite=+1 样本 | +1 超额 |", "|---|---:|---:|"]
    for name, hb in half_b.items():
        b1 = hb[1]
        lines.append(f"| {name} | {b1['n']} | {b1['mean_excess']:+.2f}% |" if b1["n"]
                     else f"| {name} | 0 | — |")
    lines += ["", "## 三、分基金", "",
              "| 基金 | 有效样本 | 基准20日 | +1样本 | +1超额 | -1样本 | -1超额 |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for code in funds:
        v, b = per_fund[code], fund_b[code]
        p1 = f"{b[1]['mean_excess']:+.2f}%" if b[1]["n"] else "—"
        m1 = f"{b[-1]['mean_excess']:+.2f}%" if b[-1]["n"] else "—"
        lines.append(f"| {code} | {len(v['samples'])} | {v['base20']:+.2f}% | "
                     f"{b[1]['n']} | {p1} | {b[-1]['n']} | {m1} |")
    if verdict:
        verdict_text = "❌ 已裁决（2026-08-25）—— composite 无增量/负贡献，engine_factor_enabled 已永久移除，本脚本留档"
    else:
        verdict_text = "❌ 未通过 —— 穿透信号保留在观察层（报告展示，不进打分）"
    lines += ["", "## 四、判定（标准事先写死）", "",
              f"1. pooled +1 超额 > +0.3% 且 > 0桶：{'✅' if c1_pass else '❌'}"
              f"（{pooled_b[1]['mean_excess']:+.2f}% vs 0桶 {pooled_b[0]['mean_excess'] or 0:+.2f}%）",
              f"2. 前后两半 +1 超额均 > 0：{'✅' if c2_pass else '❌'}"
              f"（前 {half_b['前半'][1]['mean_excess'] or 0:+.2f}% / 后 {half_b['后半'][1]['mean_excess'] or 0:+.2f}%）",
              f"3. ≥2 只基金单独 +1 超额 > 0：{'✅' if c3_pass else '❌'}",
              "", f"## 结论：{verdict_text}", "",
              "<sub>防前视：t 日仅用披露生效日 ≤ t 的持仓快照；个股指标因果计算。"
              "前复权 K 线按当日口径回放（轻微幸存者偏差已知且不可避免）。</sub>",
            ]
    report = "\n".join(lines)
    out = BASE_DIR / "output" / "backtest_lookthrough_report.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n[done] 报告 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
