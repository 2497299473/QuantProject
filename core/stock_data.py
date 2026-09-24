"""股票日 K 数据层：多源容错（腾讯主源 → 东财备源 → Tushare 可选备源）。

**V4 步 2（2026-09-17）**：源链从「`fetch_stock_kline` 函数体内硬编码」迁到
`core/datasource/` 的注册表 + 链执行器；本模块退化为**装配点 + 缓存层**。

迁移不夹带行为变更（硬约束 1 / 2）：
- 对外签名 `fetch_stock_kline(code, market, ttl_hours, min_start)` **一字不改**；
- 返回形状与缓存文件 schema（`{code, market, klines, source}`，klines 5 列升序）不变；
- 全部失败仍抛 `ValueError`，文本逐源列出原因（可诊断性优先）；
- 缓存三态 `fresh` / `cache` / `cache:fallback` 语义归调用方，本模块不动。

链次序（对齐迁移前）：腾讯 0 → 东财 1 → Tushare 2。
**新增能力**：同一进程内连续网络失败达阈值的源会被降级跳过（`health.py`），
但「确定性失败」（如科创板部分标的稳定无数据）不计降级——见 `base.classify_exc`。
"""
import json
import os
import time
from pathlib import Path

from .datasource import SourceRegistry, run_chain
from .datasource.providers.stock_eastmoney import EastmoneyKlineProvider
from .datasource.providers.stock_tencent import TencentKlineProvider
from .datasource.providers.stock_tushare import TushareKlineProvider
from .datasource.symbols import tencent_symbol

BASE_DIR = Path(__file__).resolve().parent.parent
CATEGORY = "stock_kline"

_registry_singleton: SourceRegistry | None = None


def _registry() -> SourceRegistry:
    """装配点：登记日 K 三类源。进程内单例，保证健康度跨调用累积。"""
    global _registry_singleton
    if _registry_singleton is None:
        reg = SourceRegistry()
        for cls in (TencentKlineProvider, EastmoneyKlineProvider, TushareKlineProvider):
            reg.register(cls())
        _registry_singleton = reg
    return _registry_singleton


def fetch_stock_kline(code: str, market: str, ttl_hours: float = 12.0,
                      min_start: str = "2015-01-01") -> dict:
    """market: '0' 深 / '1' 沪。返回 {code, market, klines: [(date, o, c, h, l), ...]} 升序。

    源顺序：缓存 → 腾讯 → 东财 → Tushare(可选)。全部失败抛 ValueError，
    错误信息列出尝试过的每个源的具体原因（可诊断性优先）。
    """
    cache = BASE_DIR / "data" / "stock_klines" / f"{code}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < ttl_hours * 3600:
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("klines"), list):
                return payload
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            # 坏缓存不得在 TTL 内把整条源链锁死；继续走 provider 链。
            pass

    reg = _registry()
    result = run_chain(reg.chain_for(CATEGORY), health=reg.health,
                       code=code, market=market, min_start=min_start)
    if not result.ok:
        symbol = tencent_symbol(code, market)
        detail = " | ".join(f"{a.source}:{a.error}" for a in result.attempts) \
            or "无可用源（链为空）"
        raise ValueError(f"{symbol}: 全部数据源失败（{detail}）")

    out = result.payload
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_name(cache.name + ".tmp")
    try:
        tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, cache)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return out
