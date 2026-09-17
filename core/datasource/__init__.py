"""数据源层（V4 重构 · 第 1 步骨架，2026-09-17）。

设计契约参考 FundVal-Live `sources/{base,registry}.py`（AGPL-3.0，**只借模式不搬代码**）
与 fundviewer `data_sources/fallback.py`（MIT），全部本地自写。

目标（见 Obsidian《V4数据源层重构方案-SourceRegistry-20260917》）：
把「源」从函数变成可登记、可排序、可健康记账的实体。

四条硬约束在本包内的落点：
1. 调用面冻结——旧函数（load_fund / fetch_stock_kline / fetch_realtime）签名不动，
   迁移时只换内部实现；本包第一版不接任何 provider。
2. `_source` 口径不倒退——`fresh` / `cache` / `cache:fallback` 三态由调用方保留，
   本包的 source 标记是**追加维度**（`source:<name>`），不改旧语义。
3. 零网络——本包自身不做任何 I/O；传输一律经 provider 内部走 `core/netutil.py`（保持不动）。
4. 东财频控——链执行器**严格串行**，禁止并发打同一域；定时器自动开跑受项目铁律 7 约束。
"""
from .base import FetchResult, Provider
from .registry import SourceRegistry
from .chain import ChainResult, Attempt, run_chain
from .health import HealthTracker

__all__ = [
    "FetchResult",
    "Provider",
    "SourceRegistry",
    "ChainResult",
    "Attempt",
    "run_chain",
    "HealthTracker",
]
