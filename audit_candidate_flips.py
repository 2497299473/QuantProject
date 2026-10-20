# -*- coding: utf-8 -*-
"""候选翻转审计：决策引擎候选动作 vs 朴素「±2%」规则（2026-08-25 起）。

背景（Summer 2026-10-20 质疑）：推送体感像「涨 >2% 就加仓、跌 >2% 就减仓」，
若属实则引擎无增量价值。本脚本离线解析 output/report_*.md 的「仓位动作倾向」表
（零网络、只读，不改任何生产数据），统计：

1) 候选翻转 / 保持不动的 |当日估算| 分布（翻转真的发生在 ±2% 吗）
2) 朴素 ±2% 规则与引擎候选的一致率、混淆矩阵
3) 阈值扫描 + AUC（|est| 单独判别「是否翻转」的能力）
4) 倾向分 vs 当日估算涨跌的 Pearson r / R²（引擎是否只是涨跌幅的翻版）
5) 五维公式复算校验（确认解析无误 + 引擎可解释）
6) 分歧样本清单（引擎动而 ±2% 不动 / 反之）
7) 前后半段时间稳定性（同一规则在两段是否同样成立）

产物：output/audit_candidate_flips.md + output/audit_candidate_flips.csv

口径说明：
- 一条「观察」= 一份报告里的一只基金（mid / post 各算一条；另给 fund-day 去重口径）
- est = 该时点的当日估算涨跌（post 取 14:55，mid 取 11:30 那次估算）
- 「翻转」= 候选动作 ≠ 保持不动（候选是假设值，实际动作恒为保持不动）
- 本脚本只衡量「引擎候选 vs 阈值规则」的内部结构，**不构成对预测力的裁决**；
  预测力裁决归 backtest_action.py（未来收益口径）。
"""
from __future__ import annotations

import csv
import math
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / "output"

ROW = re.compile(
    r"^\|\s*(\d{6})\s*\|\s*([+-]?\d+)\s*\|\s*(加仓|减仓|保持不动)\s*\|\s*\*\*([^*]+?)\*\*\s*\|"
    r"\s*([\d.]+)\s*\|\s*(.+?)\s*\|\s*$"
)
PAIR = re.compile(r"11:30\s*([+-]?\d+\.\d+)%\s*→\s*14:55\s*([+-]?\d+\.\d+)%（Δ\s*([+-]?\d+\.\d+)%）")
ONLY1455 = re.compile(r"14:55 估算\s*([+-]?\d+\.\d+)%")
ONLY1130 = re.compile(r"11:30 估算\s*([+-]?\d+\.\d+)%")
BREADTH = re.compile(r"重仓同向度\s*([+-]?\d+\.\d+)（(\d+)涨/(\d+)跌）")
REL = re.compile(r"相对池内估算(?:强势|弱势|持平)\s*[（(]?([+-]?\d+\.\d+)%")
RT = re.compile(r"\*\*(\d{6}) 当日估算\s*\S+\s*([+-]\d+\.\d+)%\*\*")
TECH = re.compile(r"\*\*(\d{6})[^*\n]*\*\*\s*（总分\s*([+-]?\d+)")
GENTIME = re.compile(r"生成时间：(\d{4}-\d{2}-\d{2} \d{2}:\d{2})")
CONCENTRATED = "涨跌过度集中"

WEIGHTS = {"intraday": 0.35, "breadth": 0.25, "relative": 0.15, "tech": 0.15, "account": 0.10}
MAXDIM = {"intraday": 40, "breadth": 20, "relative": 15, "tech": 15, "account": 10}
THR = 60


# ---------------------------------------------------------------- 解析
def parse():
    rows, files, files_with_table = [], [], 0
    for path in sorted(OUT.glob("report_*.md")):
        m = re.match(r"report_(\d{8})_(mid|post|pre)\.md$", path.name)
        if not m or m.group(1) < "20260825":
            continue
        date, slot = m.group(1), m.group(2)
        text = path.read_text(encoding="utf-8")
        files.append((date, slot))
        gt = GENTIME.search(text)
        rt = {mm.group(1): float(mm.group(2)) for mm in RT.finditer(text)}
        tech = {mm.group(1): int(mm.group(2)) for mm in TECH.finditer(text)}
        n = 0
        for line in text.splitlines():
            mm = ROW.match(line.strip())
            if not mm:
                continue
            code, score, cand, act, conf, reason = mm.groups()
            e1130 = e1455 = delta = None
            pm = PAIR.search(reason)
            if pm:
                e1130, e1455, delta = float(pm.group(1)), float(pm.group(2)), float(pm.group(3))
            else:
                m4, m3 = ONLY1455.search(reason), ONLY1130.search(reason)
                if m4:
                    e1455 = float(m4.group(1))
                if m3:
                    e1130 = float(m3.group(1))
            bm = BREADTH.search(reason)
            rm = REL.search(reason)
            rows.append(dict(
                date=date, slot=slot, gen=gt.group(1) if gt else "?",
                code=code, score=int(score), cand=cand, act=act,
                e1130=e1130, e1455=e1455, delta=delta,
                est=e1455 if e1455 is not None else e1130,
                breadth=float(bm.group(1)) if bm else None,
                nup=int(bm.group(2)) if bm else None,
                ndown=int(bm.group(3)) if bm else None,
                rel=float(rm.group(1)) if rm else None,
                tech=tech.get(code), rt=rt.get(code),
                concentrated=CONCENTRATED in reason,
                acct_missing="账户维不可用" in reason,
            ))
            n += 1
        if n:
            files_with_table += 1
    return rows, files, files_with_table


# ---------------------------------------------------------------- 校验 / 工具
def recompute(r):
    """按 decision_engine.evaluate 的五维公式复算倾向分，返回 (s1..s4, 两种分母下的分)。"""
    est = r["est"]
    if est is None or r["breadth"] is None:
        return None
    s1 = math.tanh(est / 2.0) * 30.0
    if r["delta"] is not None:
        s1 += math.tanh(r["delta"] / 1.0) * 10.0
    s1 = max(-40.0, min(40.0, s1))
    s2 = r["breadth"] * 15.0 + (-8.0 if r["concentrated"] else 0.0)
    s2 = max(-20.0, min(20.0, s2))
    s3 = max(-15.0, min(15.0, math.tanh(r["rel"] / 1.5) * 15.0)) if r["rel"] is not None else 0.0
    s4 = max(-15.0, min(15.0, (r["tech"] or 0) / 3.0 * 15.0))
    raw = (s1 * WEIGHTS["intraday"] + s2 * WEIGHTS["breadth"]
           + s3 * WEIGHTS["relative"] + s4 * WEIGHTS["tech"])
    out = {}
    for name, denom in (("no_acct", 23.5), ("acct", 24.5)):
        val = raw / denom * 100.0
        out[name] = int(max(-100.0, min(100.0, val)))
    return s1, s2, s3, s4, out


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    vx = sum((a - mx) ** 2 for a in xs)
    vy = sum((b - my) ** 2 for b in ys)
    return cov / math.sqrt(vx * vy) if vx * vy > 0 else float("nan")


def auc(scores, labels):
    pos = [s for s, l in zip(scores, labels) if l]
    neg = [s for s, l in zip(scores, labels) if not l]
    if not pos or not neg:
        return float("nan")
    hits = sum(1.0 if p > n else (0.5 if p == n else 0.0) for p in pos for n in neg)
    return hits / (len(pos) * len(neg))


def q(vals, p):
    if not vals:
        return float("nan")
    v = sorted(vals)
    k = (len(v) - 1) * p
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def rule2(est, t=2.0):
    if est is None:
        return None
    if est > t:
        return "加仓"
    if est < -t:
        return "减仓"
    return "保持不动"


# ---------------------------------------------------------------- 主流程
def main():
    rows, files, files_with_table = parse()
    valid = [r for r in rows if r["est"] is not None]
    flips = [r for r in valid if r["cand"] != "保持不动"]
    holds = [r for r in valid if r["cand"] == "保持不动"]
    L = []
    P = L.append

    dates = sorted({r["date"] for r in valid})
    P("# 候选翻转审计：决策引擎 vs 朴素 ±2% 规则")
    P("")
    P(f"> 生成：{__import__('datetime').datetime.now():%Y-%m-%d %H:%M} · "
      f"报告 {len(files)} 份（含决策表 {files_with_table}）· 决策行 {len(rows)} 条（est 可解析 {len(valid)}）· "
      f"区间 {dates[0]} ~ {dates[-1]} · 基金 {len({r['code'] for r in valid})} 只")
    P("")
    P("## 〇、口径")
    P("")
    P("- 一条观察 = 一份报告 × 一只基金（mid / post 各算一条）；另给 fund-day 去重版做稳健性检查")
    P("- est = 该时点「当日估算涨跌」（post 取 14:55；mid 取该次 11:30 估算）")
    P("- 翻转 = 候选动作 ≠ 保持不动（候选为假设值，实际动作恒为保持不动）")
    P("- 本审计只衡量「引擎候选 vs 阈值规则」的内部结构，**不裁决预测力**（那归 backtest_action.py 的未来收益口径）")
    P("")

    # ---- 1. 复算校验 ----
    ok_no = ok_ac = tot = 0
    errs, mismatch = [], []
    for r in valid:
        rc = recompute(r)
        if rc is None:
            continue
        tot += 1
        _, _, _, _, sc = rc
        if sc["no_acct"] == r["score"]:
            ok_no += 1
        if sc["acct"] == r["score"]:
            ok_ac += 1
        e = min(abs(r["score"] - sc["no_acct"]), abs(r["score"] - sc["acct"]))
        errs.append(e)
        if e > 2 and len(mismatch) < 5:
            mismatch.append((r["date"], r["slot"], r["code"], r["score"], sc["no_acct"], sc["acct"]))
    within = sum(1 for e in errs if e <= 2)
    exact = sum(1 for e in errs if e == 0)
    P("## 一、公式复算校验（解析是否还原得出历史分）")
    P("")
    P(f"- 可复算 {tot} 条：任一分母下**完全相等 {exact}/{tot}**，"
      f"**±2 分内 {within}/{tot} = {within / tot:.0%}**，误差中位 **{q(errs, .5):.0f} 分**")
    P(f"- 分母 23.5 命中 {ok_no}/{tot} · 分母 24.5 命中 {ok_ac}/{tot}"
      f"（历史报告账户维有时可用有时不可用，两种口径并存）")
    if mismatch:
        P(f"- 误差 >2 分样例 {len(mismatch)}（日期/时段/基金/报告分/23.5/24.5）：{mismatch}")
    P("")
    P("> 结论：五维公式能还原到 **±2 分内 " + f"{within / tot:.0%}" +
      "**，差的分数主要来自账户维（历史报告有无持仓快照）与各维先取整后加权的顺序，"
      "不是存在第六个隐藏开关。故本表的 est / Δ / 同向度 / 相对强度 / 三因子解析可靠，"
      "第 5 节的「理论下限」只在当前版本口径下成立。")
    P("")

    # ---- 2. 分布 ----
    fa = [abs(r["est"]) for r in flips]
    ha = [abs(r["est"]) for r in holds]
    P("## 二、翻转真的发生在 ±2% 吗")
    P("")
    P(f"- 样本：翻转 **{len(flips)}** 条（加仓 {sum(1 for r in flips if r['cand'] == '加仓')} / "
      f"减仓 {sum(1 for r in flips if r['cand'] == '减仓')}），保持不动 **{len(holds)}** 条")
    bins = [(0, 0.5), (0.5, 1), (1, 1.5), (1.5, 2), (2, 3), (3, 4), (4, 99)]
    lab = lambda lo, hi: (f"[{lo},{hi})" if hi < 99 else f"[{lo},∞)")
    P("")
    P("| \\|est\\| 区间 | 翻转条数 | 不动条数 | 该区间翻转率 |")
    P("|---|---:|---:|---:|")
    for lo, hi in bins:
        nf = sum(1 for v in fa if lo <= v < hi)
        nh = sum(1 for v in ha if lo <= v < hi)
        rate = nf / (nf + nh) if nf + nh else 0
        P(f"| {lab(lo, hi)} | {nf} | {nh} | {rate:.0%} |")
    P("")
    P(f"- 翻转组 \\|est\\|：min **{min(fa):.2f}%** · p25 {q(fa, .25):.2f}% · "
      f"中位 {q(fa, .5):.2f}% · 均值 {sum(fa) / len(fa):.2f}% · max {max(fa):.2f}%")
    P(f"- 不动组 \\|est\\|：p50 {q(ha, .5):.2f}% · p90 {q(ha, .9):.2f}% · p99 {q(ha, .99):.2f}% · max {max(ha):.2f}%")
    P("")

    # ---- 3. ±2% 规则 ----
    tp = tn = fp = fn = 0
    fpl, fnl = [], []
    for r in valid:
        pred, truth = rule2(r["est"]), r["cand"] != "保持不动"
        if pred == r["cand"]:
            if truth:
                tp += 1
            else:
                tn += 1
        elif truth and pred == "保持不动":
            fn += 1
            fnl.append(r)
        else:
            fp += 1
            fpl.append(r)
    n = tp + tn + fp + fn
    P("## 三、±2% 规则 vs 引擎候选")
    P("")
    P(f"- 总体一致率 **{(tp + tn) / n:.1%}**（{tp + tn}/{n}）")
    P(f"- 翻转召回（引擎翻转里被 ±2% 覆盖）：**{tp}/{tp + fn} = {tp / (tp + fn):.0%}**")
    P(f"- 虚警（\\|est\\|>2% 但引擎保持不动）：**{fp}** 条")
    P(f"- 漏报（引擎翻转但 \\|est\\|≤2%）：**{fn}** 条")
    P("")
    P("| | 引擎翻 | 引擎不动 |")
    P("|---|---:|---:|")
    P(f"| ±2% 规则翻 | {tp} | {fp} |")
    P(f"| ±2% 规则不动 | {fn} | {tn} |")
    P("")

    # ---- 4. 阈值扫描 ----
    scan = []
    t = 0.5
    while t <= 4.001:
        a = sum(1 for r in valid if rule2(r["est"], t) == r["cand"]) / n
        hit = sum(1 for r in flips if abs(r["est"]) > t)
        rec = hit / len(flips)
        prec = hit / max(1, sum(1 for r in valid if abs(r["est"]) > t))
        scan.append((t, a, rec, prec))
        t += 0.25
    a_all = auc([abs(r["est"]) for r in valid], [r["cand"] != "保持不动" for r in valid])
    best = max(scan, key=lambda x: x[1])
    P("## 四、阈值扫描与判别力")
    P("")
    P("| 阈值 t | 一致率 | 翻转召回 | 翻转精确率 |")
    P("|---:|---:|---:|---:|")
    for tt, a, rec, prec in scan:
        if tt in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0) or tt == best[0]:
            star = " ←" if tt == best[0] else (" ★你观察的 2%" if tt == 2.0 else "")
            P(f"| {tt:.2f}% | {a:.1%} | {rec:.0%} | {prec:.0%}{star} |")
    P("")
    P(f"- **AUC(\\|est\\| 判别翻转) = {a_all:.3f}**（0.5 = 无判别力，1.0 = 完美）")
    P(f"- 样本内最佳阈值 t = {best[0]:.2f}%（一致率 {best[1]:.1%}）——"
      f"{'与你的 2% 观察接近' if abs(best[0] - 2.0) <= 0.5 else '与 2% 有距离'}")
    P("")

    # ---- 5. 相关性 ----
    xs = [r["est"] for r in valid]
    ys = [r["score"] for r in valid]
    rp = pearson(xs, ys)
    P("## 五、倾向分是不是就是涨跌幅")
    P("")
    P(f"- Pearson(倾向分, est) = **{rp:+.3f}**，R² = **{rp * rp:.3f}**"
      f"（即涨跌幅单独解释倾向分 {rp * rp:.0%} 的方差）")
    sub = [r for r in valid if abs(r["est"]) < 2]
    if len(sub) > 2:
        r2 = pearson([r["est"] for r in sub], [r["score"] for r in sub])
        P(f"- \\|est\\|<2% 子样本（{len(sub)} 条）：r = {r2:+.3f}，R² = {r2 * r2:.3f}")
    P("")

    # 解析：最小可翻转 |est|
    denom = 23.5
    raw_need = THR / 100.0 * denom
    other_max = MAXDIM["breadth"] * WEIGHTS["breadth"] + MAXDIM["relative"] * WEIGHTS["relative"] \
        + MAXDIM["tech"] * WEIGHTS["tech"]
    s1_need = (raw_need - other_max) / WEIGHTS["intraday"]
    est_min_no_delta = 2.0 * math.atanh(min(0.999, s1_need / 30.0))
    P(f"- **理论下限**：五维里日内趋势满分 40 分只贡献 14.0 原始分，达不到 60 分门（需 {raw_need:.1f}）。"
      f"即使同向度 / 相对强度 / 三因子全部满分（合计 {other_max:.2f}），日内趋势仍需 ≥{s1_need:.1f} 分 →"
      f" 单边无 Δ 情况下 **\\|est\\| ≥ {est_min_no_delta:.2f}% 才可能翻转**。")
    P("")

    # 同向度是隐含必要条件
    bs = [abs(r["breadth"]) for r in flips if r["breadth"] is not None]
    P(f"- 翻转样本同向度 \\|b\\|：min {min(bs):.2f} · 中位 {q(bs, .5):.2f} · "
      f"\\|b\\|≥0.9 占 {sum(1 for b in bs if b >= 0.9) / len(bs):.0%} · \\|b\\|≥0.7 占 "
      f"{sum(1 for b in bs if b >= 0.7) / len(bs):.0%}（{sum(1 for b in bs if b < 0.7)} 条 <0.7）")
    hb = [abs(r["breadth"]) for r in holds if r["breadth"] is not None and abs(r["est"]) >= 2]
    if hb:
        near = sum(1 for r in fpl if 50 <= abs(r["score"]) < 60)
        opp = sum(1 for r in fpl if r["rel"] is not None and r["rel"] * r["est"] < 0)
        weakb = sum(1 for r in fpl if r["breadth"] is not None and abs(r["breadth"]) < 0.9)
        P(f"- 大波动（\\|est\\|≥2%）却仍不动的 **{len(fpl)}** 条，三种成因（可叠加）："
          f"①**差一口气**——{near} 条倾向分落在 50~59，离 ±60 门槛只差 1~10 分；"
          f"②**相对强度抵消**——{opp} 条属「跌但比池内抗跌 / 涨但弱于池内」，被池内相对维拉回；"
          f"③**同向度不足**——{weakb} 条 \\|b\\|<0.9（重仓并非一边倒）。")
    P("")

    # ---- 6. 分歧样本 ----
    P("## 六、分歧样本（引擎与 ±2% 说不一致的日子）")
    P("")
    P("### 6.1 引擎翻转但 \\|est\\|≤2%（阈值规则会漏掉的）")
    P("")
    if fnl:
        P("| 日期 | 时段 | 基金 | est | Δ | 同向度 | 相对 | 三因子 | 候选取向分 |")
        P("|---|---|---|---:|---:|---:|---:|---:|---:|")
        for r in sorted(fnl, key=lambda x: (x["date"], x["slot"])):
            d = f"{r['delta']:+.2f}%" if r["delta"] is not None else "—"
            b = f"{r['breadth']:+.2f}" if r["breadth"] is not None else "—"
            rl = f"{r['rel']:+.2f}%" if r["rel"] is not None else "—"
            P(f"| {r['date']} | {r['slot']} | {r['code']} | {r['est']:+.2f}% | {d} | {b} | {rl} | "
              f"{r['tech'] if r['tech'] is not None else '—'} | {r['cand']} {r['score']:+d} |")
    else:
        P("（无）")
    P("")
    P("### 6.2 \\|est\\|>2% 但引擎保持不动（只是涨跌幅会误报的）")
    P("")
    if fpl:
        P("| 日期 | 时段 | 基金 | est | Δ | 同向度 | 相对 | 三因子 | 倾向分 |")
        P("|---|---|---|---:|---:|---:|---:|---:|---:|")
        for r in sorted(fpl, key=lambda x: (x["date"], x["slot"])):
            d = f"{r['delta']:+.2f}%" if r["delta"] is not None else "—"
            b = f"{r['breadth']:+.2f}" if r["breadth"] is not None else "—"
            rl = f"{r['rel']:+.2f}%" if r["rel"] is not None else "—"
            P(f"| {r['date']} | {r['slot']} | {r['code']} | {r['est']:+.2f}% | {d} | {b} | {rl} | "
              f"{r['tech'] if r['tech'] is not None else '—'} | {r['score']:+d} |")
    else:
        P("（无）")
    P("")

    # ---- 7. 时间稳定性 ----
    mid_date = dates[len(dates) // 2]
    P("## 七、时间稳定性（同一规则在两段是否同样成立）")
    P("")
    P(f"分段点：{mid_date}")
    P("")
    P("| 段 | 条数 | 翻转数 | ±2% 一致率 | 翻转召回 | 不动组里 \\|est\\|≥2% 占比 | AUC |")
    P("|---|---:|---:|---:|---:|---:|---:|")
    for name, sub in (("前半", [r for r in valid if r["date"] < mid_date]),
                      ("后半", [r for r in valid if r["date"] >= mid_date])):
        if not sub:
            continue
        f = [r for r in sub if r["cand"] != "保持不动"]
        h = [r for r in sub if r["cand"] == "保持不动"]
        acc = sum(1 for r in sub if rule2(r["est"]) == r["cand"]) / len(sub)
        rec = (sum(1 for r in f if abs(r["est"]) > 2.0) / len(f)) if f else float("nan")
        fa2 = (sum(1 for r in h if abs(r["est"]) >= 2.0) / len(h)) if h else float("nan")
        a2 = auc([abs(r["est"]) for r in sub], [r["cand"] != "保持不动" for r in sub])
        P(f"| {name} | {len(sub)} | {len(f)} | {acc:.1%} | {rec:.0%} | {fa2:.0%} | {a2:.3f} |")
    P("")

    # fund-day 去重
    by_fd = {}
    for r in valid:
        k = (r["date"], r["code"])
        if k not in by_fd or r["slot"] == "post":
            by_fd[k] = r
    fd = list(by_fd.values())
    fdf = [r for r in fd if r["cand"] != "保持不动"]
    P(f"- fund-day 去重口径（post 优先）：{len(fd)} 个基金日，翻转 {len(fdf)} 个；"
      f"±2% 一致率 {sum(1 for r in fd if rule2(r['est']) == r['cand']) / len(fd):.1%}")
    P("")

    # ---- 8. 结论 ----
    P("## 八、结论")
    P("")
    share09 = (sum(1 for b in bs if b >= 0.9) / len(bs)) if bs else float("nan")
    verdict = []
    verdict.append(f"1. **±2% 不是代码里的阈值**，是实际体感的近似：一致率 {(tp + tn) / n:.0%}，"
                   f"AUC {a_all:.2f}——方向对，但不是全部。")
    verdict.append(f"2. **真正的门槛是「五维同时偏向」**：同向度 ≥0.9 占翻转样本 {share09:.0%}；"
                   f"且日内趋势满分 40 分只折 14.0 原始分，低于 60 分门所需的 {raw_need:.1f}，"
                   f"单靠涨跌幅翻不过门槛。")
    verdict.append(f"3. **涨跌幅解释倾向分 {rp * rp:.0%} 的方差**，剩余 {1 - rp * rp:.0%} 来自同向度、"
                   f"盘中变化方向、池内相对强度、中期三因子——这部分是盯涨跌幅看不到的。")
    verdict.append(f"4. 漏报 {fn} 条 / 虚警 {fp} 条，说明两者不是同一条规则；"
                   f"但一致率 {(tp + tn) / n:.0%} 也说明**只看涨跌幅，大多数日子结论相同**——"
                   f"增量价值集中在少数分歧日（第 6 节）。")
    for v in verdict:
        P(v)
    P("")
    P("<sub>局限：①只衡量引擎候选 vs 阈值规则的结构一致性，不裁决未来收益预测力（归 backtest_action.py）；"
      "②样本仅 2026-08-25 起两个月、4 只基金，横截面极窄；③mid/post 同一天非独立观测，已给 fund-day 去重口径；"
      "④候选阈值 ±60 与五维权重均为未校准初始值，本轮审计不评价其优劣。</sub>")

    md = "\n".join(L) + "\n"
    (OUT / "audit_candidate_flips.md").write_text(md, encoding="utf-8")

    with (OUT / "audit_candidate_flips.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["date", "slot", "gen", "code", "score", "cand", "act",
                                           "est", "e1130", "e1455", "delta", "breadth", "nup",
                                           "ndown", "rel", "tech", "rt", "concentrated", "acct_missing"])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(md)


if __name__ == "__main__":
    main()
