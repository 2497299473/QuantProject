"""腾讯实时行情源（qt.gtimg.cn）——迁自 core/real_time.py（V4 步 3）。

行为逐项对齐旧实现（迁移不夹带改动）：
- URL 拼接、GBK 解码、`;` 分行、`~` 分列、`len(parts) >= 33 and len(parts[2]) == 6`
  过滤、price<=0 丢行、ValueError/IndexError 单行跳过——一字不动；
- **语义升级（本步唯一改动）**：旧实现失败静默返回空 dict；新契约失败返回
  `FetchResult(ok=False, error=带前缀文本)`，由装配点决定降级形状。
  「拉到 0 条报价」也按失败处理（`data:` 前缀）——旧实现同样返回空 dict，
  但静默不可诊断正是步 3 要消灭的问题。

payload 形状：`{"quotes": {code: {name, price, change_pct, time}}}`。
holdings 后处理（pct/market 回填）归装配点，provider 不认识持仓。
"""
from __future__ import annotations

import time

from ..base import DATA, FetchResult, classify_exc
from ... import netutil

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def parse_quote_text(text: str) -> dict[str, dict]:
    """腾讯原始格式：v_sz300308="51~中际旭创~300308~846.00~...~时间~涨跌额~涨跌幅~...
    parts[0] 含前缀（v_sz300308="51 / \nv_sh688167="1），parts[2] 才是纯 6 位代码。
    """
    quotes: dict[str, dict] = {}
    for line in text.split(";"):
        if "~" not in line:
            continue
        parts = line.strip().split("~")
        if len(parts) < 33 or len(parts[2]) != 6:
            continue
        code = parts[2]
        try:
            price = float(parts[3]) if parts[3] else 0.0
            chg = float(parts[32]) if parts[32] else 0.0
            if price <= 0:
                continue
            quotes[code] = {"name": parts[1], "price": price,
                            "change_pct": chg, "time": parts[30]}
        except (ValueError, IndexError):
            continue
    return quotes


class RealtimeTencentProvider:
    name = "tencent"
    category = "realtime_quote"
    priority = 0            # 本类别唯一源（备源接入是后续步骤的事）
    timeout_s = 10.0        # 对齐旧实现 netutil timeout=10

    def fetch(self, *, symbols: str, **_ignored) -> FetchResult:
        """symbols: 逗号连接的腾讯符号串，如 'sh600519,sz000651'（装配点拼好）。

        2026-09-02 起走 netutil（IPv4 优先 + 无视环境死代理 + 瞬断重试），保持不动。
        """
        started = time.monotonic()
        url = f"https://qt.gtimg.cn/q={symbols}"
        try:
            text = netutil.http_get_bytes(url, headers={"User-Agent": UA},
                                          timeout=int(self.timeout_s)
                                          ).decode("gbk", errors="replace")
        except Exception as exc:  # noqa: BLE001 —— 契约：失败转 FetchResult，不上抛
            return FetchResult(ok=False, source=self.name,
                               error=f"{classify_exc(exc)}{type(exc).__name__}: {exc}",
                               latency_ms=int((time.monotonic() - started) * 1000))

        quotes = parse_quote_text(text)
        latency = int((time.monotonic() - started) * 1000)
        if not quotes:
            return FetchResult(ok=False, source=self.name,
                               error=f"{DATA}拉取成功但解析出 0 条报价（疑似上游拦截页/格式变更）",
                               latency_ms=latency)
        return FetchResult(ok=True, source=self.name,
                           payload={"quotes": quotes}, latency_ms=latency)
