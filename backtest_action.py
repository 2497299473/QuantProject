#!/usr/bin/env python3
"""动作收益回测：加仓/减仓/不动 vs 不动（GPT 第十七节 · v4 决策层证据工具）。

目标：回答「今天 14:55 若按决策倾向执行动作，相对『不动』是否更优」。
这是启用动作层（config.decision.gates.history_validated=true）的唯一证据来源。

口径（诚实声明，2026-08-25 定）：
1. 无历史盘中 11:30/14:55 快照 → 以【日线收盘】近似 14:55 时点（既有约定）；
   11:30 基线无法历史回测 → close_phase_change 维度缺失（与 backtest_fusion 同局限）
2. 防前视：t 日持仓 = 披露生效日 ≤ t 的最近季报快照（lookthrough.effective_snapshot）
3. 动作优势：ADD 桶 = fwd10（新增资金吃到的未来10日收益）；REDUCE 桶 = -fwd10
   （规避掉的未来10日涨跌）；HOLD = 0。比较对象就是「不动」本身，故不扣基准
4. 账户维未回测（holdings.json 只有当下持仓、无历史流水）→ 回测传 account_state=None，
   引擎按「账户维不可用」处理（2026-09-22 评审修复②）：不参与加权和、从归一化分母
   移除（默认权重 24.5→23.5）；live 引擎对持仓基金含 account 维（±10 分），属已知差异
   （旧报告 2026-08-25 曾把 None 误解为空仓，候选分含幽灵 +4 ≈ +1.63 倾向分，已重跑替换）
5. 权重与阈值 = config.decision 当前值（GPT 建议框架初始值）

判定标准（事先写死）：
- A. ADD 桶平均优势 > 0 且 bootstrap（按交易日聚类，B 契约 §8）95% CI 下界 > 0
- B. REDUCE 桶平均优势 > 0 且 CI 下界 > 0
- C. 前后两半 ADD 优势方向一致（均为 >0，防时段依赖）
- D. 阈值区分度：倾向分 ≥60 桶优势 > 中间桶（-20~20）
A+B+C+D 全过 → 可人工评估把 history_validated 置 true（仍需复核措辞红线）
否则 → 维持观察层，本报告留档为证据。

用法：python3 backtest_action.py
"""
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from backtest_forecast import cluster_bootstrap_ci   # B 契约 §8：唯一 bootstrap 实现，禁止另造
from backtest_spread import load_samples   # 复用样本构建（含缓存、防前视）
from frozen_dataset import resolve_samples   # V4.3 P0-1：统一冻结样本入口
from core import decision_engine


def bootstrap_ci(vals, dates, n_boot=999, seed=42):
    """ADD/REDUCE 桶优势 95% CI —— B 契约 §8（2026-09-23）：统一按交易日聚类重抽样。

    旧实现逐基金日行独立 bootstrap（rng.choice 单条重抽），把同日多基金当成
    独立样本，会高估有效样本量、CI 偏乐观——与 Forecast 层已建立的按日聚类
    纪律不一致。现复用 backtest_forecast.cluster_bootstrap_ci（按日整块重抽），
    不另造第二套 bootstrap。

    vals/dates 严格同序。交易日 <10 或重抽统计不足 → (nan, nan)：调用方
    「CI 下界 > 0」判定对 nan 恒 False，自然 fail-closed（诚实不足，不造 0.0）。
    返回口径与旧实现一致：fractions（bucket() 内再 ×100 转百分比）。
    """
    lo, hi = cluster_bootstrap_ci(
        lambda sub: float(np.mean(sub["v"])),
        {"v": np.asarray(vals, dtype=float)},
        list(dates), n_boot=n_boot, seed=seed)
    return lo, hi


def fmt_b(b):
    if not b:
        return "— | — | — | —"
    lo, hi = b["ci"]
    ci_txt = (f"[{lo:+.2f}%, {hi:+.2f}%]" if lo == lo
              else "n/a（交易日不足，fail-closed）")
    return f"{b['n']} | {b['mean']:+.2f}% | {b['win']:.0f}% | {ci_txt}"


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=None,
                    help="冻结样本 jsonl（默认自动选最新 forecast_outputs/samples_frozen_*.jsonl）")
    ap.add_argument("--fresh", action="store_true",
                    help="显式活拉样本（数字与冻结基线不可比；报告标 FRESH）")
    args = ap.parse_args()
    samples, snap_info = resolve_samples(args.snapshot, args.fresh, BASE_DIR, load_samples)
    if snap_info["mode"] in ("MISSING", "INVALID"):
        return 4
    if not samples:
        print("[fail] 无样本")
        return 1

    # 池内当日实时估算（转百分比，对齐 live 口径）
    pool_by_date: dict[str, dict[str, float]] = defaultdict(dict)
    for s in samples:
        pool_by_date[s["date"]][s["fund"]] = s["est_chg"] * 100.0

    # 逐日重放 v4 决策倾向（account 维缺席；11:30 基线缺席）
    rows = []
    for s in samples:
        feat = {
            "est_return": s["est_chg"] * 100.0,
            "breadth": s.get("breadth"),
            "concentration": s.get("concentration"),
            "covered_pct": s.get("covered_pct") or 0.0,
            "holdings_age_days": 0,
        }
        inp = decision_engine.DecisionInput(
            code=s["fund"], name=s["fund"], slot="post",
            technical_score=s["score"],
            feat_1130=None, feat_1455=feat,
            pool_est=pool_by_date.get(s["date"], {}),
            account_state=None,
        )
        d = decision_engine.evaluate(inp)
        adv = s["fwd10"] if d.candidate == "ADD" else (-s["fwd10"] if d.candidate == "REDUCE" else 0.0)
        rows.append({"fund": s["fund"], "date": s["date"], "candidate": d.candidate,
                     "score": d.score, "adv": adv, "fwd10": s["fwd10"]})

    rows.sort(key=lambda r: r["date"])
    mid = rows[len(rows) // 2]["date"]

    def bucket(cand):
        sel = [r for r in rows if r["candidate"] == cand]
        if not sel:
            return None
        advs = [r["adv"] for r in sel]
        mean = sum(advs) / len(advs) * 100
        lo, hi = bootstrap_ci(advs, [r["date"] for r in sel])
        win = sum(1 for a in advs if a > 0) / len(advs) * 100
        return {"n": len(sel), "mean": mean, "win": win, "ci": (lo * 100, hi * 100)}

    add_b, red_b, hold_b = bucket("ADD"), bucket("REDUCE"), bucket("HOLD")
    a_pass = bool(add_b and add_b["mean"] > 0 and add_b["ci"][0] > 0)
    b_pass = bool(red_b and red_b["mean"] > 0 and red_b["ci"][0] > 0)

    halves = {"前半": [r for r in rows if r["date"] < mid],
              "后半": [r for r in rows if r["date"] >= mid]}
    half_add = {}
    for name, rs in halves.items():
        sel = [r for r in rs if r["candidate"] == "ADD"]
        half_add[name] = (sum(r["adv"] for r in sel) / len(sel) * 100) if sel else None
    c_pass = (half_add["前半"] is not None and half_add["后半"] is not None
              and half_add["前半"] > 0 and half_add["后半"] > 0)

    hi_b = [r for r in rows if r["score"] >= 60]
    mid_b = [r for r in rows if -20 <= r["score"] <= 20]
    hi_mean = sum(r["adv"] for r in hi_b) / len(hi_b) * 100 if hi_b else None
    mid_mean = sum(r["adv"] for r in mid_b) / len(mid_b) * 100 if mid_b else None
    d_pass = hi_mean is not None and mid_mean is not None and hi_mean > mid_mean

    verdict = a_pass and b_pass and c_pass and d_pass
    verdict_line = (
        "✅ 通过 —— 可人工评估启用动作层（history_validated=true，仍需复核措辞红线）"
        if verdict else
        "❌ 未通过 —— 动作层继续锁死（history_validated=false），维持观察层。"
        "本报告与 2026-08-25 三轮回测裁决互为印证：当前信号组合无稳定动作 alpha。")

    # 分基金 / 分年（ADD 桶）
    by_fund = {}
    for c in sorted({r["fund"] for r in rows}):
        sel = [r for r in rows if r["fund"] == c and r["candidate"] == "ADD"]
        by_fund[c] = (sum(r["adv"] for r in sel) / len(sel) * 100) if sel else None
    by_year = {}
    for y in sorted({r["date"][:4] for r in rows}):
        sel = [r for r in rows if r["date"][:4] == y and r["candidate"] == "ADD"]
        by_year[y] = (sum(r["adv"] for r in sel) / len(sel) * 100) if sel else None

    lines = [
        "# 动作收益回测：加仓/减仓/不动 vs 不动", "",
        snap_info["report_line"],
        f"> 生成：{datetime.now():%Y-%m-%d %H:%M} · 样本（基金日）{len(rows)} · "
        f"区间 {rows[0]['date']} ~ {rows[-1]['date']} · 分段点 {mid}", "",
        "## 〇、口径（诚实声明）", "",
        "1. 以日线收盘近似 14:55 时点（免费接口无历史分时）；**11:30 基线无法历史回测** → 变化量维度缺失",
        "2. 防前视：t 日持仓 = 披露生效日 ≤ t 的最近季报快照",
        "3. 动作优势：ADD=fwd10（新增资金收益）；REDUCE=-fwd10（规避的涨跌）；HOLD=0 —— 比较对象即「不动」",
        "4. 账户维未回测（无历史持仓流水）：account_state=None = 维度不可用，不参与加权和（从分母移除）；",
        "   live 引擎对持仓基金含账户维（±10），属已知差异",
        "5. 权重/阈值 = config.decision 当前值（GPT 建议框架初始值，未经校准）",
        "6. （2026-09-22 重跑）旧报告（2026-08-25）曾把 account_state=None 误解为空仓，候选分含幽灵 +4（≈+1.63 倾向分，",
        "   恒定偏移：不改相对排序，但影响 ±60 穿越与桶归属）；本报告改用「维度不可用」语义",
        "7. （2026-09-23 B 契约 §8）CI 改按交易日聚类重抽样（复用 Forecast 层唯一实现）：",
        "   同日多基金=同一横截面整块重抽；交易日 <10 → CI 记 n/a，A/B 判定自然 fail-closed",
        "",
        "## 一、判定标准（事先写死）", "",
        "- **A** ADD 桶平均优势 > 0 且 bootstrap（按交易日聚类，B 契约 §8）95% CI 下界 > 0",
        "- **B** REDUCE 桶平均优势 > 0 且 CI 下界 > 0",
        "- **C** 前后两半 ADD 优势方向一致（均 >0，防时段依赖）",
        "- **D** 倾向分 ≥60 桶优势 > 中间桶（-20~20）",
        "",
        "## 二、分桶结果", "",
        "| 候选 | 样本 | 平均优势 | 胜率 | 95% CI |",
        "|---|---:|---:|---:|---:|",
        f"| 加仓(ADD) | {fmt_b(add_b)} |",
        f"| 减仓(REDUCE) | {fmt_b(red_b)} |",
        f"| 不动(HOLD) | {fmt_b(hold_b)} |",
        "",
        f"**判定 A**（ADD 优势>0 且 CI 下界>0）：{'✅' if a_pass else '❌'}",
        f"**判定 B**（REDUCE 优势>0 且 CI 下界>0）：{'✅' if b_pass else '❌'}",
        "",
        "## 三、时间分段（ADD 桶）", "",
        "| 段 | ADD 平均优势 |", "|---|---:|",
    ]
    lines.append(f"| 前半 | {half_add['前半']:+.2f}% |" if half_add["前半"] is not None else "| 前半 | 样本不足 |")
    lines.append(f"| 后半 | {half_add['后半']:+.2f}% |" if half_add["后半"] is not None else "| 后半 | 样本不足 |")
    lines += ["", f"**判定 C**（两半 ADD 优势均 >0）：{'✅' if c_pass else '❌'}",
              "", "## 四、阈值区分度", "", "| 桶 | 平均优势 |", "|---|---:|"]
    lines.append(f"| 倾向分 ≥60 | {hi_mean:+.2f}% |" if hi_mean is not None else "| 倾向分 ≥60 | 样本不足 |")
    lines.append(f"| 倾向分 -20~20 | {mid_mean:+.2f}% |" if mid_mean is not None else "| 倾向分 -20~20 | 样本不足 |")
    lines += ["", f"**判定 D**（高分桶 > 中间桶）：{'✅' if d_pass else '❌'}",
              "", "## 五、分基金 / 分年（ADD 桶）", "", "| 基金 | ADD 优势 |", "|---|---:|"]
    for c, v in by_fund.items():
        lines.append(f"| {c} | {v:+.2f}% |" if v is not None else f"| {c} | 样本不足 |")
    lines += ["", "| 年份 | ADD 优势 |", "|---|---:|"]
    for y, v in by_year.items():
        lines.append(f"| {y} | {v:+.2f}% |" if v is not None else f"| {y} | 样本不足 |")
    lines += ["", "## 六、结论", "", f"**{verdict_line}**", "",
              "<sub>局限：①11:30 午盘未回测（无历史分时）；②持仓季报滞后 1~3 个月；"
              "③账户维不可用（不参与评分，从分母移除）；④前复权 K 线按当日口径回放（轻微幸存者偏差已知）；⑤未含申赎费用。</sub>"]

    report = "\n".join(lines)
    out = BASE_DIR / "output" / "backtest_action_report.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n[done] 报告 → {out}")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
