"""Tushare 日 K 可选备源（daily + adj_factor 合成前复权）——迁自 core/stock_data（V4 步 2）。

仅在 `.env` 配好 `TUSHARE_TOKEN`（或同名环境变量）时启用；未配置返回 SKIP 类失败：
**不算失败业务、不计入健康度降级**，只让链继续按「全灭」路径收尾。

token 解析沿用 2026-08-27 修复后的健壮版（兼容 export 前缀 / 引号包裹 / CRLF /
UTF-8 BOM / 行内注释），旧版解析有缺陷会让备源永远空转。

与前序实现的两处**记录在案的差异**（均为标签修正，失败/成功集合不变）：
1. `daily` 有数据但 `adj_factor` 为空 → 旧实现抛 `max() arg is an empty sequence`
   被归为网络类；现归 DATA 类（确定性失败，不降级）。
2. Tushare 返回 `code != 0`（token 无效 / 积分不足）→ 归 DATA 类，不再算网络抖动。

注：Tushare 不进入任何**决策**备选（MEMORY.md 2026-09-17：积分通道不在决策范围），
此处仅作日 K 取数的最后兜底。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..base import DATA, SKIP, FetchResult, classify_exc
from ..symbols import tushare_code
from ... import netutil

_PROJECT_ROOT = Path(__file__).resolve().parents[3]   # .../QuantV1
START_DATE = "20150101"
PAGE_LIMIT = 3000


class TushareApiError(ValueError):
    """Tushare 返回 code != 0：确定性失败（token / 积分 / 参数），非网络抖动。"""


def _tushare_token() -> str:
    """读取 token：环境变量优先，其次项目 .env（健壮解析，2026-08-27 修复版）。"""
    val = (os.environ.get("TUSHARE_TOKEN") or "").strip().strip('"').strip("'")
    if val:
        return val
    env_path = _PROJECT_ROOT / ".env"
    if not env_path.exists():
        return ""
    try:
        text = env_path.read_text(encoding="utf-8-sig")   # -sig 去 BOM
    except OSError:
        return ""
    for raw in text.splitlines():
        line = raw.strip().rstrip("\r")
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, v = line.partition("=")
        if key.strip() != "TUSHARE_TOKEN":
            continue
        v = v.strip().strip('"').strip("'")
        v = v.split(" #", 1)[0].strip()      # 去行内注释尾巴
        return v
    return ""


def _post(api_name: str, token: str, params: dict, fields: str, *,
          timeout: int) -> list[dict]:
    body = json.dumps({"api_name": api_name, "token": token, "params": params,
                       "fields": fields}).encode("utf-8")
    data = netutil.http_post_json("https://api.tushare.pro", body,
                                  headers={"Content-Type": "application/json"},
                                  timeout=timeout)
    if data.get("code") != 0:
        raise TushareApiError(f"tushare {api_name}: {data.get('msg')}")
    items = data["data"]["items"]
    cols = data["data"]["fields"]
    return [dict(zip(cols, it)) for it in items]


def _fmt(trade_date: str) -> str:
    return f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"


def _fetch_klines(code: str, market: str, token: str, *, timeout: int) -> list[tuple]:
    """daily + adj_factor 合成前复权；空结果返回 []（由调用方转 DATA 类失败）。"""
    ts_code = tushare_code(code, market)
    daily: list[dict] = []
    offset = 0
    while True:
        page = _post("daily", token,
                     {"ts_code": ts_code, "start_date": START_DATE,
                      "offset": offset, "limit": PAGE_LIMIT},
                     "trade_date,open,high,low,close", timeout=timeout)
        daily += page
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    adj = _post("adj_factor", token,
                {"ts_code": ts_code, "start_date": START_DATE},
                "trade_date,adj_factor", timeout=timeout)
    adj_map = {a["trade_date"]: a["adj_factor"] for a in adj}
    if not daily or not adj_map:
        return []
    latest = max(adj_map.values())
    rows = sorted(daily, key=lambda d: d["trade_date"])
    return [(_fmt(d["trade_date"]),
             d["open"] * adj_map[d["trade_date"]] / latest,
             d["close"] * adj_map[d["trade_date"]] / latest,
             d["high"] * adj_map[d["trade_date"]] / latest,
             d["low"] * adj_map[d["trade_date"]] / latest)
            for d in rows if d["trade_date"] in adj_map]


class TushareKlineProvider:
    name = "tushare"
    category = "stock_kline"
    priority = 2            # 链尾：可选兜底
    timeout_s = 20.0

    def fetch(self, *, code: str, market: str, **_ignored) -> FetchResult:
        started = time.monotonic()
        token = _tushare_token()
        if not token:
            return FetchResult(ok=False, source=self.name,
                               error=f"{SKIP}TUSHARE_TOKEN 未配置",
                               latency_ms=int((time.monotonic() - started) * 1000))
        try:
            klines = _fetch_klines(code, market, token, timeout=int(self.timeout_s))
        except TushareApiError as exc:
            return FetchResult(ok=False, source=self.name, error=f"{DATA}{exc}",
                               latency_ms=int((time.monotonic() - started) * 1000))
        except Exception as exc:  # noqa: BLE001 —— 契约：失败转 FetchResult，不上抛
            return FetchResult(ok=False, source=self.name,
                               error=f"{classify_exc(exc)}{type(exc).__name__}: {exc}",
                               latency_ms=int((time.monotonic() - started) * 1000))
        latency = int((time.monotonic() - started) * 1000)
        if not klines:
            return FetchResult(ok=False, source=self.name,
                               error=f"{DATA}{tushare_code(code, market)}: tushare 无数据",
                               latency_ms=latency)
        return FetchResult(
            ok=True, source=self.name, latency_ms=latency,
            payload={"code": code, "market": market, "klines": klines,
                     "source": self.name})
