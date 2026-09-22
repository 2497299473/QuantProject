#!/usr/bin/env python3
"""T+5 专项证据评分卡（2026-09-01，GPT 五审 ⑲ backtest_forecast_t5.py）。

回答：「T+5 的 alpha 到底够不够格往 Policy 推？」把方向、校准、分位数、
时间稳定、跨基金泛化、regime 分化、经济价值十项证据整合成一张卡。

预注册裁决（写死，跑之前定）：
  scorecard_verdict(pooled_ci_lo, recent_ic):
    pooled_ci_lo > 0 且 recent_ic > 0  → "stable"   （稳定，可向 Summer 提分周期 ready 方案）
    pooled_ci_lo > 0 且 recent_ic <= 0 → "partial"  （部分：pooled 显著但最近窗失效 → 维持 false，观察）
    pooled_ci_lo <= 0                  → "fail"     （不成立 → 维持 false）
  recent_ic 取 Rolling OOS 最近窗 T+5 RankIC（与 WF 最近折一致时相互印证）。

证据来源（单一事实源，不重跑）：
  - ①⑤ 脚本内重算（T+5 冻结模型，口径同 backtest_forecast：build_xy 双列 14 维 + HGB）
  - ⑥  解析 output/backtest_walk_forward_20260901.md
  - ⑦⑧ 解析 output/backtest_quantile_calib_20260901.md
  - ⑨  直读 data/model_registry/lofo_evidence.json
  - ⑩  Drift(PSI/KS) 挂 09-26 重估，本卡标注"待做"

用法：python3 backtest_t5_scorecard.py [--n-boot 199] [--window-days 63] [--force]
输出：output/backtest_t5_scorecard_YYYYMMDD.md + .log
证据新鲜度：生成前校验 WF/calib/lofo 源 mtime，源晚于现有报告 → STALE_EVIDENCE 拒绝（--force 覆盖）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import numpy as np

from backtest_spread import load_samples
from frozen_dataset import resolve_samples   # V4.3 P0-1：统一冻结样本入口
from backtest_forecast import (split_date_oos, build_xy, rank_ic,
                               brier_multiclass, calibration_curve,
                               cluster_bootstrap_ci)
from experiment_state_feature import add_state_features


def scorecard_verdict(pooled_ci_lo: float, recent_ic: float) -> str:
    """预注册三档裁决。"""
    if pooled_ci_lo <= 0:
        return "fail"
    if recent_ic > 0:
        return "stable"
    return "partial"


def _window_slices(dates: list[str], window_days: int) -> list[list[str]]:
    """OOS 日期序列按交易日等分窗口（与 backtest_rolling_oos 同口径）。"""
    uniq = sorted(set(dates))
    n = len(uniq)
    if n == 0:
        return []
    k = max(1, n // window_days)
    out = []
    for i in range(k):
        lo = i * n // k
        hi = (i + 1) * n // k if i < k - 1 else n
        out.append(uniq[lo:hi])
    return out


def _rank_ic_metric(sub: dict) -> float:
    return rank_ic(sub["p"].tolist(), sub["y"].tolist())


def _economic_value(p: np.ndarray, y: np.ndarray) -> dict:
    """经济价值：p_up 排序 top40% vs bottom40% 的 fwd5 方向差。"""
    n = len(p)
    if n < 50:
        return {"top_bottom_spread": 0.0, "n_top": 0, "n_bottom": 0}
    k = max(1, int(n * 0.4))
    order = np.argsort(p)
    bottom = order[:k]
    top = order[-k:]
    spread = float(y[top].mean() - y[bottom].mean())
    return {"top_bottom_spread": spread, "n_top": int(k), "n_bottom": int(k)}


def _parse_md_table_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _read_wf_evidence(md_path: Path) -> dict:
    """从 walk_forward md 解析 pooled WF + 逐折。

    2026-09-22 列数守卫（离线冒烟抽到）：WF md 现含四张表，`| T+5 |` 与 `| 4 |`
    两个前缀在**不同表里重复出现**——只按前缀匹配会把 CQR 覆盖率行（cells[1]=
    "84.2%"）当 IC 行直接 ValueError 崩卡，也会把 Path-WF 折行（11 列）的 μ/σ
    当成 wf_ic/frozen_ic 静默错解析。改按列数认表（与 _read_calib_evidence 同风格）。
    """
    out = {"pooled_ic": None, "pooled_ci": None, "folds": []}
    for line in md_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("| T+5 |"):
            cells = _parse_md_table_row(line)
            if len(cells) != 4:
                continue        # 主判据表 4 列；CQR 覆盖率表 7 列同前缀 → 跳过
            # | T+5 | +0.080 | [+0.020, +0.140] | -0.035 |
            out["pooled_ic"] = float(cells[1])
            m = re.search(r"\[([+-]?[\d.]+),\s*\+?([\d.]+)\]", cells[2])
            if m:
                out["pooled_ci"] = (float(m.group(1)), float(m.group(2)))
        if line.startswith("| 4 |"):
            cells = _parse_md_table_row(line)
            if len(cells) != 6:
                continue        # 逐折表 6 列；Path-WF 折表 11 列同前缀 → 跳过
            # | 4 | 2026-04-13 ~ 2026-08-03 | 3069 | -0.035 | -0.012 | -0.023 |
            out["folds"].append({"fold": 4, "window": cells[1],
                                 "wf_ic": float(cells[3]), "frozen_ic": float(cells[4])})
    return out


def _read_calib_evidence(md_path: Path) -> dict:
    """从 quantile_calib md 解析 T+5 Coverage80 + 路径层。"""
    out = {"coverage80": None, "coverage_ci": None, "q50_ic": None,
           "pinball_skill": None, "mdd_q10_hit": None, "mfe_q50_hit": None}
    for line in md_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("| T+5 |"):
            cells = _parse_md_table_row(line)
            if len(cells) < 7:
                continue        # CQR 表 T+5 行（5 列）非主覆盖表（8 列），跳过
            # | T+5 | 907 | 57.4% | [54.8%, 60.9%] | 0.1056 | +0.00254 | +0.025 [-0.036,+0.085] | ⚠️ ...
            out["coverage80"] = float(cells[2].rstrip("%"))
            m = re.search(r"\[([\d.]+%),\s*([\d.]+%)\]", cells[3])
            if m:
                out["coverage_ci"] = (float(m.group(1).rstrip("%")),
                                      float(m.group(2).rstrip("%")))
            out["pinball_skill"] = float(cells[5])
            m2 = re.search(r"([+-][\d.]+)\s*\[", cells[6])
            if m2:
                out["q50_ic"] = float(m2.group(1))
        if "mdd_q10" in line:
            m = re.search(r"命中率 = ([\d.]+)%", line)
            if m:
                out["mdd_q10_hit"] = float(m.group(1))
        if "mfe_q50" in line:
            m = re.search(r"命中率 = ([\d.]+)%", line)
            if m:
                out["mfe_q50_hit"] = float(m.group(1))
    return out


def _sha12(path: Path) -> str:
    """文件 sha256 前 12 位（审计用）。"""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return "unreadable"


def _evidence_freshness(sources: list[tuple[str, Path]], out_md: Path) -> tuple[bool, list[str]]:
    """STALE_EVIDENCE 门禁：任一证据源 mtime 晚于现有评分卡 → 拒绝生成（除非 --force）。

    返回 (ok, stale_labels)。out_md 不存在时（首次生成）放行。
    """
    if not out_md.exists():
        return True, []
    out_t = out_md.stat().st_mtime
    stale = [label for label, p in sources if p.exists() and p.stat().st_mtime > out_t]
    return (len(stale) == 0), stale


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-boot", type=int, default=199)
    ap.add_argument("--window-days", type=int, default=63)
    ap.add_argument("--force", action="store_true",
                    help="证据源晚于现有报告时仍强制生成（STALE_EVIDENCE 覆盖）")
    ap.add_argument("--snapshot", default=None,
                    help="冻结样本 jsonl（默认自动选最新 forecast_outputs/samples_frozen_*.jsonl）")
    ap.add_argument("--fresh", action="store_true",
                    help="显式活拉样本（数字与冻结基线不可比；报告标 FRESH）")
    args = ap.parse_args()

    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    fc = cfg.get("forecast", {})
    flat_margin = fc.get("prob_flat_margin", 0.003)
    t0 = time.time()

    print("== [0] 加载样本 ==")
    samples, snap_info = resolve_samples(args.snapshot, args.fresh, BASE_DIR, load_samples)
    if snap_info["mode"] in ("MISSING", "INVALID"):
        return 4
    samples_sorted = sorted(samples, key=lambda s: (s["date"], s["fund"]))
    print(f"  总样本 {len(samples_sorted)}")

    train, oos, oos_start = split_date_oos(samples_sorted)
    print(f"== [1] 冻结切分：train < {oos_start}（{len(train)}），OOS ≥ {oos_start}（{len(oos)}）==")

    # ---- T+5 冻结模型（口径同 backtest_forecast）----
    print("== [2] 训练 T+5 冻结模型 ==")
    from sklearn.ensemble import HistGradientBoostingClassifier
    XY = build_xy(train, 5, flat_margin)
    Xtr, ytr, yret_tr = XY
    clf = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.08,
                                         max_depth=3, early_stopping=True,
                                         random_state=42)
    clf.fit(Xtr, ytr)
    XYo = build_xy(oos, 5, flat_margin)
    Xoo, yoo, yret_oo = XYo
    p_up = clf.predict_proba(Xoo)[:, 2]
    dates_oo = XYo.dates
    print(f"  OOS n={len(yoo)} / {len(set(dates_oo))} 日")

    # ① 方向排序 pooled RankIC + cluster CI
    print("== [3] ① 方向排序（p_up vs fwd5）==")
    ric = rank_ic(p_up.tolist(), yret_oo.tolist())
    arrays = {"p": p_up, "y": yret_oo}
    lo, hi = cluster_bootstrap_ci(_rank_ic_metric, arrays, dates_oo, n_boot=args.n_boot)
    print(f"  T+5 RankIC={ric:+.4f}  CI=[{lo:+.4f}, {hi:+.4f}]")

    # ② Rolling 分窗轨迹
    print("== [4] ② Rolling 分窗（最近窗 = 裁决 recent_ic）==")
    oos_dates = sorted(set(dates_oo))
    windows = _window_slices(oos_dates, args.window_days)
    win_ics = []
    for w in windows:
        idx = [i for i, d in enumerate(dates_oo) if d in set(w)]
        ic = rank_ic(p_up[idx].tolist(), yret_oo[idx].tolist())
        win_ics.append({"window": f"{w[0]} ~ {w[-1]}", "n": len(idx), "rank_ic": ic})
        print(f"  {w[0]} ~ {w[-1]}  n={len(idx)}  RankIC={ic:+.4f}")
    recent_ic = win_ics[-1]["rank_ic"] if win_ics else 0.0

    # ③ 概率校准 ACE
    print("== [5] ③ 概率校准（p_up vs up 标签）==")
    y_bool = (yoo == 2).astype(float)
    cv = calibration_curve(y_bool, p_up, nbins=5)
    ace = float(cv["ace"])
    brier = float(np.mean((y_bool - p_up) ** 2))   # 二值 Brier（up vs 非up），诚实口径
    print(f"  ACE={ace:.4f}  Brier(up/非up 二值化)={brier:.4f}")

    # ④ State 条件化（regime 分化）
    print("== [6] ④ State 条件化（state_trend 分组 IC）==")
    oos_st = add_state_features(oos)
    groups: dict[str, list] = {}
    for s, p_, y_ in zip(oos_st, p_up.tolist(), yret_oo.tolist()):
        g = {1: "up", -1: "down"}.get(s.get("state_trend", 0), "consolidation")
        groups.setdefault(g, []).append((p_, y_))
    state_rows = []
    for g in ("up", "consolidation", "down"):
        rows = groups.get(g, [])
        if len(rows) < 20:
            state_rows.append({"state": g, "n": len(rows), "rank_ic": None})
            continue
        ic = rank_ic([r[0] for r in rows], [r[1] for r in rows])
        state_rows.append({"state": g, "n": len(rows), "rank_ic": ic})
        print(f"  state={g:<14} n={len(rows):>4}  RankIC={ic:+.4f}")

    # ⑤ 经济价值
    print("== [7] ⑤ 经济价值（p_up 分组方向差）==")
    ev = _economic_value(p_up, yret_oo)
    print(f"  top40 - bottom40 fwd5 = {ev['top_bottom_spread']:+.4f}（n={ev['n_top']}）")

    # ⑥⑦⑧ 读留档（STALE_EVIDENCE 门禁先行：mtime 检查廉价，坏/新格式证据不应导致崩溃）
    print("== [8] ⑥⑦⑧ 读今日留档（STALE_EVIDENCE 门禁先行）==")
    out_md = BASE_DIR / "output" / f"backtest_t5_scorecard_{time.strftime('%Y%m%d')}.md"
    ev_sources = [("WF md", BASE_DIR / "output" / "backtest_walk_forward_20260901.md"),
                  ("calib md", BASE_DIR / "output" / "backtest_quantile_calib_20260901.md"),
                  ("lofo json", BASE_DIR / "data" / "model_registry" / "lofo_evidence.json")]
    fresh, stale = _evidence_freshness(ev_sources, out_md)
    if not fresh and not args.force:
        print(f"[STALE_EVIDENCE] 证据源晚于现有评分卡：{stale} → 拒绝生成。"
              f"确认已重跑证据后加 --force 覆盖")
        return 2
    ev_rows = [(label, p) for label, p in ev_sources if p.exists()]

    wf = _read_wf_evidence(BASE_DIR / "output" / "backtest_walk_forward_20260901.md")
    cal = _read_calib_evidence(BASE_DIR / "output" / "backtest_quantile_calib_20260901.md")
    lofo = json.loads((BASE_DIR / "data" / "model_registry" / "lofo_evidence.json")
                      .read_text(encoding="utf-8"))
    funds = lofo.get("funds", {})
    lofo_stab = {k: v.get("stability") for k, v in funds.items()}
    lofo_status = str(lofo.get("status", "unknown"))   # 2026-09-22 LOFO STALE
    print(f"  WF pooled IC={wf['pooled_ic']}  CI={wf['pooled_ci']}  "
          f"fold4 WF={[f['wf_ic'] for f in wf['folds']]}")
    print(f"  Coverage80={cal['coverage80']}%  mdd_q10_hit={cal['mdd_q10_hit']}%  "
          f"mfe_q50_hit={cal['mfe_q50_hit']}%")
    print(f"  LOFO stability={lofo_stab}")

    verdict = scorecard_verdict(hi if False else lo, recent_ic)  # pooled CI 下界
    print(f"== [9] 裁决：scorecard_verdict(lo={lo:+.4f}, recent_ic={recent_ic:+.4f}) = {verdict} ==")

    # 报告
    lines = [
        "# T+5 专项证据评分卡（2026-09-01，GPT 五审 ⑲）", "",
        snap_info["report_line"],
        f"> 生成：{time.strftime('%Y-%m-%d %H:%M')} · train < {oos_start}（{len(train)}）· "
        f"OOS ≥ {oos_start}（{len(oos)} / {len(set(dates_oo))} 日）· bootstrap {args.n_boot} 次", "",
        "**预注册裁决：`scorecard_verdict(lo, recent_ic)` — pooled CI 下界 >0 且最近窗 >0 → stable；"
        ">0 且最近窗 ≤0 → partial；≤0 → fail**", "",
        "## 十项证据", "",
        "| # | 证据 | 值 | 判定 | 来源 |",
        "|---|---:|---|---|---|",
        f"| ① | 方向排序 RankIC | {ric:+.4f} CI=[{lo:+.4f},{hi:+.4f}] | "
        f"{'✅ 显著' if lo > 0 else '❌ 跨零'} | 本卡重算 |",
        f"| ② | Rolling 最近窗 RankIC | {recent_ic:+.4f} | "
        f"{'✅ 正' if recent_ic > 0 else '⚠️ ≤0（衰减）'} | 本卡重算 |",
        f"| ③ | 概率校准 ACE | {ace:.4f} | {'✅' if ace < 0.05 else '⚠️'} | 本卡重算 |",
        "| ④ | State 条件化 | " + "；".join(
            (f"{r['state']}:n/a(n={r['n']})" if r['rank_ic'] is None
             else f"{r['state']}:{r['rank_ic']:+.4f}(n={r['n']})")
            for r in state_rows) + " | 观察 | 本卡重算 |",
        f"| ⑤ | 经济价值 top40−bottom40 fwd5 | {ev['top_bottom_spread']:+.4f} | "
        f"{'✅ 有方向' if ev['top_bottom_spread'] > 0.01 else '⚠️ 弱'} | 本卡重算 |",
        f"| ⑥ | WF pooled RankIC | {wf['pooled_ic']} CI={wf['pooled_ci']} | "
        f"{'✅ 显著' if wf['pooled_ci'] and wf['pooled_ci'][0] > 0 else '❌'} | walk_forward md |",
        f"| ⑦ | Coverage80（名义 80%） | {cal['coverage80']}% CI={cal['coverage_ci']} | "
        f"{'❌ 过窄' if cal['coverage80'] and cal['coverage80'] < 70 else '✅'} | calib md |",
        f"| ⑧ | 路径 MDD/MFE 校准 | mdd_q10 命中 {cal['mdd_q10_hit']}% / mfe_q50 命中 {cal['mfe_q50_hit']}% "
        f"（期望 10%/50%） | ⚠️ 两端低估 | calib md |",
        f"| ⑨ | LOFO stability | {json.dumps(lofo_stab, ensure_ascii=False)} | "
        f"{'⚠️ STALE（09-26 须重跑）' if lofo_status == 'STALE' else ('⚠️ 未全池稳定' if any(v and v < 0.7 for v in lofo_stab.values()) else '✅')} | lofo json (status={lofo_status}) |",
        "| ⑩ | Drift（PSI/KS） | 待 09-26 重估 | — | — |", "",
        "## 证据新鲜度（STALE_EVIDENCE 门禁）", "",
        "| 来源 | 生成时间 | sha256(前12) |",
        "|---|---:|---|",
        *[f"| {label} | {time.strftime('%Y-%m-%d %H:%M', time.localtime(p.stat().st_mtime))} "
          f"| {_sha12(p)} |" for label, p in ev_rows],
        "",
        "## 裁决", "", 
        f"**{verdict.upper()}** —— pooled CI 下界 {lo:+.4f}（{'>0' if lo > 0 else '≤0'}），"
        f"最近窗 {recent_ic:+.4f}（{'正' if recent_ic > 0 else '≤0'}）", "",
        "### 解读", "",
        {
            "stable": "- 所有关键证据支持 T+5 稳定 → 可向 Summer 提「分周期 ready 位」方案",
            "partial": "- pooled 显著但最近窗失效（与 WF 结论一致：regime 变化）\n"
                       "- 区间过窄 + 路径两端低估 = 风险分布不可信，Q10/Q90 仅观察参考\n"
                       "- **维持 model_ready=false；09-26 重估看最近窗是否转正**",
            "fail": "- 方向证据不成立 → 维持 model_ready=false，停止 T+5 方向推进",
        }[verdict], "",
        "<sub>口径：T+5 冻结模型（build_xy 14 维 B1 双列 + HGB 同 backtest_forecast）；②④ 无 bootstrap 仅点估计；"
        "⑥⑦⑧ 解析自 09-01 已落档 md（单一事实源，不重跑）；⑨ 直读 lofo_evidence.json；⑩ 挂 09-26。</sub>",
    ]
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[ok] 报告 → {out_md}")
    print(f"[done] 耗时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
