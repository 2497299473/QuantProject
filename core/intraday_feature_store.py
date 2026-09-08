"""预测特征日级累积存储（2026-08-29，GPT 三审 P1）。

背景：
- 此前每日 14:55 的预测特征（est_chg/breadth/...）只活在当次进程里，用完即弃；
  「state→forecast 增量实验」「Forecast→Policy 联合回测」都需要历史特征序列，
  否则每次做实验都要重放半年数据。
- 本模块把每次运行的预测特征按 (date, slot, fund) 追加落盘
  data/intraday_features.jsonl（一行一条），供后续实验读取。

纪律（诚实边界）：
1. 只追加不覆盖：同 (date, slot, fund) 重跑会产生多条记录，读取端按
   timestamp 取最后一条（last-write-wins），历史留痕不删。
2. 只存「当时实际用于预测的特征 + 上下文」，不存模型权重、不存未来标签
   （标签由 backtest_spread 在需要时统一算，避免口径分裂）。
3. 写入失败静默降级（不阻断主流程），与 state_ref_history 同哲学。
4. 文件只增，不做自动清理；体积估算 ~4 基金 × 2 时点 × 365 天 ≈ 3MB/年。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STORE_PATH = BASE_DIR / "data" / "intraday_features.jsonl"


def _read_lines() -> list[dict]:
    if not STORE_PATH.exists():
        return []
    out = []
    with open(STORE_PATH, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue          # 坏行跳过（不阻断读取）
    return out


def append_features(date: str, slot: str, fund: str, features: dict,
                    context: dict | None = None,
                    model_version: int | None = None) -> bool:
    """追加一条预测特征记录。返回是否成功（失败不抛异常）。

    features: FEATURE_KEYS 口径（est_chg/est_sign/breadth/concentration/
              covered_pct/composite/score），None 值保留为 null（如实记录缺失）。
    context:  附加上下文（model_version/overall_confidence/state 等）。
    """
    if not features:
        return False
    try:
        STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "date": date, "slot": slot, "fund": fund,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "model_version": model_version,
            "features": features,
            "context": context or {},
        }
        with open(STORE_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def load_history(fund: str | None = None,
                 slot: str | None = None,
                 since: str | None = None) -> list[dict]:
    """读取历史特征序列（按 timestamp 升序）。

    同 (date, slot, fund) 多条时 last-write-wins（重跑以最后一次为准）。
    返回的每条已去重，date 升序、slot 字典序。
    """
    best: dict[tuple[str, str, str], dict] = {}
    for rec in _read_lines():
        if fund and rec.get("fund") != fund:
            continue
        if slot and rec.get("slot") != slot:
            continue
        if since and rec.get("date", "") < since:
            continue
        key = (rec.get("date", ""), rec.get("slot", ""), rec.get("fund", ""))
        prev = best.get(key)
        if prev is None or rec.get("timestamp", "") >= prev.get("timestamp", ""):
            best[key] = rec
    out = list(best.values())
    out.sort(key=lambda r: (r["date"], r["slot"], r["fund"]))
    return out


def coverage_summary() -> dict:
    """存储健康度摘要（诊断用）：覆盖日期数 / 基金数 / 记录数 / 日期范围。"""
    recs = _read_lines()
    dates = {r["date"] for r in recs if r.get("date")}
    funds = {r["fund"] for r in recs if r.get("fund")}
    return {
        "n_records": len(recs),
        "n_dates": len(dates),
        "n_funds": len(funds),
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
    }
