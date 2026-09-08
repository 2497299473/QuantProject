"""信号引擎：三因子弱参考（总分 -3 ~ +3）。

历史依据（2026-08-22 回测校准，4802 交易日全样本）：
- 旧 10 维引擎总分 IC=-0.041 方向性错误 → 已废弃（均值回归在主动基金上无效）
- 三因子「趋势中买回调」买入≥2 超额：002112 +1.56% / 002207 +0.54%（严格优于旧引擎）
- 但时段依赖（002112 前50% 为 -0.53%）→ 只能作「弱参考」，绝不构成买卖指令

输出措辞恒为：偏多 / 偏空 / 中性 —— 不出现「买入/卖出」指令性表述。
"""
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_cfg() -> dict:
    return json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))


def ema(values: list[float], span: int) -> list[float]:
    alpha = 2.0 / (span + 1)
    out = []
    prev = values[0]
    for v in values:
        prev = alpha * v + (1 - alpha) * prev
        out.append(prev)
    return out


def macd_hist(navs: list[float], fast: int, slow: int, dea: int) -> list[float]:
    dif = [f - s for f, s in zip(ema(navs, fast), ema(navs, slow))]
    dea_line = ema(dif, dea)
    return [d - e for d, e in zip(dif, dea_line)]


def factor_macd_hist_trend(navs: list[float], params: dict) -> dict:
    hist = macd_hist(navs, params["ema_fast"], params["ema_slow"], params["dea"])
    h = hist[-1]
    return {"score": 1 if h > 0 else (-1 if h < 0 else 0), "detail": f"MACD柱最新值 {h:+.4f}"}


def factor_drawdown_from_high(navs: list[float], params: dict) -> dict:
    window = navs[-params["window"]:]
    high = max(window)
    dd = navs[-1] / high - 1 if high else 0.0
    if dd <= params["pullback_threshold"]:
        score = 1   # 刚回调：趋势中买回调逻辑的买点侧
    elif dd >= params["near_high_threshold"]:
        score = -1  # 贴近新高：短期动能透支侧
    else:
        score = 0
    return {"score": score, "detail": f"距{params['window']}日新高 {dd*100:+.2f}%"}


def factor_pool_rank_20d(pool_returns: dict[str, float], code: str, params: dict) -> dict:
    """池内 20 日收益排名（横截面相对强弱）。上半池 +1，下半池 -1；样本为奇数时中位 0。"""
    valid = sorted(pool_returns.items(), key=lambda kv: kv[1], reverse=True)
    n = len(valid)
    if n < 2:
        return {"score": 0, "detail": "池内有效样本不足"}
    rank = [c for c, _ in valid].index(code) + 1
    half = n / 2
    score = 1 if rank <= half - 0.5 else (-1 if rank >= half + 0.5 else 0)
    ret = pool_returns.get(code)
    return {"score": score, "detail": f"20日收益 {ret*100:+.2f}%，池内第 {rank}/{n} 名"}


def compute_signals(funds: dict[str, dict], lookthrough: dict[str, dict] | None = None) -> dict[str, dict]:
    """funds: {code: {name, navs: [(date, nav)], ...}}；lookthrough: 穿透观察（仅供报告展示）。

    注（2026-08-25 移除）：缠论 composite 曾作为可选第四因子接入（engine_factor_enabled），
    三轮回测（组合/方向差/OOS）一致证明其无增量甚至负贡献 → 永久移出引擎，仅保留展示。
    """
    full_cfg = _load_cfg()
    cfg = full_cfg["signal"]
    fcfg, wcfg = cfg["factors"], cfg["weak_signal"]
    min_hist = wcfg["min_history"]

    pool_returns: dict[str, float] = {}
    for code, f in funds.items():
        navs = [v for _, v in f["navs"]]
        if len(navs) >= fcfg["pool_rank_20d"]["window"] + 1:
            pool_returns[code] = navs[-1] / navs[-1 - fcfg["pool_rank_20d"]["window"]] - 1

    results = {}
    for code, f in funds.items():
        navs = [v for _, v in f["navs"]]
        last_date = f["navs"][-1][0]
        factors, insufficient = {}, False

        if len(navs) < min_hist:
            insufficient = True
            factors["note"] = f"历史 {len(navs)} 条 < {min_hist}，长周期因子数据不足"
        if len(navs) >= fcfg["macd_hist_trend"]["ema_slow"] + fcfg["macd_hist_trend"]["dea"]:
            factors["macd_hist_trend"] = factor_macd_hist_trend(navs, fcfg["macd_hist_trend"])
        if len(navs) >= fcfg["drawdown_from_60d_high"]["window"]:
            factors["drawdown_from_60d_high"] = factor_drawdown_from_high(navs, fcfg["drawdown_from_60d_high"])
        if code in pool_returns:
            factors["pool_rank_20d"] = factor_pool_rank_20d(pool_returns, code, fcfg["pool_rank_20d"])

        score = sum(v["score"] for k, v in factors.items() if isinstance(v, dict))
        if score >= wcfg["bull_threshold"]:
            stance = "偏多"
        elif score <= wcfg["bear_threshold"]:
            stance = "偏空"
        else:
            stance = "中性"

        results[code] = {
            "name": f["name"], "last_nav_date": last_date, "last_nav": navs[-1],
            "nav_count": len(navs), "factors": factors, "score": score, "stance": stance,
            "insufficient_history": insufficient,
            "purchase_status": f.get("purchase_status", ""),
            "redeem_status": f.get("redeem_status", ""),
            "_source": f.get("_source", "fresh"),
            "pool_rank_returns": pool_returns,
            "wording": f"总分 {score:+d} · {stance}（弱参考，不构成买卖指令）",
        }
    return results
