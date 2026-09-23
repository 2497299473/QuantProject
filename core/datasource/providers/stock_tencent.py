"""腾讯日 K 主源（web.ifzq.gtimg.cn，前复权 qfq）——迁自 core/stock_data._fetch_page（V4 步 2）。

行为对齐旧实现，**除一处有意偏差（P0-2，2026-09-23 Summer 授权）**：
- 分页 640 根、最多 5 页（约 3200 根，覆盖 2013 年以来），页间 sleep 0.12s；
- 某页中途失败：**fail-closed 整链判失败**（旧实现是「保留已取到的页继续出 ok=True」——
  那会把截断历史静默写入缓存并污染下游回测，P0-2 修复改为此语义，由链上下一源补全量）；
- 跨页按日期去重后升序输出 `(date, open, close, high, low)`，与旧实现同形状。

已知源级短板（2026-08-27 记录）：部分科创板股（688382/688266/688428/688192 等，
均为 022853 重仓）稳定无数据 → 按 DATA 类失败处理：不降级、由链上东财源接手。
"""
from __future__ import annotations

import time

from ..base import DATA, FetchResult, classify_exc
from ..symbols import tencent_symbol
from ... import netutil

PAGE = 640
MAX_PAGES = 5
PAGE_SLEEP_S = 0.12     # 源级频控纪律：页间节流
_END_DATE = "2050-01-01"


def _fetch_page(symbol: str, end_date: str, *, timeout: int) -> list[list[str]]:
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={symbol},day,,{end_date},{PAGE},qfq")
    data = netutil.http_get_json(url, timeout=timeout).get("data") or {}
    node = data.get(symbol) or {}
    return node.get("qfqday") or node.get("day") or []


class TencentKlineProvider:
    name = "tencent"
    category = "stock_kline"
    priority = 0            # 链首：旧实现的主源
    timeout_s = 12.0

    def fetch(self, *, code: str, market: str,
              min_start: str = "2015-01-01", **_ignored) -> FetchResult:
        started = time.monotonic()
        symbol = tencent_symbol(code, market)
        pages: list[list[list[str]]] = []
        err: BaseException | None = None
        end_date = _END_DATE
        for _ in range(MAX_PAGES):
            try:
                page = _fetch_page(symbol, end_date, timeout=int(self.timeout_s))
            except Exception as exc:  # noqa: BLE001 —— 契约：失败转 FetchResult，不上抛
                err = exc
                break
            if not page:
                break
            pages.append(page)
            oldest = page[0][0]
            if len(page) < PAGE or oldest <= min_start:
                break
            end_date = oldest
            time.sleep(PAGE_SLEEP_S)

        latency = int((time.monotonic() - started) * 1000)
        if err is not None:
            # P0-2（2026-09-23 授权）：任一页失败 ⇒ 不返回半截数据，整源判失败。
            # 前缀仍由 classify_exc 定（网络类计入健康度连续失败 → 达阈值降级到链尾）。
            kind = classify_exc(err)
            if pages:
                msg = (f"{kind}partial_page: got={len(pages)}/{MAX_PAGES} "
                       f"before {type(err).__name__}: {err}")
            else:
                msg = f"{kind}{type(err).__name__}: {err}"
            return FetchResult(ok=False, source=self.name,
                               error=msg, latency_ms=latency)
        if not pages:
            return FetchResult(ok=False, source=self.name,
                               error=f"{DATA}{symbol}: 腾讯源无 K 线", latency_ms=latency)

        rows: dict[str, tuple] = {}
        for page in pages:
            for r in page:
                rows[r[0]] = (r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]))
        klines = [rows[d] for d in sorted(rows)]
        if not klines:
            return FetchResult(ok=False, source=self.name,
                               error=f"{DATA}{symbol}: 腾讯源无 K 线", latency_ms=latency)
        return FetchResult(
            ok=True, source=self.name, latency_ms=latency,
            payload={"code": code, "market": market, "klines": klines,
                     "source": self.name})
