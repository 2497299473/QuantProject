"""SourceRegistry：源登记 / 按类别取链 / 源级健康记账入口。

设计要点（参考 FundVal-Live registry.py 的模式，代码自写）：
- 登记是显式的：provider 文件不自动注册自己，由装配点（未来迁移步 2~4）
  统一 `register()`——避免 import 副作用，保持可审计。
- 同一类别内按 (priority, name) 稳定排序；priority 相同的用名字兜底，保证确定性。
- `chain_for()` 默认过滤掉健康度降级源（healthy=False），但允许
  `include_degraded=True` 强制全量——用于人工诊断，不用于静默兜底。
- 重复登记直接报错：一个类别里不允许两个同名源存在语义分叉。
"""
from __future__ import annotations

from .base import Provider
from .health import HealthTracker


class DuplicateProvider(Exception):
    """同一类别登记了同名源。"""


class SourceRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, list[Provider]] = {}   # category -> providers
        self.health = HealthTracker()                     # 进程内健康记账（不落盘）

    def register(self, provider: Provider) -> None:
        bucket = self._providers.setdefault(provider.category, [])
        if any(p.name == provider.name for p in bucket):
            raise DuplicateProvider(
                f"category={provider.category!r} 已登记同名源 {provider.name!r}")
        bucket.append(provider)

    def get(self, category: str, name: str) -> Provider:
        for p in self._providers.get(category, []):
            if p.name == name:
                return p
        raise KeyError(f"未登记：category={category!r} name={name!r}")

    def chain_for(self, category: str, *, include_degraded: bool = False) -> list[Provider]:
        """返回该类别的 fallback 链（已按 priority 排序；不含被健康度降级的源）。"""
        providers = sorted(self._providers.get(category, []),
                           key=lambda p: (p.priority, p.name))
        if include_degraded:
            return list(providers)
        return [p for p in providers if self.health.healthy(p.name)]

    def categories(self) -> list[str]:
        return sorted(self._providers)
