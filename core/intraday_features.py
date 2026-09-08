"""日内行为特征引擎（观察层，纯描述，不输出动作）。

背景（GPT-5.6 诊断 + 2026-08-25 三轮回测裁决的融合结论）：
- P0：实时行情此前只进报告/推送，未进入任何特征/决策层 → 本模块把实时行情组织成描述性特征
- P1：前十大估算缺少「可信度」→ 引入 coverage + 持仓新鲜度 + 同向一致度合成可信度
- 「加仓/减仓/不动」动作决策不在此层——必须经 backtest_action.py 证据裁决后由 decision_engine 启用

特征（全部可解释、可回测）：
- est_return          当日估算涨跌（前十大权重加权，来自 real_time.weighted_estimate，百分数）
- breadth             重仓股同向一致度（按权重：(上涨权重-下跌权重)/有涨跌权重，∈[-1,+1]）
- concentration       集中度：max(单票贡献绝对值)/Σ|贡献|，∈[0,1]，越接近 1 越「一票独大」
- covered_pct         实时覆盖用披露权重占比（百分数，73 表示 73%）
- holdings_age_days   持仓新鲜度（snapshot_date 距今天自然日）
- reliability         估算可信度 ∈[0,1]：0.5×覆盖率归一 + 0.5×新鲜度
- close_phase_change  14:55 估算 - 11:30 估算（由调用方传入 prev_est 合成）

诚实边界：全部基于「最新季报前十大 × 实时行情」的估算口径，持仓滞后 1~3 个月。
"""
from datetime import date, datetime


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def days_between(a: str, b: str) -> int:
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)
    except (TypeError, ValueError):
        return 0


def holdings_freshness(snapshot_date: str, today: str | None = None) -> float:
    """持仓新鲜度：≤45 天=1.0，45~120 天线性衰减，≥120 天=0.0（对齐准入红线 120 天）。"""
    age = days_between(snapshot_date, today or _today()) if snapshot_date else 0
    if age <= 45:
        return 1.0
    if age >= 120:
        return 0.0
    return 1.0 - (age - 45) / 75.0


def compute_features(holdings, quotes, est, snapshot_date, prev_est=None) -> dict:
    """从实时行情 + 披露持仓合成描述性特征。

    holdings: [{market, code, name, pct}, ...]（lookthrough aggregate 的 rows）
    quotes:   {code: {name, price, change_pct, ...}}（real_time.fetch_realtime 输出）
    est:      real_time.weighted_estimate 输出 {est_change_pct, covered_pct}
    snapshot_date: 'YYYY-MM-DD'（季报披露日）
    prev_est: 同日 11:30 的 est_change_pct（14:55 传入以计算变化量）

    返回特征 dict；数据不足时字段退化为 None 而不抛异常。
    """
    if not holdings or not quotes or est.get("est_change_pct") is None:
        return {}

    up_w = down_w = 0.0
    n_up = n_down = 0
    contribs = []  # (code, 权重×涨跌)
    for h in holdings:
        q = quotes.get(h["code"])
        if not q:
            continue
        chg = q.get("change_pct") or 0.0
        contribs.append((h["code"], h["pct"] * chg))
        if chg > 0:
            up_w += h["pct"]
            n_up += 1
        elif chg < 0:
            down_w += h["pct"]
            n_down += 1

    est_return = est["est_change_pct"]
    covered = est.get("covered_pct", 0.0)

    breadth = (up_w - down_w) / (up_w + down_w) if (up_w + down_w) > 0 else None

    abs_sum = sum(abs(c) for _, c in contribs)
    if abs_sum > 1e-9:
        concentration = max(abs(c) for _, c in contribs) / abs_sum
    else:
        concentration = None

    fresh = holdings_freshness(snapshot_date)
    cov_norm = min(covered / 100.0, 1.0) if covered else 0.0
    reliability = round(0.5 * cov_norm + 0.5 * fresh, 3)

    feats = {
        "est_return": est_return,
        "breadth": breadth,
        "concentration": concentration,
        "covered_pct": covered,
        "holdings_age_days": days_between(snapshot_date, _today()) if snapshot_date else 0,
        "reliability": reliability,
        "n_up": n_up,
        "n_down": n_down,
    }
    if prev_est is not None and est_return is not None:
        feats["close_phase_change"] = est_return - prev_est
    return feats
