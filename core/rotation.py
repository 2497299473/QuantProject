"""池内轮动参考（横截面·弱参考）。

证据依据（2026-08-22 阶段②连续因子检验 + 2026-08-23 重建版复验）：
- 横截面（选基/轮动）显著强于时序（择时）：MACD柱横截面 IC 最高，20日收益分桶单调
- 轮动 = 每日在池内按横截面综合分排名，提示相对强弱，**不构成调仓指令**
- 综合分 = rank(20日收益) + rank(MACD柱) 两个最强横截面因子的秩和（越高越强）
"""
import json
from pathlib import Path

from .signal_engine import macd_hist

BASE_DIR = Path(__file__).resolve().parent.parent


def evaluate_rotation(funds: dict[str, dict]) -> dict | None:
    """funds: {code: {name, navs}} → 当日池内横截面排名视图。"""
    if len(funds) < 2:
        return None
    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    fcfg = cfg["signal"]["factors"]
    rows = []
    for code, f in funds.items():
        navs = [v for _, v in f["navs"]]
        if len(navs) < fcfg["pool_rank_20d"]["window"] + 1:
            continue
        r20 = navs[-1] / navs[-1 - fcfg["pool_rank_20d"]["window"]] - 1
        hist = macd_hist(navs, fcfg["macd_hist_trend"]["ema_fast"],
                         fcfg["macd_hist_trend"]["ema_slow"], fcfg["macd_hist_trend"]["dea"])[-1]
        rows.append({"code": code, "name": f["name"], "r20": r20, "macd_hist": hist})

    if len(rows) < 2:
        return None
    for key in ("r20", "macd_hist"):   # 秩和：最强=2，最弱=0（双因子各 0/1）
        order = sorted(rows, key=lambda r: r[key])
        for i, r in enumerate(order):
            r[f"rank_{key}"] = i
    for r in rows:
        r["score"] = r["rank_r20"] + r["rank_macd_hist"]
    rows.sort(key=lambda r: -r["score"])
    return {"as_of": max(f["navs"][-1][0] for f in funds.values()),
            "ranking": rows,
            "top": rows[0]["code"]}
