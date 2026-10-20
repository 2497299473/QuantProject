#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""刹车裁决 · 长历史扩容版（样本外复核）。

背景（2026-10-20）：live 窗口只有 8/25–10/19、53 条虚警 / 26 基金日，CI 宽到无法定论。
本脚本改用仓库既有冻结样本（samples_frozen_*.jsonl，2020-04 ~ 2026-08）做**历史回放**：
同一条 decision_engine 在 6 年样本上重新评分，取 |est|>2% 但引擎不动的日子，
用样本自带的 fwd1/3/5 算 alpha，检验 live 窗口那个「涨侧刹车有效 / 跌侧刹车有害」的
不对称结论能否在独立时段复现。

口径与局限（诚实声明）：
1. 零网络：只读 forecast_outputs/samples_frozen_*.jsonl
2. fwd1/3/5 为样本自带标签（已按 T 日净值起算），单位分数，报表 ×100 显示
3. **与 live 不同构**：历史无 11:30 快照 → feat_1130=None，缺「盘中变化方向 Δ」一维；
   account_state=None → 账户维不参与。故这里测的是四维 + 涨跌幅的刹车，
   与 8/25 起实时五维口径有已知差异（live 有 Δ，历史没有）
4. 冻结窗口 2020-04-27 ~ 2026-08-12 与 live 窗口（8/25 起）**不重叠**，属独立样本外
5. 费用敏感性同 live 版：每次动作扣 0.5%
6. alpha = direction × fwd；方向来自「若按 ±2% 规则」或「引擎候选」

用法：python3 backtest_brake_history.py
产物：output/brake_history.md + .csv
"""
from __future__ import annotations

import csv
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import numpy as np  # noqa: E402

from core import decision_engine as de  # noqa: E402
import backtest_brake as bb  # noqa: E402  复用 cluster_boot / pct / ci_txt

OUT = BASE_DIR / "output"
FWD = (1, 3, 5)
COST = 0.005


def load_frozen() -> tuple[Path, list[dict]]:
    paths = sorted((BASE_DIR / "forecast_outputs").glob("samples_frozen_*.jsonl"))
    if not paths:
        raise SystemExit("[fail] 找不到 samples_frozen_*.jsonl")
    p = paths[-1]
    rows = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return p, rows


def replay(samples: list[dict]) -> list[dict]:
    """用当前 decision_engine 重放历史样本（account=None、无 11:30 基线）。"""
    pool_by_date: dict[str, dict[str, float]] = defaultdict(dict)
    for s in samples:
        if s.get("est_chg") is not None:
            pool_by_date[s["date"]][s["fund"]] = float(s["est_chg"]) * 100.0

    out = []
    for s in samples:
        est = s.get("est_chg")
        if est is None:
            continue
        feat = {
            "est_return": float(est) * 100.0,
            "breadth": s.get("breadth"),
            "concentration": s.get("concentration"),
            "covered_pct": s.get("covered_pct") or 0.0,
            "holdings_age_days": 0,
        }
        inp = de.DecisionInput(
            code=s["fund"], name=s["fund"], slot="post",
            technical_score=s.get("score"),
            feat_1130=None, feat_1455=feat,
            pool_est=pool_by_date.get(s["date"], {}),
            account_state=None,
        )
        d = de.evaluate(inp)
        out.append({
            "date": s["date"], "code": s["fund"], "est": float(est) * 100.0,
            "cand": d.candidate, "score": d.score,
            "breadth": s.get("breadth"),
            "fwd": {h: s.get(f"fwd{h}") for h in FWD},
        })
    return out


def summarize(events: list[dict], h: int, key: str = "rule") -> dict:
    vals, dates = [], []
    for r in events:
        fr = r["fwd"].get(h)
        if fr is None:
            continue
        if key == "rule":
            direction = 1 if r["est"] > 0 else -1
        else:
            direction = 1 if r["cand"] == "ADD" else (-1 if r["cand"] == "REDUCE" else 0)
        vals.append(direction * float(fr))
        dates.append(r["date"])
    n = len(vals)
    if not n:
        return {"n": 0, "mean": float("nan"), "median": float("nan"),
                "win": float("nan"), "ci": (float("nan"), float("nan")), "t": float("nan")}
    mean = sum(vals) / n
    win = sum(1 for v in vals if v > 0) / n
    lo, hi = bb.cluster_boot(vals, dates)
    sd = (sum((v - mean) ** 2 for v in vals) / (n - 1)) ** 0.5 if n > 1 else 0.0
    t = mean / (sd / math.sqrt(n)) if sd > 0 else float("nan")
    return {"n": n, "mean": mean, "median": sorted(vals)[n // 2],
            "win": win, "ci": (lo, hi), "t": t}


def row_md(label: str, h: int, r: dict) -> str:
    if not r["n"]:
        return f"| {label} | T+{h} | 0 | — | — | — | — |"
    return (f"| {label} | T+{h} | {r['n']} | {bb.pct(r['mean'])} | {bb.pct(r['median'])} | "
            f"{r['win']:.0%} | {bb.ci_txt(r['ci'])} | {r['t']:+.2f} |")


def main() -> int:
    path, samples = load_frozen()
    ev = replay(samples)

    fp = [r for r in ev if abs(r["est"]) > 2.0 and r["cand"] == "HOLD"]
    flips = [r for r in ev if r["cand"] in ("ADD", "REDUCE")]
    big = [r for r in ev if abs(r["est"]) > 2.0]
    fp_up = [r for r in fp if r["est"] > 0]
    fp_dn = [r for r in fp if r["est"] < 0]

    lines: list[str] = []
    P = lines.append
    dates = sorted({r["date"] for r in ev})
    P("# 刹车裁决 · 长历史扩容版（样本外复核）")
    P("")
    P(f"> 生成：{datetime.now():%Y-%m-%d %H:%M} · 样本 {path.name} · "
      f"决策行 {len(ev)} · 基金日 {len(dates)} · 区间 {dates[0]} ~ {dates[-1]} · "
      f"基金 {len({r['code'] for r in ev})} 只 · 零网络")
    P("")
    P("## 〇、口径与局限")
    P("")
    P("- 历史回放用当前 `decision_engine` 重评：`feat_1130=None`（无 11:30 基线，缺 Δ 维）、"
      "`account_state=None`（账户维不参与）→ **与 8/25 起实时五维口径不同构**")
    P("- fwd1/3/5 为冻结样本自带标签；alpha = direction × fwd，毛收益，未扣费")
    P("- 本窗口 2020-04 ~ 2026-08 与 live 窗口（8/25 起）不重叠，属独立样本外")
    P(f"- 敏感性：每次动作扣 {COST * 100:.1f}% 单边费用")
    P("")
    P("## 一、样本")
    P("")
    P(f"- 全样本 {len(ev)} 行 / {len(dates)} 个交易日 / {len({r['code'] for r in ev})} 只基金")
    P(f"- 大波动（\\|est\\|>2%）{len(big)} 行；虚警（大波动但引擎不动）**{len(fp)}** 行；"
      f"引擎翻转 **{len(flips)}** 行")
    P("")
    P("## 二、虚警日：照 ±2% 规则动手的 alpha（全样本）")
    P("")
    P("| 分组 | 周期 | n | 平均 alpha | 中位 | 胜率 | 95% CI（按日聚类） | t |")
    P("|---|---:|---:|---:|---:|---:|---|---:|")
    for h in FWD:
        P(row_md("虚警日·全部", h, summarize(fp, h)))
    P("")
    P("## 三、分方向（关键）")
    P("")
    P("| 分组 | 周期 | n | 平均 alpha | 中位 | 胜率 | 95% CI | t |")
    P("|---|---:|---:|---:|---:|---:|---|---:|")
    for h in FWD:
        P(row_md("涨→加仓（追涨）", h, summarize(fp_up, h)))
    for h in FWD:
        P(row_md("跌→减仓（止损）", h, summarize(fp_dn, h)))
    P("")
    P("## 四、对照：引擎真翻转日")
    P("")
    P("| 分组 | 周期 | n | 平均 alpha | 胜率 | 95% CI |")
    P("|---|---:|---:|---:|---:|---|")
    for h in FWD:
        r = summarize(flips, h, "eng")
        if r["n"]:
            P(f"| 引擎翻转 | T+{h} | {r['n']} | {bb.pct(r['mean'])} | {r['win']:.0%} | {bb.ci_txt(r['ci'])} |")
    P("")
    P("| 周期 | 翻转日 | 虚警日 | 差值（翻转−虚警） |")
    P("|---:|---:|---:|---:|")
    for h in FWD:
        a, b = summarize(flips, h, "eng"), summarize(fp, h)
        if a["n"] and b["n"]:
            P(f"| T+{h} | {bb.pct(a['mean'])} | {bb.pct(b['mean'])} | **{bb.pct(a['mean'] - b['mean'])}** |")
    P("")
    P("## 五、逐年稳定性（虚警日 alpha，检验结论是否依赖某一段行情）")
    P("")
    P("| 年 | 虚警 n | T+1 | T+3 | T+5 |")
    P("|---:|---:|---:|---:|---:|")
    for y in sorted({r["date"][:4] for r in fp}):
        sel = [r for r in fp if r["date"][:4] == y]
        cells = [bb.pct(summarize(sel, h)["mean"]) for h in FWD]
        P(f"| {y} | {len(sel)} | " + " | ".join(cells) + " |")
    P("")
    P("## 六、费用敏感性（每次动作扣 0.5%）")
    P("")
    P("| 分组 | 周期 | 毛 alpha | 净 alpha |")
    P("|---|---:|---:|---:|")
    for label, sel in (("虚警·涨→加仓", fp_up), ("虚警·跌→减仓", fp_dn)):
        for h in FWD:
            r = summarize(sel, h)
            if r["n"]:
                P(f"| {label} | T+{h} | {bb.pct(r['mean'])} | {bb.pct(r['mean'] - COST)} |")
    P("")

    up5, dn5 = summarize(fp_up, 5), summarize(fp_dn, 5)
    P("## 七、裁决（与 live 窗口对照）")
    P("")
    P("| 结论 | live 窗口（26 基金日） | 长历史（本表） | 是否复现 |")
    P("|---|---|---|---|")
    up_live, dn_live = "-4.50%（CI 不含 0）", "+1.87%（胜率 73%）"
    P(f"| 涨侧刹车是否有效 | {up_live} | {bb.pct(up5['mean'])}（n={up5['n']}，"
      f"{bb.ci_txt(up5['ci'])}） | "
      + ("是" if up5["n"] and up5["mean"] < 0 else "否") + " |")
    P(f"| 跌侧刹车是否有害 | {dn_live} | {bb.pct(dn5['mean'])}（n={dn5['n']}，"
      f"{bb.ci_txt(dn5['ci'])}） | "
      + ("是" if dn5["n"] and dn5["mean"] > 0 else "否") + " |")
    P("")
    P("<sub>局限：①历史回放缺 Δ 维与账户维，与实时口径不同构，故只能验证「涨跌幅 + 同向度 + "
      "相对 + 中期」四维版的行为，不能替代实时五维的最终裁决；②fwd 标签由样本生成时固化，"
      "不改写；③alpha 按等权 1 单位头寸，非组合层复利；④跨 2020–2026 含多轮 regime，"
      "逐年表用于识别时段依赖；⑤不含申赎费与滑点（另有 0.5% 敏感性）。</sub>")

    md = "\n".join(lines) + "\n"
    (OUT / "brake_history.md").write_text(md, encoding="utf-8")

    with (OUT / "brake_history.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["date", "code", "est_pct", "score", "candidate", "breadth",
                    "fwd1", "fwd3", "fwd5", "alpha1", "alpha3", "alpha5"])
        for r in fp:
            sign = 1 if r["est"] > 0 else -1
            w.writerow([r["date"], r["code"], round(r["est"], 4), r["score"], r["cand"],
                        r["breadth"]]
                       + [None if r["fwd"].get(h) is None else round(float(r["fwd"][h]), 6)
                          for h in FWD]
                       + [None if r["fwd"].get(h) is None else round(sign * float(r["fwd"][h]), 6)
                          for h in FWD])

    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
