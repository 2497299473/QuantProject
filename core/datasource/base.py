"""Provider 协议 + FetchResult 契约。

单一返回值形状：provider 一律返回 FetchResult，**不抛异常**——
失败也是一次合法结果（ok=False + error 文本），保持与 stock_data.py
`attempts` 列表同级的可诊断性。

命名与口径（硬约束 2）：
- `source` 为源名（'eastmoney' / 'tencent' / 'sina' / 'tushare'），
  缓存层三态 `fresh` / `cache` / `cache:fallback` 仍由调用方函数维护，
  本包不动它们的语义。
- `error` 必须带**失败前缀**（NETWORK / DATA / SKIP / PROTOCOL），
  健康度只对 NETWORK 计连续失败；前缀由 `classify_exc()` 统一判定。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# 失败原因前缀（health.py 据此区分「网络抖动」与「确定性失败」）
NETWORK = "network:"     # 传输层故障（断连/超时/SSL/上游拒绝）→ 计入连续失败
DATA = "data:"           # 确定性失败（无此代码/无数据/上游明确拒绝）→ 不计入
SKIP = "skip:"           # 源主动跳过（未配 token 等）→ 不计入
PROTOCOL = "protocol:"   # provider 违反「不抛异常」契约 → 不计入

# http.client / curl_cffi 的异常不在 OSError 树下，只能按类名兜底。
# JSONDecodeError 也算传输类：多为上游返回拦截页/非预期体，不是「此代码无数据」。
_TRANSPORT_TYPE_NAMES = frozenset({
    "HTTPException", "RemoteDisconnected", "IncompleteRead", "BadStatusLine",
    "LineTooLong", "CurlError", "SSLError", "gaierror", "timeout",
    "TimeoutError", "ConnectionError", "ConnectionResetError",
    "ConnectionRefusedError", "ConnectionAbortedError", "JSONDecodeError",
})


def classify_exc(exc: BaseException) -> str:
    """异常 → 失败前缀。**不 import 网络库**（本包零网络依赖的硬约束）。

    OSError 家族（URLError / HTTPError / SSLError / socket.timeout 等）覆盖 netutil
    绝大多数上抛路径；其余按 MRO 类名兜底。未识别的一律归 DATA——宁可少降级
    也不冤枉正常源（2026-08-27 科创板「稳定无数据」教训）。
    """
    if isinstance(exc, OSError):
        return NETWORK
    for klass in type(exc).__mro__:
        if klass.__name__ in _TRANSPORT_TYPE_NAMES:
            return NETWORK
    return DATA


@dataclass(frozen=True)
class FetchResult:
    """一次源调用的结果。frozen——链路上任何一环不得原地改写。"""

    ok: bool
    source: str                     # 源名：'eastmoney' | 'tencent' | 'sina' | 'tushare' | ...
    payload: dict | None = None     # 成功时的数据体（形状由各类别自行约定并在 provider 文档写明）
    error: str | None = None        # 失败原因（须带 NETWORK/DATA/SKIP 前缀）；成功时为 None
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
