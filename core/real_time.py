"""重仓股实时行情：腾讯 qt.gtimg.cn（盘中实时 / 收盘定格）。

用途：推送时点（午盘 11:30 / 收盘前 14:55）展示持仓股票实时涨跌，
并以前十大披露权重加权得到「底层持仓估算涨跌」（lookthrough_estimated_change）。

诚实边界（2026-08-25 定，措辞对齐 GPT-5.6 诊断第四节）：
- 场外基金当日净值约 20:00 后才公布 → 基金涨跌无法直接获取
- 估算 = 最新季报前十大 × 实时行情加权，是「滞后持仓的实时市场冲击估计」，
  不是基金经理今日真实持仓 → 报告/推送措辞统一用「当日估算」，避免与正式净值混淆

**V4 步 3（2026-09-17）**：取数迁 `core/datasource/providers/realtime_tencent.py`；
本模块退化为**装配点**。迁移不夹带行为变更（硬约束 1）：
- 对外签名 `fetch_realtime(holdings)` / `weighted_estimate(holdings, quotes)` 一字不改；
- 成功返回形状不变：`{code: {name, price, change_pct, time, pct, market}}`；
- **步 3 的唯一语义升级（验收点）**：失败不再静默返回 `{}`——改为
  `{"_error": "network:...|data:..."}`。`_error` 不是报价条目（6 位代码键才会
  被 `weighted_estimate` 命中），调用方按现有 `est_change_pct is None` 分支自然
  降级，同时错误文本可诊断、可入日志。
"""
from .datasource import SourceRegistry, run_chain
from .datasource.providers.realtime_tencent import RealtimeTencentProvider
from .datasource.symbols import tencent_symbol

CATEGORY = "realtime_quote"

_registry_singleton: SourceRegistry | None = None


def _registry() -> SourceRegistry:
    """装配点：登记实时行情源。进程内单例，健康度跨调用累积。"""
    global _registry_singleton
    if _registry_singleton is None:
        reg = SourceRegistry()
        reg.register(RealtimeTencentProvider())
        _registry_singleton = reg
    return _registry_singleton


def fetch_realtime(holdings: list[dict]) -> dict:
    """holdings: [{market, code, name, pct}, ...] → {code: {name, price, change_pct, time, pct}}。

    全链失败返回 `{"_error": 原因}`（旧实现为静默 `{}`，步 3 起留痕）；
    调用方降级路径不变（报告/卡片不展示实时栏，基金进 degraded_funds）。
    """
    out: dict[str, dict] = {}
    if not holdings:
        return out
    syms = ",".join(tencent_symbol(h["code"], h["market"]) for h in holdings)

    reg = _registry()
    result = run_chain(reg.chain_for(CATEGORY), health=reg.health, symbols=syms)
    if not result.ok:
        return {"_error": result.last_error or "无可用源（链为空）"}

    quotes = result.payload["quotes"]
    for h in holdings:
        q = quotes.get(h["code"])
        if q:
            q["pct"] = h["pct"]
            q["market"] = h["market"]
    return quotes


def weighted_estimate(holdings: list[dict], quotes: dict) -> dict:
    """按披露权重加权 → 基金估算涨跌。返回 {est_change_pct, covered_pct}；无覆盖返回 None。"""
    wsum = wchg = 0.0
    for h in holdings:
        q = quotes.get(h["code"])
        if q:
            wsum += h["pct"]
            wchg += h["pct"] * q["change_pct"]
    if wsum <= 0:
        return {"est_change_pct": None, "covered_pct": 0.0}
    return {"est_change_pct": wchg / wsum, "covered_pct": wsum}
