"""东财基金净值主源（pingzhongdata JS）——迁自 core/data_loader.fetch_pingzhongdata（V4 步 4）。

解析逻辑逐字平移（迁移不夹带行为变更），**唯一语义升级**：`Data_netWorthTrend`
解析为空数组时按 `data:` 失败处理——旧实现在单源下会静默产出空净值 fresh；
入链后换备源（sina）明显优于出空数据。`data:` 前缀不计健康度降级。

URL 由装配点以 `pz_url` 注入（provider 不读 config.json，保持无配置状态；
未注入视为装配缺陷，归 `data:`）。传输一律走 netutil（IPv4 优先 + 无视环境死代理 +
瞬断重试），与旧 `data_loader._http_get` 同通道。

payload 形状：`{"code", "name", "navs": [(date, nav), ...] 升序, "source"}`。
新浪备源无基金名称字段，`name` 回退为代码（与旧实现 fS_name 缺失时同口径）。
"""
from __future__ import annotations

import json
import re
import time

from ..base import DATA, FetchResult, classify_exc
from ... import netutil

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class EastmoneyFundNavProvider:
    name = "eastmoney"
    category = "fund_nav"
    priority = 0            # 链首：现状唯一源，保持主源地位
    timeout_s = 15.0        # 对齐旧 _http_get(timeout=15)

    def fetch(self, *, code: str, pz_url: str = "", **_ignored) -> FetchResult:
        started = time.monotonic()
        if not pz_url:
            return FetchResult(ok=False, source=self.name,
                               error=f"{DATA}pz_url 未注入（装配点缺陷）")
        try:
            # 2026-09-02 起旧实现就走 netutil；此处同通道同参数。
            text = netutil.http_get(pz_url.format(code=code),
                                    headers={"User-Agent": UA},
                                    timeout=int(self.timeout_s))
        except Exception as exc:  # noqa: BLE001 —— 契约：失败转 FetchResult，不上抛
            return FetchResult(ok=False, source=self.name,
                               error=f"{classify_exc(exc)}{type(exc).__name__}: {exc}",
                               latency_ms=int((time.monotonic() - started) * 1000))

        latency = int((time.monotonic() - started) * 1000)
        name_m = re.search(r'var fS_name = "([^"]+)"', text)
        trend_m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", text)
        if not trend_m:
            # 旧实现在此抛 ValueError(同文本)；入链后转 data: 失败（确定性，不降健康度）
            return FetchResult(ok=False, source=self.name,
                               error=f"{DATA}{code}: pingzhongdata 中未找到 Data_netWorthTrend",
                               latency_ms=latency)
        try:
            raw = json.loads(trend_m.group(1))
            navs = [(time.strftime("%Y-%m-%d", time.localtime(p["x"] / 1000)), p["y"])
                    for p in raw]
        except Exception as exc:  # noqa: BLE001
            return FetchResult(ok=False, source=self.name,
                               error=f"{classify_exc(exc)}{type(exc).__name__}: {exc}",
                               latency_ms=latency)
        if not navs:
            return FetchResult(ok=False, source=self.name,
                               error=f"{DATA}{code}: Data_netWorthTrend 为空",
                               latency_ms=latency)
        return FetchResult(
            ok=True, source=self.name, latency_ms=latency,
            payload={"code": code, "name": name_m.group(1) if name_m else code,
                     "navs": navs, "source": self.name})
