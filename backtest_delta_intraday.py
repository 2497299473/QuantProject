#!/usr/bin/env python3
"""盘中 delta 实验（2026-09-01 预注册，GPT 五审 P2+；执行窗口 09-26 重估）。

设计（Obsidian v7「附录 4」L366 原文）：
    11:30→14:55 盘中变化量实验：baseline vs baseline+delta_intraday
    严格 OOS 对比（infrastructure 已备，FEATURE_KEYS 暂不动）

数据来源：core.intraday_feature_store（data/intraday_features.jsonl，08-31 起
每日 mid/post 自动落盘，(date, slot, fund) 主键，last-write-wins）。

预注册判定（写死，09-26 不得临场改规则）：
    R1 数据门槛：配对交易日（同日同基金 mid+post 齐全）>= MIN_PAIRED_DAYS=15，
       不足 → 打印存储健康度 + exit 1（诚实占位，不产出 verdict）。
    R2 delta 构造：delta_i = feat_i(post) − feat_i(mid)，7 特征逐项；
       任一侧缺失/非数值 → delta_i=None（缺失不伪造），missing_mask=1 保留
       「缺失模式」本身（B1 协议已铺路，v3+ 消费 (值, mask) 双列）。
    R3 判据（与 WF/评分卡同哲学，pooled + CI）：
       主判据 = 增量信息 ΔIC = IC(baseline+delta) − IC(baseline)（T+5 口径，
       RankIC cluster bootstrap CI）。
       ΔIC > +0.01 且 delta 侧 IC 正 → delta_stable（盘中变化量有增量）
       ΔIC ∈ [−0.01, +0.01] → delta_partial（无系统性增量，不加）
       ΔIC < −0.01 → delta_fail（有损，禁止接入）
    R4 防泄漏：标签统一用 backtest_spread.load_samples 的 fwd5（PIT 口径）；
       delta 只用当日已发生的历史快照（11:30/14:55 均早于 T+5 标签窗口）；
       训练切分沿用 split_date_oos 冻结口径，delta 特征只进 OOS 对照实验，
       不进生产 FEATURE_KEYS（FEATURE_KEYS 暂不动）。

诚实边界：本实验只产证据，不自动改 model_ready / FEATURE_KEYS / 权重。
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from core import intraday_feature_store as store  # noqa: E402
from core import forecast_engine                  # noqa: E402

# 预注册常量（改判据 = 改预注册，需重新审，不在此静默改）
MIN_PAIRED_DAYS = 15          # 数据门槛：配对交易日数（≈3 周，对齐「2~4 周数据」口径）
DELTA_IC_STABLE = 0.01        # ΔIC 上界：> +0.01 且 delta 侧 IC 正 → stable
DELTA_IC_FAIL = -0.01         # ΔIC 下界：< −0.01 → fail
VERDICT_VERSION = 1
FEATURE_KEYS = list(forecast_engine.FEATURE_KEYS)


def pair_slots(history: list[dict]) -> list[dict]:
    """从 store 历史中构造 mid+post 配对样本。

    配对键 = (date, fund)，要求同日 mid 与 post 均存在且 features 非空。
    返回 [{date, fund, mid: {...}, post: {...}}]，按 date 升序。
    """
    by_key: dict[tuple[str, str], dict] = {}
    for rec in history:
        date, slot, fund = rec.get("date", ""), rec.get("slot", ""), rec.get("fund", "")
        feats = rec.get("features") or {}
        if not date or not slot or not fund or not feats:
            continue
        key = (date, fund)
        by_key.setdefault(key, {})[slot] = feats
    out = []
    for (date, fund), slots in by_key.items():
        if "mid" in slots and "post" in slots:
            out.append({"date": date, "fund": fund,
                        "mid": slots["mid"], "post": slots["post"]})
    out.sort(key=lambda r: r["date"])
    return out


def build_delta_features(mid: dict, post: dict,
                         feature_keys: list[str] | None = None) -> dict:
    """构造 delta 特征（纯函数，可单测）。

    返回 {key: {"delta": float|None, "missing": bool}}。
    delta = post − mid；任一侧缺失/非数值 → delta=None + missing=True（不伪造）。
    """
    keys = feature_keys or FEATURE_KEYS
    out = {}
    for k in keys:
        a, b = mid.get(k), post.get(k)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            out[k] = {"delta": float(b) - float(a), "missing": False}
        else:
            out[k] = {"delta": None, "missing": True}
    return out


def paired_day_count(history: list[dict]) -> int:
    """配对交易日数（同日同基金 mid+post 齐全的天数，基金不重复计）。"""
    return len({r["date"] for r in pair_slots(history)})


def coverage_report(history: list[dict]) -> dict:
    """存储健康度摘要（诊断用）。"""
    raw = store.coverage_summary()
    paired = pair_slots(history)
    return {
        "store": raw,
        "n_paired_records": len(paired),
        "n_paired_days": paired_day_count(history),
        "paired_dates": sorted({r["date"] for r in paired}),
    }


def verdict_for(delta_ic: float | None, delta_ic_ci: tuple[float, float] | None,
                n_paired_days: int) -> dict:
    """预注册判定（R1/R3）。纯函数，数据齐后由回测填充 delta_ic。

    delta_ic=None 且门槛不足 → {"verdict": "insufficient_data", "reason": ...}
    """
    if n_paired_days < MIN_PAIRED_DAYS:
        return {
            "verdict": "insufficient_data",
            "rule_version": VERDICT_VERSION,
            "reason": (f"配对交易日 {n_paired_days} < 门槛 {MIN_PAIRED_DAYS}，"
                       f"继续积累（09-26 重估复核）"),
        }
    if delta_ic is None:
        return {"verdict": "pending", "rule_version": VERDICT_VERSION,
                "reason": "数据门槛已过但 delta IC 未计算（回测未填充）"}
    lo = delta_ic_ci[0] if delta_ic_ci else delta_ic
    if delta_ic > DELTA_IC_STABLE and lo > 0:
        return {"verdict": "delta_stable", "rule_version": VERDICT_VERSION,
                "reason": f"ΔIC={delta_ic:+.3f} CI[{lo:+.3f},...] 下界>0 且>+0.01："
                          f"盘中变化量有增量信息，可讨论接入"}
    if delta_ic < DELTA_IC_FAIL:
        return {"verdict": "delta_fail", "rule_version": VERDICT_VERSION,
                "reason": f"ΔIC={delta_ic:+.3f} < −0.01：加 delta 有损，禁止接入"}
    return {"verdict": "delta_partial", "rule_version": VERDICT_VERSION,
            "reason": f"ΔIC={delta_ic:+.3f} ∈ [−0.01, +0.01]：无系统性增量，不加"}


def main() -> int:
    history = store.load_history()
    report = coverage_report(history)
    print("== 盘中 delta 实验 · 预注册骨架（VERDICT_VERSION=%d）==" % VERDICT_VERSION)
    print(f"  存储健康度: {report['store']}")
    print(f"  配对样本: {report['n_paired_records']} 条 / {report['n_paired_days']} 天 "
          f"{report['paired_dates']}")
    v = verdict_for(None, None, report["n_paired_days"])
    print(f"  判定: {v['verdict']} — {v['reason']}")
    if v["verdict"] == "insufficient_data":
        print("[exit] 数据不足，不产出 verdict；09-26 重估复核（cron d62bd422）")
        return 1
    print("[ok] 数据门槛已过，等待回测填充 delta IC（backtest_delta 主判据）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
