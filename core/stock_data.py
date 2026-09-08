"""股票日 K 数据层：多源容错（腾讯主源 → 东财备源 → Tushare 可选备源）。

数据源链（2026-08-27 重构）：
1. 腾讯 web.ifzq.gtimg.cn（前复权 qfq，分页 640 条）
2. 东财 push2his 免费接口（前复权 fqt=1，一次全量）——修复背景：
   腾讯源对部分科创板股（688382/688266/688428/688192 等，均为 022853 重仓）
   稳定失败，且旧版 _tushare_token() 解析 .env 有 bug 导致备源永远空转，
   造成 022853 在回测中 0 样本。东财源无 key、与持仓同域已验证可达。
3. Tushare daily+adj_factor 合成前复权（需 .env 配置 TUSHARE_TOKEN）

统一输出 {code, market, klines: [(date, open, close, high, low), ...]}（升序、前复权）。
缓存 data/stock_klines/{code}.json，TTL 内复用；source 字段标注实际来源。
"""
import json
import os
import time
from pathlib import Path

from . import netutil

BASE_DIR = Path(__file__).resolve().parent.parent
PAGE = 640
MAX_PAGES = 5  # ~3200 根，覆盖 2013 年以来
_MKT = {"0": "sz", "1": "sh"}          # 东财市场前缀 → 腾讯
_EM_SECID = {"0": "0", "1": "1"}       # 东财市场前缀 → 东财 secid 前缀（深0 沪1）


def _tushare_token() -> str:
    """备源 token：环境变量或项目 .env。

    健壮解析（2026-08-27 修复）：兼容 export 前缀 / 引号包裹 / CRLF / UTF-8 BOM /
    行内注释后缀；此前实现存在解析缺陷，即使 .env 配好 token 也返回空，
    导致腾讯源失败时备源从未真正启用过。
    """
    val = (os.environ.get("TUSHARE_TOKEN") or "").strip().strip('"').strip("'")
    if val:
        return val
    env_path = BASE_DIR / ".env"
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
        # 去掉行内注释尾巴（仅当值非引号包裹时可能出现）
        v = v.split(" #", 1)[0].strip()
        return v
    return ""


def _tushare_post(api_name: str, token: str, params: dict, fields: str) -> list[dict]:
    body = json.dumps({"api_name": api_name, "token": token, "params": params,
                       "fields": fields}).encode("utf-8")
    data = netutil.http_post_json("http://api.tushare.pro", body,
                                  headers={"Content-Type": "application/json"})
    if data.get("code") != 0:
        raise ValueError(f"tushare {api_name}: {data.get('msg')}")
    items = data["data"]["items"]
    cols = data["data"]["fields"]
    return [dict(zip(cols, it)) for it in items]


def _fetch_stock_kline_tushare(code: str, market: str, token: str) -> dict:
    """Tushare 备源：daily + adj_factor 合成前复权。"""
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
        return {"code": code, "market": market, "klines": klines, "source": "tushare"}
    raise ValueError(f"{ts_code}: tushare 无数据")


def _http_json(url: str, timeout: int = 12) -> dict:
    # 2026-09-02: 走 netutil（IPv4 优先 + 无视环境死代理 + 瞬断重试）。
    # 背景：push2his 双栈域名 IPv6 路径 100% 断连、WSL autoProxy 注入死代理 7892，
    # 详见 Obsidian《基金日频参谋-数据源修复与板块探测-20260902》。
    return netutil.http_get_json(url, timeout=timeout)


def _fetch_page(symbol: str, end_date: str) -> list[list[str]]:
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={symbol},day,,{end_date},{PAGE},qfq")
    data = _http_json(url).get("data") or {}
    node = data.get(symbol) or {}
    return node.get("qfqday") or node.get("day") or []


def _fetch_stock_kline_eastmoney(code: str, market: str,
                                 min_start: str = "2015-01-01") -> dict:
    """东财免费日K备源（前复权 fqt=1）。无 key，与持仓页同域名已验证 WSL 可达。

    返回行格式与腾讯源一致：(date, open, close, high, low) 升序。
    """
    beg = min_start.replace("-", "")
    # 2026-09-02 晚：东财 HTTPS 被 TLS 指纹过滤掐断（握手过、请求即断，Python/curl
    # 同拦），HTTP 明文通道 200 全通且数据与缓存逐项一致，降级 HTTP。
    url = ("http://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={_EM_SECID[market]}.{code}"
           "&klt=101&fqt=1"
           f"&beg={beg}&end=20500101"
           "&fields1=f1,f2,f3,f4,f5,f6"
           "&fields2=f51,f52,f53,f54,f55,f56")
    data = _http_json(url, timeout=15)
    kl = ((data.get("data") or {}).get("klines")) or []
    klines = []
    for row in kl:
        parts = row.split(",")
        if len(parts) >= 5:
            klines.append((parts[0], float(parts[1]), float(parts[2]),
                           float(parts[3]), float(parts[4])))
    if not klines:
        raise ValueError(f"em.{code}: 无K线")
    return {"code": code, "market": market, "klines": klines, "source": "eastmoney"}


def fetch_stock_kline(code: str, market: str, ttl_hours: float = 12.0,
                      min_start: str = "2015-01-01") -> dict:
    """market: '0' 深 / '1' 沪。返回 {code, klines: [(date, o, c, h, l), ...]} 升序。

    源顺序：缓存 → 腾讯 → 东财 → Tushare(可选)。全部失败抛 ValueError，
    错误信息列出尝试过的每个源的具体原因（可诊断性优先）。
    """
    cache = BASE_DIR / "data" / "stock_klines" / f"{code}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < ttl_hours * 3600:
        return json.loads(cache.read_text(encoding="utf-8"))

    symbol = f"{_MKT[market]}{code}"
    attempts: list[str] = []

    # --- 主源：腾讯（分页拼接） ---
    out = None
    pages = []
    end_date = "2050-01-01"
    for _ in range(MAX_PAGES):
        try:
            page = _fetch_page(symbol, end_date)
        except Exception as e:
            attempts.append(f"tencent:{type(e).__name__}:{e}")
            page = []
        if not page:
            break
        pages.append(page)
        oldest = page[0][0]
        if len(page) < PAGE or oldest <= min_start:
            break
        end_date = oldest
        time.sleep(0.12)

    if pages:
        rows: dict[str, tuple] = {}
        for page in pages:
            for r in page:
                rows[r[0]] = (r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]))
        klines = [rows[d] for d in sorted(rows)]
        out = {"code": code, "market": market, "klines": klines, "source": "tencent"}

    # --- 备源 1：东财免费接口 ---
    if out is None or not out.get("klines"):
        try:
            out = _fetch_stock_kline_eastmoney(code, market, min_start)
        except Exception as e:
            attempts.append(f"eastmoney:{type(e).__name__}:{e}")

    # --- 备源 2：Tushare（可选，未配置跳过并在错误信息中说明） ---
    if out is None or not out.get("klines"):
        token = _tushare_token()
        if token:
            try:
                out = _fetch_stock_kline_tushare(code, market, token)
            except Exception as e:
                attempts.append(f"tushare:{type(e).__name__}:{e}")
        else:
            attempts.append("tushare:skip(TUSHARE_TOKEN 未配置)")

    if out is None or not out.get("klines"):
        raise ValueError(f"{symbol}: 全部数据源失败（{' | '.join(attempts)}）")

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out
