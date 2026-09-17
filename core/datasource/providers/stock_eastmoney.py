"""东财日 K 备源（push2his，前复权 fqt=1）——迁自 core/stock_data（V4 步 2）。

**必须保持 HTTP 明文**（2026-09-02 实测）：东财 HTTPS 被 TLS 指纹过滤掐断
（握手过、请求即断，Python / curl / curl_cffi 四指纹同拦），HTTP 通道 200 全通
且数据与缓存逐项一致。

无 key，与持仓页同域。输出与腾讯源同形状：`(date, open, close, high, low)` 升序。
"""
from __future__ import annotations

import time

from ..base import DATA, FetchResult, classify_exc
from ..symbols import eastmoney_secid
from ... import netutil


class EastmoneyKlineProvider:
    name = "eastmoney"
    category = "stock_kline"
    priority = 1            # 链上次序：腾讯失败后的第一备源
    timeout_s = 15.0

    def fetch(self, *, code: str, market: str,
              min_start: str = "2015-01-01", **_ignored) -> FetchResult:
        started = time.monotonic()
        beg = min_start.replace("-", "")
        url = ("http://push2his.eastmoney.com/api/qt/stock/kline/get"
               f"?secid={eastmoney_secid(code, market)}"
               "&klt=101&fqt=1"
               f"&beg={beg}&end=20500101"
               "&fields1=f1,f2,f3,f4,f5,f6"
               "&fields2=f51,f52,f53,f54,f55,f56")
        try:
            data = netutil.http_get_json(url, timeout=int(self.timeout_s))
        except Exception as exc:  # noqa: BLE001 —— 契约：失败转 FetchResult，不上抛
            return FetchResult(ok=False, source=self.name,
                               error=f"{classify_exc(exc)}{type(exc).__name__}: {exc}",
                               latency_ms=int((time.monotonic() - started) * 1000))

        kl = ((data.get("data") or {}).get("klines")) or []
        klines = []
        for row in kl:
            parts = row.split(",")
            if len(parts) >= 5:
                klines.append((parts[0], float(parts[1]), float(parts[2]),
                               float(parts[3]), float(parts[4])))
        latency = int((time.monotonic() - started) * 1000)
        if not klines:
            return FetchResult(ok=False, source=self.name,
                               error=f"{DATA}em.{code}: 无K线", latency_ms=latency)
        return FetchResult(
            ok=True, source=self.name, latency_ms=latency,
            payload={"code": code, "market": market, "klines": klines,
                     "source": self.name})
