"""Provider 协议 + FetchResult 契约。

单一返回值形状：provider 一律返回 FetchResult，**不抛异常**——
失败也是一次合法结果（ok=False + error 文本），保持与 stock_data.py
`attempts` 列表同级的可诊断性。

命名与口径（硬约束 2）：
- `source` 为源名（'eastmoney' / 'tencent' / 'sina' / 'tushare'），
  缓存层三态 `fresh` / `cache` / `cache:fallback` 仍由调用方函数维护，
  本包不动它们的语义。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class FetchResult:
    """一次源调用的结果。frozen——链路上任何一环不得原地改写。"""

    ok: bool
    source: str                     # 源名：'eastmoney' | 'tencent' | 'sina' | 'tushare' | ...
    payload: dict | None = None     # 成功时的数据体（形状由各类别自行约定并在 provider 文档写明）
    error: str | None = None        # 失败原因文本；成功时为 None
    latency_ms: int = 0             # 本次调用耗时，供报告层 source trace 使用

    def __post_init__(self) -> None:
        if self.ok and self.error is not None:
            raise ValueError("ok=True 时 error 必须为 None")
        if not self.ok and self.error is None:
            raise ValueError("ok=False 时必须携带 error 原因")


@runtime_checkable
class Provider(Protocol):
    """一个数据源 = 一个 Provider 实现，放 `providers/` 下，一个源一个文件。

    实现要求：
    - `fetch()` **不得抛异常**，网络失败必须转成 FetchResult(ok=False, error=...)；
    - `timeout_s` 由实现内部传给 `netutil.http_get*`（传输层归 netutil，本包不管）；
    - 纯本地计算型 provider 可设 timeout_s=0（表示无网络语义）。
    """

    name: str           # 唯一注册名，如 'stock_tencent'
    category: str       # 数据类别，如 'stock_kline' | 'fund_nav' | 'realtime_quote'
    priority: int       # 越小越先尝试（链的静态次序，对齐现状：腾讯 0 → 东财 1 → Tushare 2）
    timeout_s: float    # 源级超时（秒）

    def fetch(self, **params: Any) -> FetchResult:
        ...
