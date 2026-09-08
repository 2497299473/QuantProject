"""State Engine 条件分布只读查询（2026-08-27，Forecast 接入第一步）。

职责边界（诚实记录）：
- 只查询、只格式化，不做任何信号/动作决策——旧评分系仍是唯一决策来源；
- 数据源：backtest_state.py 落盘的 output/state_transition_table.json
  （逐日前缀重放的 PIT 条件频率统计，train/oos 分段 + stability 评分）；
- 当前状态用 state_engine 对该基金已落盘净值全序列重放取最后一格，
  因果性与转移矩阵构建口径一致。

展示口径：优先 OOS 段，样本不足回退 train（与 txt 表一致）；
"全周期稳定"判定与 summarize_stability/空真修复后的口径一致：
至少一个周期有真实评估，且所有有评估的周期均稳定。
"""
from __future__ import annotations

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
TABLE_PATH = BASE_DIR / "output" / "state_transition_table.json"
HORIZONS = (1, 3, 5)


def _load_table() -> dict:
    """读落盘表（无缓存，文件仅数十 KB）；缺失返回空壳而非抛错。"""
    if not TABLE_PATH.exists():
        return {}
    try:
        return json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def state_for_fund(code: str) -> str | None:
    """基金当前的因果结构状态串（trend|pos[~bucket][|event]）。数据不足 → None。"""
    try:
        from core import data_loader
        from core import state_engine as se
        navs = data_loader.load_fund(code)["navs"]
        if len(navs) < se.WARMUP_BARS:
            return None
        states = se.states_for_fund(navs)
        return states[-1]["state"] if states else None
    except Exception:
        return None


def lookup(state: str, table_data: dict | None = None) -> dict | None:
    """按状态串查条件分布与稳定性。注入 table_data 供测试；状态不在表 → None。"""
    raw = table_data if table_data is not None else _load_table()
    row = (raw.get("table") or {}).get(state)
    stab = (raw.get("stability") or {}).get(state)
    if not row or not stab:
        return None
    horizons: dict[str, dict] = {}
    for h in HORIZONS:
        seg = row.get(f"h{h}") or {}
        stats = seg.get("oos") or seg.get("train")
        s = stab.get(h) or {}
        horizons[f"T{h}"] = {
            "p_up": stats.get("p_up") if stats else None,
            "mean": stats.get("mean") if stats else None,
            "n_oos": (seg.get("oos") or {}).get("n"),
            "n_train": (seg.get("train") or {}).get("n"),
            "stable": bool(s.get("stable")),
        }
    # 与 backtest_state 空真修复后同口径：空评估不算稳定
    evaluated = [v for v in stab.values()
                 if isinstance(v, dict) and v.get("p_up_train") is not None]
    all_stable = bool(evaluated) and all(v.get("stable") for v in evaluated)
    return {"state": state, "horizons": horizons,
            "all_horizons_stable": bool(all_stable),
            "meta": raw.get("meta") or {}}


def fund_state_ref(code: str, table_data: dict | None = None) -> dict | None:
    """决策侧入口：当前状态 + 其历史条件分布参考。状态未入表也返回标识位。"""
    st = state_for_fund(code)
    if not st:
        return None
    ref = lookup(st, table_data=table_data)
    return ref if ref else {"state": st, "in_table": False}
