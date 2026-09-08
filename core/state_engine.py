"""State Engine v0（Phase 2 骨架，2026-08-27）：缠论结构状态的因果编码与转移统计。

定位（对齐 2026-08-27 OOS 双重否决后的路线）：
- 旧特征组（est_chg/breadth/composite/score...）OOS 无方向 Alpha（T+1/3/5 RankIC 均负）；
  本模块不修补旧特征，而是换轨道——用「当前处于什么结构状态」做条件分布，
  输出真实版状态转移矩阵（对应评审建议第十二节，数字来自历史统计而非拍脑袋）。

状态编码 v0.1（2026-08-27 起）：state = "<trend>|<pivot_pos>[~<bucket>][|<recent_event>]"
    - trend ∈ {up, down, consolidation, expand, na}     ← core/chanlun.analyze()
    - pivot_pos ∈ {above, inside, below, none}          ← 净值相对最后中枢 [ZD,ZG]
    - ~bucket ∈ {fresh, stale}（v0.1 新增，仅 above/below 有）：
      截至当日的连续枢外交易日数 ≤ PIVOT_AWAY_FRESH_DAYS 记 fresh。
      背景：down|above 混态诊断为「结构尺度(trend) × 价格尺度(pos)」双时间尺度
      叠加——刚反弹离枢与高位盘距枢已久混在同一状态，分桶后拆开检验。

已知简化（诚实记录，Phase 2 后续迭代项）：
- 结构序列用基金净值合成日K（open=前一净值，high/low=max/min），非穿透组合加权指数；
- 单级别日线；无区间套/多级别联立（沿用 chanlun.py 同款口径）；
- analyze() 对每个样本日前缀重放，O(n²)——池内最大 ~2600 根，全池实测约 1~2 分钟。

因果性保证（可被 tests/test_state_engine.py 的防前视测试验证）：
任何日期的状态只由该日及之前的数据决定；篡改未来数据不改变过去状态。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from . import chanlun as cl

BASE_DIR = Path(__file__).resolve().parent.parent

STATE_VERSION = 2        # 编码口径版本：字段含义或窗口变更时 +1（v2 加距离分桶）
PIVOT_AWAY_FRESH_DAYS = 10   # 枢外连续天数 ≤ 此值视为 fresh（2026-08-27 定标）
WARMUP_BARS = 60         # analyze() 要求最少 K 线数
EVENT_WINDOW = 10        # 「最近事件」回看窗口（交易日）
DEFAULT_HORIZONS = (1, 3, 5)


# ---------------------------------------------------------------- 合成日K与状态

def synth_klines(navs: list[tuple[str, float]]) -> list[tuple[str, float, float, float, float]]:
    """净值 → 缠论输入格式 (date, open, close, high, low)。

    已知简化：单点净值摊成开收高低四价。merge_inclusion/fractal 依赖 high/low，
    这里以相邻净值的极值充当，等价于把 NAV 锯齿当作结构源。
    """
    out = []
    for i, (d, v) in enumerate(navs):
        o = navs[i - 1][1] if i > 0 else v
        out.append((d, o, v, max(o, v), min(o, v)))
    return out


def _distance_bucket(navs: list[tuple[str, float]], i: int, pos: str,
                     zg: float | None, zd: float | None,
                     fresh_max: int = PIVOT_AWAY_FRESH_DAYS) -> tuple[int, str | None]:
    """净值截至第 i 日连续枢外的交易日数（含当日）及其分桶。

    - above: 连续 nav>ZG；below: 连续 nav<ZD；inside/none → (0, None)
    - fresh: away <= fresh_max；否则 stale。因果：只用 navs[:i+1]。
    """
    if pos == "above":
        thr = zg
    elif pos == "below":
        thr = zd
    else:
        return 0, None
    j = i
    if pos == "above":
        while j >= 0 and navs[j][1] > thr:
            j -= 1
    else:
        while j >= 0 and navs[j][1] < thr:
            j -= 1
    away = i - j
    return away, ("fresh" if away <= fresh_max else "stale")


def state_at(navs: list[tuple[str, float]], i: int,
             ev_window: int = EVENT_WINDOW,
             fresh_days: int = PIVOT_AWAY_FRESH_DAYS) -> dict:
    """第 i 日的因果结构状态（只用 navs[:i+1]，绝不读未来）。"""
    prefix = synth_klines(navs[:i + 1])
    res = cl.analyze(prefix)
    trend = res.get("trend") or "na"
    pivots = res.get("pivots") or []
    nav_i = navs[i][1]

    pos, zg, zd = "none", None, None
    if pivots:
        p = pivots[-1]
        zg, zd = p["ZG"], p["ZD"]
        if nav_i > zg:
            pos = "above"
        elif nav_i < zd:
            pos = "below"
        else:
            pos = "inside"

    # 距离分桶（v0.1）：区分「刚离枢」与「久离枢」，因果只用到第 i 日
    away_days, bucket = _distance_bucket(navs, i, pos, zg, zd, fresh_days)

    d_now = navs[i][0]
    idx_cutoff = navs[max(0, i - ev_window)][0]
    recent_ev = None
    for e in reversed(res.get("events") or []):
        if e["confirm_date"] <= d_now:
            if e["confirm_date"] >= idx_cutoff:
                recent_ev = e["type"]
            break                              # 只看最近一次已确认事件

    state = f"{trend}|{pos}"
    if bucket:
        state += f"~{bucket}"
    if recent_ev:
        state += f"|{recent_ev}"
    return {"date": d_now, "nav": nav_i, "state": state, "trend": trend,
            "pos": pos, "away": away_days if bucket else None,
            "event": recent_ev, "ZG": zg, "ZD": zd}


def states_for_fund(navs: list[tuple[str, float]],
                    ev_window: int = EVENT_WINDOW,
                    fresh_days: int = PIVOT_AWAY_FRESH_DAYS) -> list[dict]:
    """全序列逐日因果状态（跳过 WARMUP_BARS 根之前的日期）。"""
    out = []
    for i in range(WARMUP_BARS, len(navs)):
        st = state_at(navs, i, ev_window, fresh_days)
        st["idx"] = i
        out.append(st)
    return out


# ---------------------------------------------------------------- 转移统计

def _labels(navs: list[tuple[str, float]], idx: int,
            horizons: tuple[int, ...], dates: list[str]) -> dict[int, float | None]:
    """fwd_k = NAV(idx+k)/NAV(idx) − 1（标准监督标签口径，与 backtest_spread 一致）。"""
    out: dict[int, float | None] = {}
    for h in horizons:
        j = idx + h
        out[h] = navs[j][1] / navs[idx][1] - 1 if j < len(navs) else None
    return out


def build_transition_table(per_fund_states: dict[str, list[dict]],
                           per_fund_navs: dict[str, list[tuple[str, float]]],
                           horizons: tuple[int, ...] = DEFAULT_HORIZONS,
                           flat_margin: float = 0.003,
                           oos_start: str = "2025-04-29",
                           min_n: int = 30) -> dict:
    """聚合全部基金样本 → 按状态统计未来收益经验分布。

    返回 {state: {horizon: 统计}}，其中每个周期含：
      n_train/n_oos  样本数（min_n 按 train+oos 总量判定）
      mean/med       经验均值/中位数
      p_up           P(fwd > flat_margin)
      q10/q90        经验分位数（替代旧「正态假设 Q10/Q90」，第六节问题的正解雏形）
    数字含义诚实声明：这是条件频率统计，不是概率模型预测。
    """
    # 累积 (fund, date, value)：value 可能为 None（尾部无未来标签）
    rows: dict[str, dict[int, list[tuple[str, str, float]]]] = {}
    for fund, states in per_fund_states.items():
        navs = per_fund_navs[fund]
        dates = [d for d, _ in navs]
        for st in states:
            lbls = _labels(navs, st["idx"], horizons, dates)
            by_h = rows.setdefault(st["state"], {h: [] for h in horizons})
            for h in horizons:
                v = lbls[h]
                if v is not None:
                    by_h[h].append((fund, st["date"], v))

    table: dict[str, dict] = {}
    for state, by_h in rows.items():
        row_dates = {(f, d) for h in horizons for f, d, _ in by_h[h]}
        n_train = sum(1 for _, d in row_dates if d < oos_start)
        n_oos = len(row_dates) - n_train
        entry: dict[str, object] = {
            "n_total": len(row_dates), "n_train": n_train, "n_oos": n_oos,
            "sufficient": len(row_dates) >= min_n,
        }
        for h, triples in by_h.items():
            entry[f"h{h}"] = {
                "train": _h_stats([v for _, d, v in triples if d < oos_start], flat_margin),
                "oos": _h_stats([v for _, d, v in triples if d >= oos_start], flat_margin),
            }
        table[state] = entry
    return table


def _h_stats(vals: list[float], flat_margin: float) -> dict | None:
    """单段经验分布摘要（None=样本不足）。q10/q90 为经验分位数，非正态假设。"""
    if not vals:
        return None
    arr = np.array(sorted(vals), dtype=float)
    return {
        "n": int(len(arr)),
        "mean": round(float(arr.mean()), 6),
        "med": round(float(np.median(arr)), 6),
        "p_up": round(float((arr > flat_margin).mean()), 4),
        "q10": round(float(arr[int(0.10 * (len(arr) - 1))]), 6),
        "q90": round(float(arr[int(0.90 * (len(arr) - 1))]), 6),
    }


# ---------------------------------------------------------------- 稳定性评分

STABILITY_MIN_OOS = 20         # OOS 样本数低于此不评稳定性
STABILITY_MAX_DIFF = 0.15      # |p_up_train − p_up_oos| 阈值


def summarize_stability(table: dict,
                        horizons: tuple[int, ...] = DEFAULT_HORIZONS,
                        min_oos: int = STABILITY_MIN_OOS,
                        max_diff: float = STABILITY_MAX_DIFF) -> dict:
    """按状态×周期评估 train vs OOS 稳定性（OOS 回看）。

    返回 {state: {h: {p_up_train, p_up_oos, diff, agree, stable, reason}}}。
    稳定条件（全部满足才标记 stable）：
      - oos 样本 >= min_oos
      - |p_up_train − p_up_oos| <= max_diff
      - 方向一致：两者同侧于 0.5（或均在 0.45~0.55 中性带内视为一致）
    reason 说明不达标原因，供人工筛选状态集用。
    """
    out: dict[str, dict] = {}
    for state, e in table.items():
        by_h: dict[int, dict] = {}
        for h in horizons:
            seg = e.get(f"h{h}", {})
            tr = seg.get("train")
            oos = seg.get("oos")
            if tr is None or oos is None or oos.get("n", 0) < min_oos or tr.get("n", 0) < min_oos:
                by_h[h] = {"p_up_train": None, "p_up_oos": None, "diff": None,
                           "agree": None, "stable": False,
                           "reason": "样本不足" if (tr is None or oos is None)
                           else f"oos={oos.get('n')}/{min_oos}"}
                continue
            pt, po = tr["p_up"], oos["p_up"]
            diff = abs(pt - po)
            neutral = (0.45 <= pt <= 0.55) and (0.45 <= po <= 0.55)
            agree = neutral or ((pt - 0.5) * (po - 0.5) > 0)
            stable = diff <= max_diff and agree
            # 失败原因只报实际不满足的项（修复：此前无论哪个条件失败都打印 diff>阈值，误导）
            fails = []
            if diff > max_diff:
                fails.append(f"diff={diff:.2f}>{max_diff:.2f}")
            if not agree:
                fails.append("方向不一致")
            reason = "; ".join(fails)
            by_h[h] = {"p_up_train": pt, "p_up_oos": po, "diff": round(diff, 3),
                       "agree": bool(agree), "stable": bool(stable), "reason": reason}
        out[state] = by_h
    return out
