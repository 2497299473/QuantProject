"""账户面管理（本系统的核心定位）。

持仓来源 holdings.json（本地私有）。提供：
- 每只持仓基金市值 / 浮盈浮亏
- 账户合计
- 次日净值区间预估（最新净值 × (1 + μ20 ± N·σ20)，μ/σ 为近 20 日日收益均值与标准差）
- 异常波动告警（单日涨跌超过 N·σ20）

融合版增强（2026-08-23）：区间公式吸收 quant_test 母本的均值项——
原 GLM 对称区间中心恒为最新净值，若近 20 日存在漂移（μ≠0），区间中心系统性偏移；
现以 μ 平移中心，σ 控制宽度，与母本 mean ± σ 口径一致（μ 用 20 日滚动均值）。
"""
import json
import math
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def load_holdings() -> dict:
    return json.loads((BASE_DIR / "holdings.json").read_text(encoding="utf-8"))


def daily_return_stats(navs: list[float], window: int) -> tuple[float, float]:
    """近 window 日日收益率的 (均值, 标准差)。

    融合版增强：quant_test 母本的账户面均值项并入主干。次日区间 = nav × (1 + mean ± N·σ)，
    使区间中心贴近近期漂移，避免对称区间中心恒为旧净值（次日漂移不为零）。
    返回 (mean, sigma)；样本不足或 sigma<=0 时区间退化为点。
    """
    if len(navs) < window + 1:
        window = len(navs) - 1
    if window < 2:
        return 0.0, 0.0
    rets = [navs[i] / navs[i - 1] - 1 for i in range(len(navs) - window, len(navs))]
    mean = sum(rets) / len(rets)
    sigma = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    return mean, sigma


def evaluate_account(signals: dict[str, dict]) -> dict:
    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))["account"]
    holdings = load_holdings()["funds"]
    vol_window = cfg["vol_window"]

    positions, total_mv, total_cost = [], 0.0, 0.0
    for code, h in holdings.items():
        sig = signals.get(code)
        if not sig or not h.get("shares"):
            continue
        nav, cost = sig["last_nav"], h["cost_nav"]
        mv = h["shares"] * nav
        pnl_pct = nav / cost - 1 if cost else 0.0
        # 因子分解里没有原始净值序列，此处从缓存取
        fund = json.loads((BASE_DIR / "data" / "klines" / f"{code}.json").read_text(encoding="utf-8"))
        navs = [v for _, v in fund["navs"]]
        mu, sigma = daily_return_stats(navs, vol_window)
        last_change = navs[-1] / navs[-2] - 1 if len(navs) >= 2 else 0.0
        # 次日区间：nav × (1 + μ ± N·σ)。μ 平移中心（母本均值项），σ 控制宽度。
        lo_f = 1 + mu - cfg["next_day_range_sigma"] * sigma
        hi_f = 1 + mu + cfg["next_day_range_sigma"] * sigma
        positions.append({
            "code": code, "name": sig["name"],
            "shares": h["shares"], "cost_nav": cost, "last_nav": nav,
            "market_value": round(mv, 2),
            "pnl_pct": round(pnl_pct * 100, 2),
            "daily_vol": sigma,
            "daily_mean": round(mu * 100, 4),
            "last_change_pct": round(last_change * 100, 2),
            "abnormal": abs(last_change) > cfg["abnormal_vol_sigma"] * sigma if sigma > 0 else False,
            "next_day_range": (
                round(nav * lo_f, 4),
                round(nav * hi_f, 4),
            ) if sigma > 0 else (nav, nav),
            "next_day_mv_range": (
                round(h["shares"] * nav * lo_f, 2),
                round(h["shares"] * nav * hi_f, 2),
            ) if sigma > 0 else (round(mv, 2), round(mv, 2)),
            "stance": sig["stance"], "score": sig["score"],
        })
        total_mv += mv
        total_cost += h["shares"] * (cost or nav)

    return {
        "as_of_nav_date": signals[next(iter(signals))]["last_nav_date"] if signals else "",
        "positions": positions,
        "total_market_value": round(total_mv, 2),
        "total_cost": round(total_cost, 2),
        "total_pnl_pct": round((total_mv / total_cost - 1) * 100, 2) if total_cost else 0.0,
        "abnormal_alerts": [p["code"] for p in positions if p["abnormal"]],
    }
