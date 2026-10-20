#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""刹车裁决：大波动但引擎「保持不动」的日子，未来 1/3/5 日收益 vs ±2% 规则的动作。

背景（2026-10-20 Summer 质疑 → 审计 → 裁决）：
  audit_candidate_flips.py 已证实：引擎候选与朴素「|est|>2% 就加/减仓」规则
  一致率 78.9%，翻转召回 98%，虚警 53 条（|est|>2% 但引擎不动）。
  本脚本回答：那 53 条虚警里，如果按 ±2% 规则动手，事后是赚还是亏？

口径（诚实声明）：
1. 零网络：净值取 output/sina_nav/{code}.json 本地缓存（sina 源，已与东财逐日核对一致）
2. 时点：决策发生在 T 日 14:55，场外基金按 T 日净值成交（当晚公布）；
   前向收益 = NAV[T+h] / NAV[T] - 1，h ∈ {1,3,5} 个净值日，无前视
3. 动作优势 alpha = sign(direction) × fwd_ret：
   ADD（加仓）→ +fwd（新钱吃到的收益）；REDUCE（减仓）→ -fwd（规避掉的跌幅）
4. 费用：C 类份额持有 ≥7 天通常免申赎费，<7 天赎回费 0.5%~1.5%。
   主表按**毛收益**（不含费），并给「每次动作扣 0.5%」的敏感性列；
   1/3/5 日的效应量级普遍小于费用，结论须带着这一条读
5. 独立性：mid/post 同日非独立 → 先按 fund-day 去重（post 优先），bootstrap 按日聚类
6. 样本极小（≤53 条 / ≤40 基金日）：只作方向性裁决，不宣称统计显著

用法：python3 backtest_brake.py   （零网络，可随时复跑）
产物：output/brake_arbitration.md + .csv
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
OUT = BASE_DIR / "output"
NAV_DIR = OUT / "sina_nav"
FWD = (1, 3, 5)
COST = 0.005  # 敏感性用：每次动作 0.5% 单边费用


def load_navs() -> dict[str, tuple[list[str], list[float], dict[str, int]]]:
    out = {}
    for p in sorted(NAV_DIR.glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        rows = [(str(x[0]), float(x[1])) for x in d["navs"]]
        rows.sort()
        dates = [r[0] for r in rows]
        vals = [r[1] for r in rows]
        out[d["code"]] = (dates, vals, {dt: i for i, dt in enumerate(dates)})
    return out


def parse_events() -> tuple[list[dict], list[dict], dict]:
    """复用审计脚本的解析：返回 (全部行, 去重后基金日行, 解析器字典)。"""
    import runpy
    g = runpy.run_path(str(BASE_DIR / "audit_candidate_flips.py"), run_name="audit_mod")
    rows, files, _ = g["parse"]()
    valid = [r for r in rows if r["est"] is not None]
    return valid, files, g


def dedupe(rows: list[dict]) -> list[dict]:
    """同一 fund-day 只留一条：post 优先（信息更晚、与收盘决策同源）。"""
    best: dict[tuple[str, str], dict] = {}
    for r in rows:
        k = (r["date"], r["code"])
        cur = best.get(k)
        if cur is None or (r["slot"] == "post" and cur["slot"] != "post"):
            best[k] = r
    return sorted(best.values(), key=lambda r: (r["date"], r["code"]))


def fwd_ret(code: str, date: str, h: int, navs) -> float | None:
    if code not in navs:
        return None
    dates, vals, idx = navs[code]
    iso = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    i = idx.get(iso)
    if i is None or i + h >= len(vals):
        return None
    if vals[i] <= 0:
        return None
    return vals[i + h] / vals[i] - 1.0


def cluster_boot(vals: list[float], dates: list[str], n_boot=2000, seed=42):
    """按交易日整块重抽样（与项目 B 契约 §8 同法），返回 95% CI。"""
    import numpy as np
    if len(set(dates)) < 3 or not vals:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    by_day = defaultdict(list)
    for v, d in zip(vals, dates):
        by_day[d].append(v)
    keys = list(by_day)
    means = []
    for _ in range(n_boot):
        pick = rng.choice(len(keys), size=len(keys), replace=True)
        sample = []
        for j in pick:
            sample.extend(by_day[keys[j]])
        if sample:
            means.append(float(np.mean(sample)))
    if not means:
        return (float("nan"), float("nan"))
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(lo), float(hi)


def summarize(events: list[dict], h: int, key: str, navs, tag: str) -> dict:
    """key: 'rule'（±2% 规则 direction）或 'eng'（引擎 candidate）。"""
    vals, dates, used, skipped = [], [], [], []
    for r in events:
        fr = fwd_ret(r["code"], r["date"], h, navs)
        if fr is None:
            skipped.append(r)
            continue
        if key == "rule":
            direction = 1 if r["est"] > 0 else -1
        else:
            direction = 1 if r["cand"] == "加仓" else (-1 if r["cand"] == "减仓" else 0)
        vals.append(direction * fr)
        dates.append(r["date"])
        used.append(r)
    if not vals:
        return {"tag": tag, "h": h, "n": 0, "skipped": len(skipped)}
    n = len(vals)
    mean = sum(vals) / n
    win = sum(1 for v in vals if v > 0) / n
    lo, hi = cluster_boot(vals, dates)
    sd = (sum((v - mean) ** 2 for v in vals) / (n - 1)) ** 0.5 if n > 1 else 0.0
    t = mean / (sd / math.sqrt(n)) if sd > 0 else float("nan")
    return {"tag": tag, "h": h, "n": n, "skipped": len(skipped),
            "mean": mean, "median": sorted(vals)[n // 2], "win": win,
            "ci": (lo, hi), "t": t, "vals": vals, "dates": dates}


def pct(x: float, nd=2) -> str:
    return "—" if x != x else f"{x * 100:+.{nd}f}%"


def ci_txt(ci) -> str:
    lo, hi = ci
    if lo != lo:
        return "n/a（交易日不足）"
    return f"[{lo * 100:+.2f}%, {hi * 100:+.2f}%]"


def main() -> int:
    navs = load_navs()
    rows, files, _ = parse_events()
    valid = [r for r in rows if r["est"] is not None]
    dd = dedupe(valid)

    flips_all = [r for r in dd if r["cand"] in ("加仓", "减仓")]
    holds_all = [r for r in valid if r["cand"] == "保持不动"]
    fp_all = [r for r in valid if abs(r["est"]) > 2.0 and r["cand"] == "保持不动"]
    fp = [r for r in dd if abs(r["est"]) > 2.0 and r["cand"] == "保持不动"]
    big = [r for r in dd if abs(r["est"]) > 2.0]

    lines: list[str] = []
    P = lines.append
    P("# 刹车裁决：大波动日「不动」vs「±2% 规则动手」")
    P("")
    P(f"> 生成：{datetime.now():%Y-%m-%d %H:%M} · 零网络（本地 sina_nav 缓存）· "
      f"决策行 {len(valid)} · 去重基金日 {len(dd)} · 区间 {dd[0]['date']} ~ {dd[-1]['date']} · "
      f"净值覆盖至 {max(v[0][-1] for v in navs.values())}")
    P("")
    P("## 〇、口径")
    P("")
    P("- 事件：T 日 14:55 决策 → 按 T 日净值成交（当晚公布）→ 前向 NAV[T+h]/NAV[T]-1")
    P("- 动作优势 alpha = sign × fwd：加仓吃 +fwd，减仓规避 -fwd；毛收益，未扣费")
    P("- 同日 mid/post 非独立 → fund-day 去重（post 优先）；bootstrap 按交易日聚类")
    P(f"- 敏感性：每次动作扣 {COST * 100:.1f}% 单边费用（C 类份额 <7 日赎回费 0.5%~1.5%）")
    P("")

    # ---- 样本量 ----
    P("## 一、样本与可评分性")
    P("")
    P(f"- 虚警（\\|est\\|>2% 但引擎不动）：{len(fp_all)} 条 → 去重 {len(fp)} 个基金日")
    P(f"- 大波动日总数（去重）：{len(big)}；引擎翻转（去重）：{len(flips_all)}")
    sk = [r for r in fp if fwd_ret(r["code"], r["date"], 1, navs) is None]
    if sk:
        P(f"- 无法评分 {len(sk)} 个基金日（净值尚未公布/停牌）："
          + "、".join(f"{r['date']}/{r['code']}" for r in sk[:8])
          + ("…" if len(sk) > 8 else ""))
    P("")

    # ---- 主表：虚警日的 ±2% 动作 alpha ----
    res = {h: summarize(fp, h, "rule", navs, "虚警日·±2%动作") for h in FWD}
    P("## 二、虚警日：照 ±2% 规则动手，事后赚还是亏")
    P("")
    P("| 周期 | 可评分 | 平均 alpha | 中位 | 胜率 | 95% CI（按日聚类） | t |")
    P("|---:|---:|---:|---:|---:|---|---:|")
    for h in FWD:
        r = res[h]
        if not r["n"]:
            P(f"| T+{h} | 0 | — | — | — | — | — |")
            continue
        P(f"| T+{h} | {r['n']} | **{pct(r['mean'])}** | {pct(r['median'])} | "
          f"{r['win']:.0%} | {ci_txt(r['ci'])} | {r['t']:+.2f} |")
    P("")
    P(f"> alpha **< 0 表示「不动」是对的**（动手会亏），> 0 表示刹车踩错了（漏掉收益）。")
    P("")

    # ---- 分方向 ----
    P("## 三、分方向：追涨 vs 杀跌")
    P("")
    P("| 方向 | 周期 | n | 平均 alpha | 胜率 | 95% CI |")
    P("|---|---:|---:|---:|---:|---|")
    for label, sel in (("跌→减仓（规避跌幅）", [r for r in fp if r["est"] < 0]),
                       ("涨→加仓（追涨）", [r for r in fp if r["est"] > 0])):
        for h in FWD:
            r = summarize(sel, h, "rule", navs, label)
            if not r["n"]:
                continue
            P(f"| {label} | T+{h} | {r['n']} | {pct(r['mean'])} | {r['win']:.0%} | {ci_txt(r['ci'])} |")
    P("")

    # ---- 对照：引擎真翻转日 ----
    P("## 四、对照：引擎「真翻转」日动手，事后赚还是亏")
    P("")
    P("| 周期 | 引擎翻转 n | 引擎动作 alpha | 胜率 | 95% CI | 虚警日 alpha | 差值（翻转−虚警） |")
    P("|---:|---:|---:|---:|---|---:|---:|")
    for h in FWD:
        rf = summarize(flips_all, h, "eng", navs, "翻转日·引擎动作")
        rh = res[h]
        if not rf["n"] or not rh["n"]:
            continue
        P(f"| T+{h} | {rf['n']} | {pct(rf['mean'])} | {rf['win']:.0%} | {ci_txt(rf['ci'])} | "
          f"{pct(rh['mean'])} | **{pct(rf['mean'] - rh['mean'])}** |")
    P("")

    # ---- 敏感性：扣费 ----
    P("## 五、费用敏感性（每次动作扣 0.5%）")
    P("")
    P("| 周期 | 毛 alpha | 净 alpha | 结论是否翻转 |")
    P("|---:|---:|---:|---|")
    for h in FWD:
        r = res[h]
        if not r["n"]:
            continue
        net = r["mean"] - COST
        flip = "是" if (r["mean"] > 0) != (net > 0) else "否"
        P(f"| T+{h} | {pct(r['mean'])} | {pct(net)} | {flip} |")
    P("")

    # ---- 逐条明细 ----
    P("## 六、逐条明细（虚警日）")
    P("")
    P("| 日期 | 基金 | est | 同向度 | 相对 | 倾向分 | T+1 | T+3 | T+5 |")
    P("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for r in fp:
        cells = []
        for h in FWD:
            fr = fwd_ret(r["code"], r["date"], h, navs)
            cells.append("—" if fr is None else f"{fr * 100:+.2f}%")
        P(f"| {r['date']} | {r['code']} | {r['est']:+.2f}% | "
          f"{'—' if r['breadth'] is None else f'{r[chr(98) + chr(114) + chr(101) + chr(97) + chr(100) + chr(116) + chr(104)]:+.2f}'} | "
          f"{'—' if r['rel'] is None else f'{r[chr(114) + chr(101) + chr(108)]:+.2f}%'} | "
          f"{r['score']:+d} | " + " | ".join(cells) + " |")
    P("")

    # ---- 裁决 ----
    P("## 七、裁决")
    P("")
    if res[1]["n"]:
        m1 = res[1]["mean"]
        lo1, hi1 = res[1]["ci"]
        sig = lo1 == lo1 and ((lo1 > 0 and hi1 > 0) or (lo1 < 0 and hi1 < 0))
        verdict = ("刹车踩对了（虚警日动手为负 alpha）" if m1 < 0
                   else "刹车踩错了（虚警日动手为正 alpha，漏掉收益）")
        P(f"1. **T+1 平均 alpha {pct(m1)}**（95% CI {ci_txt((lo1, hi1))}）→ {verdict}。"
          f"CI {'不含 0，方向上可判定' if sig else '跨 0，统计上不可判定，只能作方向参考'}。")
        m5 = res[5]["mean"] if res[5]["n"] else float("nan")
        P(f"2. T+5 平均 alpha {pct(m5)}："
          + ("时间拉长后动手的收益/亏损是否放大，见第四节与逐条明细。" if m5 == m5 else "样本不足。"))
        P(f"3. 费用现实：C 类份额 <7 日赎回费 0.5%~1.5%，量级与上述毛 alpha 同阶——"
          f"**任何 T+1~T+5 的调仓，费用都可能吃掉全部差别**，这也是「不动」的隐含优势。")
    P("")
    P("<sub>局限：①样本仅 53 条 / "
      f"{len(fp)} 基金日、2 个月、4 只基金，横截面极窄，CI 宽；②净值源为 sina 缓存且只到 "
      f"{max(v[0][-1] for v in navs.values())}，10-19 及之后事件无法评分；"
      "③前向收益为净值口径，未含申赎费与择时滑点（另有 0.5% 敏感性列）；"
      "④alpha 按等权 1 单位头寸计，非组合层复利；⑤只裁决「当天该不该动」，不裁决仓位上限。</sub>")

    md = "\n".join(lines) + "\n"
    (OUT / "brake_arbitration.md").write_text(md, encoding="utf-8")

    # CSV
    import csv
    with (OUT / "brake_arbitration.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["date", "code", "est_pct", "breadth", "rel", "score", "candidate",
                    "fwd1", "fwd3", "fwd5", "alpha1", "alpha3", "alpha5"])
        for r in fp:
            fs = [fwd_ret(r["code"], r["date"], h, navs) for h in FWD]
            sign = 1 if r["est"] > 0 else -1
            w.writerow([r["date"], r["code"], round(r["est"], 4), r["breadth"], r["rel"],
                        r["score"], r["cand"]]
                       + [None if x is None else round(x, 6) for x in fs]
                       + [None if x is None else round(sign * x, 6) for x in fs])

    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
