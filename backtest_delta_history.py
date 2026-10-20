#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Δ 维历史口径：用 11:30/14:55 配对快照补算决策层边际影响与预注册 ΔIC。

背景（2026-10-20）：长历史回放（2020-04 ~ 2026-08）没有 11:30 快照，Δ 维缺席，
四维结论不能直接外推到实时五维。本脚本用 2026-08-31 起的配对快照
（data/intraday_features.jsonl，116 条 / 29 个交易日）补两块证据：

  A. 决策层：同一行数据，带 Δ / 不带 Δ 各重放一次 decision_engine，
     量分数位移、候选翻转、虚警日 alpha 变化——回答「四维历史回放是不是
     五维的公平代理」
  B. 预测层：预注册 backtest_delta_intraday 的 ΔIC 判据（R3），此前停在 pending

口径与局限（诚实声明）：
  1) 零网络：净值取 output/sina_nav 缓存（截至 2026-10-16），故 fwd5 标签只到约 10-09
  2) 14:55 侧取当日 post 报告的 est / Δ / 同向度，concentration 与 covered 由快照补齐；
     11:30 侧取同日 mid 快照；用 est1130 = est1455 − Δ 保证两侧同尺度（避免单位歧义）
  3) 重放分数与报告分数做一致性校验；偏差大的行在报告里标出
  4) 只有 29 个交易日，ΔIC 仅作方向证据，不作「接入 Δ 维」的依据

用法：python3 backtest_delta_history.py
产物：output/delta_history.md + .csv
"""
from __future__ import annotations

import json
import math
import runpy
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
OUT = BASE_DIR / "output"
NAV_DIR = OUT / "sina_nav"
FWD = (1, 3, 5)


# ---------- 数据装载 ----------

def load_pairs() -> dict[tuple[str, str], dict[str, dict]]:
    """(date, code) -> {'mid': feats, 'post': feats}；两槽齐全才算配对。"""
    path = BASE_DIR / "data" / "intraday_features.jsonl"
    by_key: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            feats = r.get("features") or {}
            if r.get("date") and r.get("fund") and feats:
                by_key[(r["date"], r["fund"])][r.get("slot")] = feats
    return {k: v for k, v in by_key.items() if "mid" in v and "post" in v}


def load_reports() -> dict[tuple[str, str], dict]:
    """post 决策行：(date, code) -> {est, delta, breadth, tech, cand, score}。"""
    g = runpy.run_path(str(BASE_DIR / "audit_candidate_flips.py"), run_name="audit_mod")
    rows, _files, _meta = g["parse"]()
    out = {}
    for r in rows:
        if r.get("slot") != "post" or r.get("est") is None:
            continue
        d = str(r["date"])
        iso = f"{d[:4]}-{d[4:6]}-{d[6:]}" if "-" not in d else d
        out[(iso, r["code"])] = r
    return out


def load_navs() -> dict[str, tuple[list[str], list[float], dict[str, int]]]:
    out = {}
    for p in sorted(NAV_DIR.glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        rows = sorted((str(x[0]), float(x[1])) for x in d["navs"])
        dates = [r[0] for r in rows]
        vals = [r[1] for r in rows]
        out[d["code"]] = (dates, vals, {dt: i for i, dt in enumerate(dates)})
    return out


def fwd_ret(navs, code: str, date: str, h: int) -> float | None:
    if code not in navs:
        return None
    _dates, vals, idx = navs[code]
    iso = f"{date[:4]}-{date[4:6]}-{date[6:]}" if "-" not in date else date
    i = idx.get(iso)
    if i is None or i + h >= len(vals) or vals[i] <= 0:
        return None
    return (vals[i + h] / vals[i] - 1.0) * 100.0


# ---------- 重放 ----------

def build_rows(pairs, reports) -> list[dict]:
    rows = []
    for (date, code), slots in sorted(pairs.items()):
        rep = reports.get((date, code))
        if rep is None:
            continue
        est1455 = float(rep["est"])
        delta = rep.get("delta")
        if delta is None:
            delta = 0.0
        est1130 = est1455 - float(delta)
        mid_f, post_f = slots["mid"], slots["post"]
        f1130 = {"est_return": est1130,
                 "breadth": mid_f.get("breadth"),
                 "concentration": mid_f.get("concentration"),
                 "covered_pct": mid_f.get("covered_pct") or 0.0,
                 "holdings_age_days": mid_f.get("holdings_age_days") or 0}
        f1455 = {"est_return": est1455,
                 "breadth": rep.get("breadth") if rep.get("breadth") is not None
                 else post_f.get("breadth"),
                 "concentration": post_f.get("concentration"),
                 "covered_pct": post_f.get("covered_pct") or 0.0,
                 "holdings_age_days": mid_f.get("holdings_age_days") or 0}
        rows.append({"date": date, "code": code, "est1455": est1455,
                     "delta": float(delta), "est1130": est1130,
                     "tech": rep.get("tech"), "cand_report": rep.get("cand"),
                     "score_report": rep.get("score"),
                     "f1130": f1130, "f1455": f1455})
    return rows


def replay(rows) -> None:
    from core import decision_engine as de
    pool_by_date = defaultdict(dict)
    for r in rows:
        pool_by_date[r["date"]][r["code"]] = r["est1455"]

    for r in rows:
        for tag, f1130 in (("with", r["f1130"]), ("without", None)):
            inp = de.DecisionInput(
                code=r["code"], name=r["code"], slot="post",
                technical_score=r["tech"] if r["tech"] is not None else 0,
                feat_1130=f1130, feat_1455=r["f1455"],
                pool_est=pool_by_date.get(r["date"], {}),
                account_state=None)
            d = de.evaluate(inp)
            r[f"score_{tag}"] = float(d.score)
            r[f"cand_{tag}"] = d.candidate


# ---------- 统计 ----------

def spearman(x: list[float], y: list[float]) -> float:
    if len(x) < 3:
        return float("nan")
    a = np.asarray(x, dtype=float)
    b = np.asarray(y, dtype=float)
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    if ra.std() == 0 or rb.std() == 0:
        return float("nan")
    return float(np.corrcoef(ra, rb)[0, 1])


def ols_fit(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    X1 = np.column_stack([np.ones(len(X)), X])
    beta, *_ = np.linalg.lstsq(X1, y, rcond=None)
    return beta


def lo_day_out_ic(rows: list[dict], feats: tuple[str, ...], key_y: str = "fwd5") -> float:
    """留一日交叉：训练=其余交易日，预测当日；返回 pooled Spearman(pred, y)。"""
    days = sorted({r["date"] for r in rows})
    preds, ys = {}, {}
    for d in days:
        tr = [r for r in rows if r["date"] != d]
        te = [r for r in rows if r["date"] == d]
        if len(tr) < 10 or not te:
            continue
        Xtr = np.array([[r[k] for k in feats] for r in tr], dtype=float)
        ytr = np.array([r[key_y] for r in tr], dtype=float)
        mu, sd = Xtr.mean(0), Xtr.std(0)
        sd[sd == 0] = 1.0
        beta = ols_fit((Xtr - mu) / sd, ytr)
        for r in te:
            x = (np.array([r[k] for k in feats], dtype=float) - mu) / sd
            preds[id(r)] = float(np.r_[1.0, x] @ beta)
            ys[id(r)] = r[key_y]
    keys = list(preds)
    return spearman([preds[k] for k in keys], [ys[k] for k in keys])


def day_bootstrap_dic(rows, n_boot=1000, seed=42):
    """按交易日整块重抽样，返回 ΔIC 的 95% CI。"""
    rng = np.random.default_rng(seed)
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["date"]].append(r)
    keys = list(by_day)
    base = spearman([r["est1455"] for r in rows], [r["fwd5"] for r in rows])
    diffs = []
    for _ in range(n_boot):
        pick = rng.choice(len(keys), size=len(keys), replace=True)
        sub = [r for j in pick for r in by_day[keys[j]]]
        if len(sub) < 10:
            continue
        b = spearman([r["est1455"] for r in sub], [r["fwd5"] for r in sub])
        a = lo_day_out_ic(sub, ("est1455", "delta"))
        if b == b and a == a:
            diffs.append(a - b)
    if not diffs:
        return float("nan"), float("nan")
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return float(lo), float(hi)


def pct(x, nd=2):
    return "—" if x != x else f"{x:+.{nd}f}%"


def main() -> int:
    pairs = load_pairs()
    reports = load_reports()
    navs = load_navs()
    rows = build_rows(pairs, reports)
    if not rows:
        print("[fail] 配对快照与 post 报告无交集（日期口径不一致？），未产出报告")
        return 1
    replay(rows)

    labeled = []
    for r in rows:
        r["fwd5"] = fwd_ret(navs, r["code"], r["date"], 5)
        for h in FWD:
            r[f"fwd{h}"] = fwd_ret(navs, r["code"], r["date"], h)
        if r["fwd5"] is not None:
            labeled.append(r)

    shifts = [r["score_with"] - r["score_without"] for r in rows]
    flips = [r for r in rows if r["cand_with"] != r["cand_without"]]
    agree = [r for r in rows if r["score_report"] is not None and abs(
        r["score_with"] - float(r["score_report"])) <= 2]

    base_ic = spearman([r["est1455"] for r in labeled], [r["fwd5"] for r in labeled])
    aug_ic = lo_day_out_ic(labeled, ("est1455", "delta"))
    delta_ic_only = spearman([r["delta"] for r in labeled], [r["fwd5"] for r in labeled])
    est1130_ic = spearman([r["est1130"] for r in labeled], [r["fwd5"] for r in labeled])
    dic = aug_ic - base_ic
    lo, hi = day_bootstrap_dic(labeled)

    g = runpy.run_path(str(BASE_DIR / "backtest_delta_intraday.py"),
                       run_name="delta_exp")
    verdict = g["verdict_for"](dic, (lo, hi), len({r["date"] for r in rows}))

    # 虚警日 alpha：带 Δ / 不带 Δ
    def fp_alpha(tag, h):
        vals = [(-r[f"fwd{h}"] if r["est1455"] < 0 else r[f"fwd{h}"])
                for r in rows
                if abs(r["est1455"]) > 2.0 and r[f"cand_{tag}"] == "HOLD"
                and r.get(f"fwd{h}") is not None]
        return (len(vals), sum(vals) / len(vals)) if vals else (0, float("nan"))

    lines = []
    P = lines.append
    P("# Δ 维历史口径：配对快照上的边际影响与 ΔIC")
    P("")
    P(f"> 生成：{datetime.now():%Y-%m-%d %H:%M} · 零网络 · 配对样本 {len(rows)} 条 / "
      f"{len({r['date'] for r in rows})} 个交易日 · 标签可评分 {len(labeled)} 条 · "
      f"净值截至 {max(v[0][-1] for v in navs.values())}")
    P("")
    P("## 〇、这笔账要回答什么")
    P("")
    P("长历史回放（2020-04 ~ 2026-08）没有 11:30 快照，Δ 维整体缺席。")
    P("本表只用「11:30 与 14:55 都留痕」的 29 个交易日，量 Δ 在这段窗口里到底改变了什么，")
    P("以此判断四维历史结论能否当作五维的代理。")
    P("")
    P("## 一、决策层：分数位移与候选翻转")
    P("")
    arr = np.array(shifts, dtype=float)
    P(f"- 重放行 {len(rows)}；分数位移 mean {arr.mean():+.2f} / median {np.median(arr):+.2f} / "
      f"p90 {np.percentile(np.abs(arr), 90):.2f}")
    P(f"- 位移 ≥5 分：{int((np.abs(arr) >= 5).sum())} 行（{np.mean(np.abs(arr) >= 5):.0%}）；"
      f"≥10 分：{int((np.abs(arr) >= 10).sum())} 行")
    P(f"- 候选翻转：{len(flips)} 行 / {len(rows)}（{len(flips) / max(len(rows), 1):.0%}）")
    if flips:
        for r in flips[:8]:
            P(f"  - {r['date']} {r['code']}：{r['cand_without']} → {r['cand_with']}"
              f"（{r['score_without']:+.0f} → {r['score_with']:+.0f}，Δ{r['delta']:+.2f}%）")
    P(f"- 重放分数与报告分数一致（±2 分内）：{len(agree)}/{len(rows)}"
      f"（{len(agree) / max(len(rows), 1):.0%}）")
    P("")
    P("## 二、预测层：预注册 ΔIC（此前停在 pending）")
    P("")
    P(f"- IC(14:55 est, fwd5) = {base_ic:+.3f}")
    P(f"- IC(11:30 est, fwd5) = {est1130_ic:+.3f}")
    P(f"- IC(Δ 单独, fwd5) = {delta_ic_only:+.3f}")
    P(f"- IC(est+Δ，留一日交叉) = {aug_ic:+.3f}")
    P(f"- **ΔIC = {dic:+.3f}**，按日聚类 95% CI [{lo:+.3f}, {hi:+.3f}]")
    P(f"- 预注册判定：**{verdict['verdict']}** — {verdict['reason']}")
    P("")
    P("## 三、对刹车裁决的影响")
    P("")
    P("| 周期 | 带 Δ：虚警 n | 带 Δ alpha | 不带 Δ：虚警 n | 不带 Δ alpha |")
    P("|---:|---:|---:|---:|---:|")
    for h in FWD:
        n1, a1 = fp_alpha("with", h)
        n0, a0 = fp_alpha("without", h)
        P(f"| T+{h} | {n1} | {pct(a1)} | {n0} | {pct(a0)} |")
    P("")
    P("<sub>局限：①仅 29 个交易日、116 条，ΔIC 只能作方向证据；②fwd5 标签来自本地净值缓存"
      "（截至 2026-10-16），故仅约 28 个交易日可评分；③14:55 的 concentration/covered 取自同日快照，"
      "与实盘引擎入参可能有细微差异；④预注册判据的 ΔIC 阈值未改，仅补算了数值。</sub>")

    md = "\n".join(lines) + "\n"
    (OUT / "delta_history.md").write_text(md, encoding="utf-8")

    import csv
    with (OUT / "delta_history.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["date", "code", "est1455", "delta", "est1130", "score_with",
                    "score_without", "cand_with", "cand_without", "score_report",
                    "fwd1", "fwd3", "fwd5"])
        for r in rows:
            w.writerow([r["date"], r["code"], round(r["est1455"], 4), round(r["delta"], 4),
                        round(r["est1130"], 4), r["score_with"], r["score_without"],
                        r["cand_with"], r["cand_without"], r["score_report"]]
                       + [None if r.get(f"fwd{h}") is None else round(r[f"fwd{h}"], 4)
                          for h in FWD])

    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
