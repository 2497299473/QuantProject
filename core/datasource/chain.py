"""fallback 链执行器：串行尝试 + source trace。

与现状的关系（硬约束 1 / 2）：
- 本模块**不接管缓存**——`cache` / `cache:fallback` 三态仍归调用方
  （data_loader.load_fund 等）维护；本模块只负责「真取数时按链换源」。
- 严格串行、禁止并发（硬约束 4：多源重构不得变成自动多路并发打东财）。
- 每个源的失败原因进 `ChainResult.attempts`，与旧 stock_data.attempts 的可诊断性持平。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .base import FetchResult, Provider
from .health import HealthTracker


@dataclass(frozen=True)
class Attempt:
    """链上单环记录（供报告层 source trace 渲染）。"""

    source: str
    ok: bool
    error: str | None
    latency_ms: int


@dataclass
class ChainResult:
    ok: bool
    source: str | None = None            # 成功源名；全灭时为 None
    payload: dict | None = None
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def last_error(self) -> str | None:
        return self.attempts[-1].error if self.attempts else None


def run_chain(chain: list[Provider], *,
              health: HealthTracker | None = None, **params: Any) -> ChainResult:
    """按给定顺序串行尝试 provider，第一个 ok 即返回。

    - provider.fetch 契约上不应抛异常；若真抛了（实现 bug 或协议违反），
      在此兜底转成 ok=False 并**继续走链**，同时留 error 前缀 `protocol:` 便于识别。
    - 空链返回 ok=False / attempts=[]，由调用方决定如何降级（如读缓存）。
    - `health` 传入注册表的 HealthTracker 时，逐环记账（连续网络失败 → 降级到链尾）。
    """
    attempts: list[Attempt] = []
    for provider in chain:
        started = time.monotonic()
        try:
            result = provider.fetch(**params)
        except Exception as exc:  # 协议违反兜底：不让一个坏 provider 打断整条链
            result = FetchResult(ok=False, source=provider.name,
                                 error=f"protocol:{type(exc).__name__}: {exc}")
        elapsed_ms = int((time.monotonic() - started) * 1000)
        # 用 provider 自己的计时（若给了），否则用包裹层墙钟
        latency = result.latency_ms if result.latency_ms else elapsed_ms
        attempts.append(Attempt(source=result.source, ok=result.ok,
                                error=result.error, latency_ms=latency))
        if health is not None:
            health.record(provider.name, ok=result.ok, error=result.error)
        if result.ok:
            return ChainResult(ok=True, source=result.source,
                               payload=result.payload, attempts=attempts)
    return ChainResult(ok=False, attempts=attempts)
