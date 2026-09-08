"""股票日 K 数据层：腾讯行情接口（前复权，免费无需 Key）。

接口：web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={mkt}{code},day,,{end},{640},qfq
- 每次最多 640 条，用 end_date 向前分页拼接
- 返回 [日期, 开, 收, 高, 低, 量]（前复权）
- 代码前缀：0=深(00/30)，1=沪(60/68)；本池持仓均为 A 股
缓存：data/stock_klines/{code}.json，TTL 内复用。
"""
import json
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PAGE = 640
MAX_PAGES = 5  # ~3200 根，覆盖 2013 年以来
_MKT = {"0": "sz", "1": "sh"}  # 东财市场前缀 → 腾讯


def _tushare_token() -> str:
    """备源 token：环境变量或项目 .env（可选配置，未配置则备源不可用）。"""
    import os
    env = {k: v for k, v in os.environ.items() if k.startswith("TUSHARE")}
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("TUSHARE_TOKEN="):
                env.setdefault("TUSHARE_TOKEN", line.split("=", 1)[1].strip())
    return (env.get("TUSHARE_TOKEN") or "").strip()


def _tushare_post(api_name: str, token: str, params: dict, fields: str) -> list[dict]:
    body = json.dumps({"api_name": api_name, "token": token, "params": params,
                       "fields": fields}).encode("utf-8")
    req = urllib.request.Request("http://api.tushare.pro", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 0:
        raise ValueError(f"tushare {api_name}: {data.get('msg')}")
    items = data["data"]["items"]
    cols = data["data"]["fields"]
    return [dict(zip(cols, it)) for it in items]


def _fetch_stock_kline_tushare(code: str, market: str, token: str) -> dict:
    """Tushare 备源：daily + adj_factor 合成前复权（腾讯源失败时启用）。"""
    ts_code = f"{code}.{'SZ' if market == '0' else 'SH'}"
    daily, offset = [], 0
    while True:
        page = _tushare_post("daily", token, {"ts_code": ts_code, "start_date": "20150101",
                                              "offset": offset, "limit": 3000},
                             "trade_date,open,high,low,close")
        daily += page
        if len(page) < 3000:
            break
        offset += 3000
    adj = _tushare_post("adj_factor", token, {"ts_code": ts_code, "start_date": "20150101"},
                        "trade_date,adj_factor")
    adj_map = {a["trade_date"]: a["adj_factor"] for a in adj}
    if daily:
        latest = max(adj_map.values())
        rows = sorted(daily, key=lambda d: d["trade_date"])
        klines = [(f"{d['trade_date'][:4]}-{d['trade_date'][4:6]}-{d['trade_date'][6:]}",
                   d["open"] * adj_map[d["trade_date"]] / latest,
                   d["close"] * adj_map[d["trade_date"]] / latest,
                   d["high"] * adj_map[d["trade_date"]] / latest,
                   d["low"] * adj_map[d["trade_date"]] / latest) for d in rows
                  if d["trade_date"] in adj_map]
        out = {"code": code, "market": market, "klines": klines, "source": "tushare"}
        return out
    raise ValueError(f"{ts_code}: tushare 无数据")


def _http_json(url: str, timeout: int = 12) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _fetch_page(symbol: str, end_date: str) -> list[list[str]]:
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={symbol},day,,{end_date},{PAGE},qfq")
    data = _http_json(url).get("data") or {}
    node = data.get(symbol) or {}
    return node.get("qfqday") or node.get("day") or []


def fetch_stock_kline(code: str, market: str, ttl_hours: float = 12.0,
                      min_start: str = "2015-01-01") -> dict:
    """market: '0' 深 / '1' 沪。返回 {code, klines: [(date, open, close, high, low), ...]} 升序。"""
    cache = BASE_DIR / "data" / "stock_klines" / f"{code}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < ttl_hours * 3600:
        return json.loads(cache.read_text(encoding="utf-8"))

    symbol = f"{_MKT[market]}{code}"   # 东财 0/1 → 腾讯 sz/sh
    pages, end_date = [], "2050-01-01"
    for _ in range(MAX_PAGES):
        try:
            page = _fetch_page(symbol, end_date)
        except Exception:
            page = []
        if not page:
            break
        pages.append(page)
        oldest = page[0][0]
        if len(page) < PAGE or oldest <= min_start:
            break
        end_date = oldest  # 下一页从更早结束（会与上页重叠 1 天，去重）
        time.sleep(0.12)
    out = None
    if pages:
        rows: dict[str, tuple] = {}
        for page in pages:
            for r in page:
                rows[r[0]] = (r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]))
        klines = [rows[d] for d in sorted(rows)]
        out = {"code": code, "market": market, "klines": klines, "source": "tencent"}
    else:
        # 备源：Tushare（需在 .env 配 TUSHARE_TOKEN）
        token = _tushare_token()
        if token and token != "你的token":
            try:
                out = _fetch_stock_kline_tushare(code, market, token)
            except Exception as e:
                raise ValueError(f"{symbol}: 腾讯源失败，Tushare 备源也失败（{e}）") from e
    if out is None or not out.get("klines"):
        raise ValueError(f"{symbol}: 未取得任何K线（腾讯源失败，且未配置可用的 TUSHARE_TOKEN 备源）")

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out
