"""网络工具层（2026-09-02 新增，v3）：修复 WSL 数据源双故障。

背景（2026-09-02 实测，详见 Obsidian `量化交易工具/基金日频参谋-数据源修复与板块探测-20260902.md`）：
1. **东财 CDN 节点好坏混杂**：push2/push2his 解析出 v4/v6 多地址——
   v6 端点稳定「TCP/TLS 握手成功但请求后 0 字节即断」（RemoteDisconnected），
   v4 端点也随机好坏（实测 103.220.167.80 → 200 / 61.129.129.199 → 0 字节）。
   DNS 轮询导致每次解析可能拿到不同节点，CPython 无 happy-eyeballs，
   只连第一个地址 → 命中坏节点即整请求失败。
   → v3 方案：本模块自己解析**全部**地址（v4 优先、v6 兜底），逐 IP 建连；
     命中坏节点（对端断连/SSL 断/超时）**自动 failover 到下一个 IP**，
     全部 IP 失败才上抛。4xx/5xx（服务端明确响应）不 failover。
2. **死代理注入**：WSL autoProxy 把 Windows 系统代理（127.0.0.1:7892）同步进
   http_proxy 环境变量；代理客户端未运行时 urllib 全部 Connection refused。
   → 本模块主链路不走 urllib opener，完全不读环境代理；数据源（腾讯/东财/
     Tushare）全部国内直连，不需要代理。

API：
- http_get(url, *, headers=None, timeout=15, retries=2, backoff=1.0) -> str
- http_get_bytes(url, ...) -> bytes（GBK 响应如腾讯 qt.gtimg.cn 用它再自行 decode）
- http_get_json(url, ...) -> dict
- http_post_json(url, data: bytes, *, headers=None, timeout=20, retries=1) -> dict
- NO_PROXY_OPENER：无代理 opener（飞书推送等特殊调用；不带本模块 DNS/重试逻辑）
- curl_cffi 浏览器指纹兜底（2026-09-04 v4 新增）：原生栈逐 IP failover +
  时间重试全部穷尽、且错误属连接层（断连/SSL/超时/reset）时，自动改用
  curl_cffi(chrome 指纹) 再试一次；仅 GET，4xx/5xx 与 ECONNREFUSED 不兜底。
  背景：2026-09-03 起东财对 push2his/push2 的 kline/get 做接口级拦截，
  当日 chrome/edge/safari/firefox 指纹亦被掐；保留兜底等其策略回松。

  v5（2026-09-04 夜实测）：curl/curl_cffi 四指纹、编号子域名(92./33./7.)、
  ut token 全部连接层被掐，但真实浏览器可正常加载 K 线图 → 假设服务端要求
  「先访问主页种会话 cookie」的请求链路。本模块用 curl_cffi chrome 指纹访问
  quote 主页种 cookie（进程内一次、TTL 30min），对 eastmoney.com 域请求注入
  Cookie+Referer（原生逐 IP 路径与 curl_cffi 兜底路径均注入）；原生链路耗尽
  且属连接层错误、距上次种 cookie 超 300s 时强制重种再走一轮。种不到 cookie
  自动降级 v4 行为（注入跳过、不重试主页）。
  v5 实测结论（2026-09-04 23:5x，cookie 假设被否证）：
  - quote.eastmoney.com 主页 200 但 cookies:[]（不种任何会话 cookie），
    带/不带 Cookie 头接口均在连接层被掐 → 本路径为死代码，无害保留；
  - Windows 宿主 curl.exe（与浏览器同网络出口，schannel）同样
    "server closed abruptly" → 拦截在客户端指纹（TLS/JA3/JA4 或 h2）层，
    与 IP/WSL/cookie 均无关；真实 Chromium 是目前唯一确认可通过的路径
    （Playwright/自托管 Firecrawl），换数据源（腾讯 K 线）可彻底绕开。

重试/容错边界（诚实记录）：
- 连接层瞬断（断连/重置/SSL 层/超时）→ 先 IP failover（同请求内），
  再时间重试（retries 次，backoff 递增）；
- HTTP 4xx/5xx 不重试不 failover——服务端已明确响应；
- ConnectionRefused（无人监听）不重试——立即失败；
- 仅用于幂等调用（数据 GET / Tushare 只读查询）。推送类请勿用重试 API。
"""
from __future__ import annotations

import errno
import http.client
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

_DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# ------------------------------------------------ 东财 cookie 会话（v5）
_EM_SESSION_URL = "https://quote.eastmoney.com/"
_EM_HOSTS = ("eastmoney.com",)
_EM_COOKIE_TTL = 1800.0     # 同 cookie 复用窗口 30 分钟
_REPRIME_GAP = 300.0        # 原生链路耗尽后，距上次种 cookie 超过 5 分钟才重新种
_em_cookie = ""             # "k=v; k2=v2"
_em_cookie_at = 0.0
_em_last_prime = 0.0


def _em_host_ok(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in _EM_HOSTS)


def prime_eastmoney_session(force: bool = False) -> bool:
    """访问东财行情主页种会话 cookie（curl_cffi chrome 指纹）。

    成功返回 True；失败保留旧 cookie（若有）。TTL 窗口内不重复访问；
    force=True 强制重种。任何异常都只降级、不上抛（保持 v4 行为）。
    """
    global _em_cookie, _em_cookie_at, _em_last_prime
    now = time.time()
    _em_last_prime = now
    if not force and _em_cookie and now - _em_cookie_at < _EM_COOKIE_TTL:
        return True
    cc = _load_curl_cffi()
    if cc is None:
        return bool(_em_cookie)
    try:
        r = cc.get(_EM_SESSION_URL, impersonate="chrome", timeout=15)
        pairs: list[str] = []
        cookies = getattr(r, "cookies", None)
        if isinstance(cookies, dict):
            pairs = [f"{k}={v}" for k, v in cookies.items() if v]
        elif cookies:
            pairs = [f"{c.name}={c.value}" for c in cookies]
        if not pairs:
            for hk, hv in list(r.headers.items()):
                if hk.lower() == "set-cookie" and "=" in hv:
                    pairs.append(hv.split(";", 1)[0].strip())
        if pairs:
            _em_cookie = "; ".join(pairs)
            _em_cookie_at = now
            return True
        return bool(_em_cookie)
    except Exception:  # noqa: BLE001
        return bool(_em_cookie)


def _em_session_headers() -> dict:
    return {"Cookie": _em_cookie,
            "Referer": "https://quote.eastmoney.com/"}


# ------------------------------------------------ curl_cffi 指纹兜底（v4）

_CURL_IMPERSONATE = ("chrome",)
_curl_cffi_mod = None
_curl_cffi_checked = False


def _load_curl_cffi():
    """惰性加载 curl_cffi；缺失/损坏时缓存 None，不再重复尝试导入。"""
    global _curl_cffi_mod, _curl_cffi_checked
    if _curl_cffi_checked:
        return _curl_cffi_mod
    _curl_cffi_checked = True
    try:
        from curl_cffi import requests as _cc  # noqa: PLC0415
        _curl_cffi_mod = _cc
    except Exception:  # noqa: BLE001
        _curl_cffi_mod = None
    return _curl_cffi_mod


def _curl_cffi_get(url: str, headers: dict, timeout: int) -> bytes:
    """浏览器指纹 GET 兜底；结果语义与原生路径一致（4xx/5xx 抛 HTTPError）。"""
    cc = _load_curl_cffi()
    if _em_host_ok(urllib.parse.urlsplit(url).hostname or "") and _em_cookie:
        headers = dict(headers)
        headers.setdefault("Cookie", _em_cookie)
        headers.setdefault("Referer", "https://quote.eastmoney.com/")
    if cc is None:
        raise RuntimeError("curl_cffi unavailable")
    last: Exception | None = None
    for imp in _CURL_IMPERSONATE:
        try:
            r = cc.get(url, headers=headers, timeout=timeout,
                       impersonate=imp)
            if r.status_code >= 400:
                raise urllib.error.HTTPError(
                    url, r.status_code, r.reason or "", {}, None)
            return r.content
        except urllib.error.HTTPError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
    assert last is not None
    raise last


# ---------------------------------------------------------------- DNS 解析

_orig_getaddrinfo = socket.getaddrinfo


def _sort_v4_first(infos: list) -> list:
    """同族内保持原顺序，v4 整体排在 v6 前（稳定排序）。"""
    if len(infos) > 1:
        infos = sorted(infos, key=lambda i: 0 if i[0] == socket.AF_INET else 1)
    return infos


def _getaddrinfo_v4_first(host, port, family=0, type=0, proto=0, flags=0):
    return _sort_v4_first(_orig_getaddrinfo(host, port, family, type, proto, flags))


def _install_dns_patch() -> None:
    """给 socket.getaddrinfo 加 v4 优先排序（对 Python 层调用方生效）。

    注意：C 层连接（http.client/ssl 内部解析）不走此入口；本模块真正的连接
    控制由 _resolve_candidates + 逐 IP 建连完成。此补丁仅作第三方库兜底。
    """
    if getattr(socket.getaddrinfo, "_quantv1_v4_first", False):
        return
    _getaddrinfo_v4_first._quantv1_v4_first = True  # type: ignore[attr-defined]
    socket.getaddrinfo = _getaddrinfo_v4_first


_install_dns_patch()


def _resolve_candidates(host: str, port: int) -> list[tuple]:
    """全部候选 (family, ip)：v4 优先、v6 兜底，去重，保持族内原顺序。"""
    infos = _sort_v4_first(
        _orig_getaddrinfo(host, port, proto=socket.IPPROTO_TCP))
    out: list[tuple] = []
    seen = set()
    for fam, ip in ((i[0], i[4][0]) for i in infos):
        if (fam, ip) not in seen:
            seen.add((fam, ip))
            out.append((fam, ip))
    if not out:
        raise socket.gaierror(f"无法解析 {host}")
    return out


# ---------------------------------------------------------------- 连接构造
# http.client 预置 sock 模式：自己选定 IP 建 TCP + TLS（SNI 保留证书校验），
# conn.sock 非 None 时 http.client 跳过 connect()，彻底绕开 C 层 DNS。

_ssl_ctx = ssl.create_default_context()


def _conn_for(host: str, port: int, fam: int, ip: str,
              is_https: bool, timeout) -> http.client.HTTPConnection:
    raw = socket.create_connection((ip, port), timeout=timeout)
    if is_https:
        raw = _ssl_ctx.wrap_socket(raw, server_hostname=host)
    conn = (http.client.HTTPSConnection(host, timeout=timeout) if is_https
            else http.client.HTTPConnection(host, timeout=timeout))
    conn.sock = raw
    return conn


def _request_once(url: str, data: bytes | None, headers: dict,
                  timeout: int) -> bytes:
    """单次逻辑请求：逐 IP failover。

    连接层错误（断连/SSL/超时/reset）→ 换下一个 IP 重试；
    4xx/5xx → 立即抛 HTTPError（不 failover，服务端已明确响应）。
    """
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname
    is_https = parsed.scheme == "https"
    port = parsed.port or (443 if is_https else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    if _em_host_ok(host) and _em_cookie:
        headers.setdefault("Cookie", _em_cookie)
        headers.setdefault("Referer", "https://quote.eastmoney.com/")

    last: Exception | None = None
    for fam, ip in _resolve_candidates(host, port):
        conn = None
        try:
            conn = _conn_for(host, port, fam, ip, is_https, timeout)
            conn.request("POST" if data is not None else "GET",
                         path, body=data, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
            if resp.status >= 400:
                raise urllib.error.HTTPError(
                    url, resp.status, resp.reason or "", {}, None)
            return body
        except urllib.error.HTTPError:
            raise
        except Exception as e:  # noqa: BLE001 —— 断连/SSL/超时/reset → failover
            last = e
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    assert last is not None
    raise last


def _is_retryable(exc: BaseException) -> bool:
    """连接层瞬断 → True；HTTP 4xx/5xx、ECONNREFUSED → False。"""
    if isinstance(exc, urllib.error.HTTPError):
        return False

    def _refused(r: object) -> bool:
        return isinstance(r, ConnectionRefusedError) or (
            isinstance(r, OSError) and r.errno == errno.ECONNREFUSED)

    if isinstance(exc, urllib.error.URLError):
        return not _refused(exc.reason)
    if isinstance(exc, (http.client.HTTPException, ssl.SSLError)):
        return True
    if isinstance(exc, (socket.timeout, TimeoutError, OSError)):
        return not _refused(exc)
    return False


def _open_with_retry(url: str, data: bytes | None, headers: dict,
                     timeout: int, retries: int, backoff: float) -> bytes:
    last: Exception | None = None
    host = urllib.parse.urlsplit(url).hostname or ""
    if _em_host_ok(host):
        prime_eastmoney_session()
    for attempt in range(retries + 1):
        try:
            return _request_once(url, data, headers, timeout)
        except Exception as e:  # noqa: BLE001 —— 统一判定后原样上抛
            last = e
            if attempt < retries and _is_retryable(e):
                time.sleep(backoff * (attempt + 1))
                continue
            break
    assert last is not None
    # 原生栈穷尽且属连接层错误 → cookie 超 300s 则强制重种再走一轮（v5）。
    if (_em_host_ok(host) and data is None and retries > 0
            and _is_retryable(last)
            and time.time() - _em_last_prime > _REPRIME_GAP):
        if prime_eastmoney_session(force=True):
            try:
                return _request_once(url, data, headers, timeout)
            except Exception:  # noqa: BLE001 —— 重种后仍失败保持原语义
                pass
    # 原生栈穷尽且属连接层错误 → 浏览器指纹兜底一次（仅 GET；POST 不兜底）。
    # retries=0：调用方显式要求"瞬断直接上抛、不吞错"，兜底一并跳过（契约优先）。
    if data is None and retries > 0 and _is_retryable(last):
        try:
            return _curl_cffi_get(url, headers, timeout)
        except Exception:  # noqa: BLE001 —— 兜底失败保持原语义：上抛原生异常
            pass
    raise last


def http_get_bytes(url: str, *, headers: dict | None = None,
                   timeout: int = 15, retries: int = 2,
                   backoff: float = 1.0) -> bytes:
    h = dict(headers or {})
    h.setdefault("User-Agent", _DEFAULT_UA)
    return _open_with_retry(url, None, h, timeout, retries, backoff)


def http_get(url: str, *, headers: dict | None = None,
             timeout: int = 15, retries: int = 2,
             backoff: float = 1.0) -> str:
    return http_get_bytes(url, headers=headers, timeout=timeout,
                          retries=retries, backoff=backoff).decode(
                              "utf-8", errors="replace")


def http_get_json(url: str, *, headers: dict | None = None,
                  timeout: int = 15, retries: int = 2,
                  backoff: float = 1.0) -> dict:
    return json.loads(http_get(url, headers=headers, timeout=timeout,
                               retries=retries, backoff=backoff))


def http_post_json(url: str, data: bytes, *, headers: dict | None = None,
                   timeout: int = 20, retries: int = 1,
                   backoff: float = 1.0) -> dict:
    h = dict(headers or {})
    h.setdefault("User-Agent", _DEFAULT_UA)
    return json.loads(_open_with_retry(url, data, h, timeout,
                                       retries, backoff).decode(
                                           "utf-8", errors="replace"))


# 给「需要绕开环境代理但不需要本模块 DNS/重试」的调用（飞书 webhook 推送）。
NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
