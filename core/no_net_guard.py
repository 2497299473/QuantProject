"""零网络守卫共享实现（D-06，2026-09-30 面 4 审计）。

职责边界（先说清楚，防语义混排——面 4 审计 C 节的定性）：
- 本守卫是**频控纪律 / 零网络自检**（铁律 1/7/8：防止离线作业误发东财请求
  打挂 IP 级频控），**不是 PIT 防穿越机制**。PIT 防穿越靠数据结构
  （label/特征分离）+ 切分纪律（label-end purge）+ 冻结件三件套（G-A sha）。
  报告措辞引用本守卫时只可写「零网络（频控纪律）」，不得写成「防穿越」。
- 守卫是**进程内纪律墙**：挡本进程经 socket 模块发起的新连接；挡不住
  子进程、patch 前已绑定的旧引用、磁盘缓存污染。需要更强保证时用
  审计轨（block_construction=True）+ 事后核验（run_manifest / 请求计数）。

为什么收敛到这里（ponytail lite：工具/胶水层收敛重复）：
- 收敛前全仓 6 个脚本各自复制 monkeypatch 样板（14 处赋值），实现不一
  （有的只拦 connect、有的拦构造、有的还拦 create_connection），
  UDP sendto 全部漏拦。单一实现后行为一致、缺口统一补上。

用法：
    from core import no_net_guard
    restore = no_net_guard.install("[脚本名] 零网络作业")
    try:
        ...  # 离线计算
    finally:
        no_net_guard.restore(restore)      # 长驻脚本可不 restore（进程级纪律）

    # 审计轨（更严：连构造都拒绝，装载路径实测用）：
    restore = no_net_guard.install(msg, block_construction=True)
"""
from __future__ import annotations

import socket
from typing import Callable

DEFAULT_MSG = "[no-net] 本进程为零网络作业（频控纪律，铁律 1/7/8）：socket 已封锁"

# 出网动作全名单：TCP connect 系 + UDP sendto + 高层便捷入口。
# 收敛前各脚本只拦 connect ⇒ UDP 裸发不拦（面 4 审计 C 节缺口 5）。
_BLOCKED_METHODS = ("connect", "connect_ex", "send", "sendto", "sendall", "sendfile")


class NoNetViolation(RuntimeError):
    """守卫拦截到出网尝试（显式异常类型，供 selftest 判别，不与业务错误混淆）。"""


def _make_guard(msg: str, block_construction: bool):
    class _NoNet(socket.socket):
        if block_construction:
            def __init__(self, *a, **k):
                raise NoNetViolation(msg)

    def _deny(self, *a, **k):
        raise NoNetViolation(msg)

    if not block_construction:
        for name in _BLOCKED_METHODS:
            setattr(_NoNet, name, _deny)
    return _NoNet


def install(msg: str | None = None, block_construction: bool = False) -> Callable[[], None]:
    """装上守卫，返回 restore 可调用（幂等：restore 可重复调用）。

    block_construction=False（缺省）：socket.socket(...) 可构造但一切出网
    方法抛 NoNetViolation——与收敛前多数脚本语义一致（构造不发包）。
    block_construction=True（审计轨）：构造即抛——「装载路径不得出现任何
    socket 意图」的最严口径（原 build_panel_dlite 语义）。
    同时封锁 socket.create_connection（requests/urllib3 连接池入口）。
    """
    text = msg or DEFAULT_MSG
    guard = _make_guard(text, block_construction)
    orig_socket = socket.socket
    orig_create_connection = socket.create_connection

    def _deny_cc(*a, **k):
        raise NoNetViolation(text)

    socket.socket = guard                    # type: ignore[misc]
    socket.create_connection = _deny_cc      # type: ignore[assignment]

    def restore() -> None:
        socket.socket = orig_socket          # type: ignore[misc]
        socket.create_connection = orig_create_connection  # type: ignore[assignment]

    return restore


def selftest(msg: str = "selftest") -> tuple[bool, str]:
    """守卫行为自检（供各脚本 --selftest 复用，取代各自手写的拦截实测）。

    返回 (ok, detail)：connect 与 sendto 都必须报 NoNetViolation 而非 OS 错
    （对 127.0.0.1:1 发包：无守卫时是 ConnectionRefusedError，有守卫时是我方异常）。
    """
    restore = install(msg)
    try:
        results = {}
        for probe in ("connect", "sendto"):
            try:
                s = socket.socket()
                if probe == "connect":
                    s.connect(("127.0.0.1", 1))
                else:
                    s.sendto(b"x", ("127.0.0.1", 1))
                results[probe] = "NOT_BLOCKED"
            except NoNetViolation:
                results[probe] = "blocked"
            except OSError as e:               # 守卫失效 ⇒ OS 层错误穿透
                results[probe] = f"OS:{type(e).__name__}"
        ok = all(v == "blocked" for v in results.values())
        return ok, str(results)
    finally:
        restore()
