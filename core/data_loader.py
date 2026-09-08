"""数据层：东方财富免费接口（无需 API Key）。

两个数据源（均已在 2026-08-22 会话验证可用）：
1. pingzhongdata/{code}.js —— 全量历史净值（含基金名称/费率等元信息）
2. api.fund.eastmoney.com/f10/lsjz —— 净值明细 + 申购/赎回状态（需带 Referer）

已知失效接口（不要使用）：fundgz 实时估值（404）、fundmobapi（网络繁忙）。
本地缓存 data/klines/{code}.json，TTL 内复用，避免同一运行日重复抓取。
"""
import json
import re
import time
from pathlib import Path

from . import netutil

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
BASE_DIR = Path(__file__).resolve().parent.parent


def _http_get(url: str, referer: str | None = None, timeout: int = 15) -> str:
    # 2026-09-02: 走 netutil（IPv4 优先 + 无视环境死代理 + 瞬断重试）。
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    return netutil.http_get(url, headers=headers, timeout=timeout)


def _load_config() -> dict:
    return json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))


def _cache_path(code: str) -> Path:
    return BASE_DIR / "data" / "klines" / f"{code}.json"


def _cache_fresh(path: Path, ttl_hours: float) -> bool:
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < ttl_hours * 3600


def fetch_pingzhongdata(code: str) -> dict:
    """解析 pingzhongdata JS，返回 {name, navs: [(date, nav), ...]}。"""
    cfg = _load_config()["data"]
    url = cfg["pingzhongdata_url"].format(code=code)
    text = _http_get(url)
    name_m = re.search(r'var fS_name = "([^"]+)"', text)
    trend_m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", text)
    if not trend_m:
        raise ValueError(f"{code}: pingzhongdata 中未找到 Data_netWorthTrend")
    raw = json.loads(trend_m.group(1))
    navs = [(time.strftime("%Y-%m-%d", time.localtime(p["x"] / 1000)), p["y"]) for p in raw]
    return {"code": code, "name": name_m.group(1) if name_m else code, "navs": navs}


def fetch_lsjz(code: str) -> dict:
    """净值明细 + 申赎状态。返回 {records: [{date, nav, acc_nav, purchase, redeem}], latest_date}。"""
    cfg = _load_config()["data"]
    url = cfg["lsjz_url"].format(code=code)
    text = _http_get(url, referer="https://fundf10.eastmoney.com/")
    data = json.loads(text)
    rows = (data.get("Data") or {}).get("LSJZList") or []
    records = [{
        "date": r.get("FSRQ", ""),
        "nav": float(r["DWJZ"]) if r.get("DWJZ") else None,
        "acc_nav": float(r["LJJZ"]) if r.get("LJJZ") else None,
        "purchase": r.get("SGZT", ""),   # 申购状态：开放申购/暂停申购/限大额
        "redeem": r.get("SHZT", ""),     # 赎回状态：开放赎回/暂停赎回
    } for r in rows]
    return {"records": records, "latest_date": records[0]["date"] if records else ""}


def load_fund(code: str, force_refresh: bool = False) -> dict:
    """带缓存的全量净值加载。缓存未过期则直接复用。

    _source 标记数据来源（融合版增强，吸收 quant_test 母本降级告警链路）：
    - "fresh"          本次运行成功抓取官方接口
    - "cache"          缓存未过期命中（数据可能非最新，报告层透传提示）
    - "cache:fallback" 接口失败降级读缓存（母本 _source 口径，报告层显式告警）
    """
    cfg = _load_config()["data"]
    cache = _cache_path(code)
    if cache.exists():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if not force_refresh and _cache_fresh(cache, cfg["cache_ttl_hours"]):
                cached["_source"] = "cache"
                return cached
        except (json.JSONDecodeError, OSError):
            pass  # 缓存损坏则走网络
    try:
        fund = fetch_pingzhongdata(code)
        fund["_source"] = "fresh"
    except Exception as e:
        if cache.exists():
            try:
                cached = json.loads(cache.read_text(encoding="utf-8"))
                cached["_source"] = f"cache:fallback({e})"
                return cached
            except (json.JSONDecodeError, OSError):
                pass
        raise
    # 净值序列以 lsjz 最新一条为准确认口径（两者都是官方净值，仅校验日期齐不齐）
    try:
        lsjz = fetch_lsjz(code)
        fund["lsjz"] = lsjz
        fund["purchase_status"] = lsjz["records"][0]["purchase"] if lsjz["records"] else ""
        fund["redeem_status"] = lsjz["records"][0]["redeem"] if lsjz["records"] else ""
    except Exception as e:  # 申赎状态拿不到不阻断信号计算
        fund["lsjz"] = {"records": [], "latest_date": ""}
        fund["purchase_status"] = "未知"
        fund["redeem_status"] = "未知"
        fund["_lsjz_error"] = str(e)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(fund, ensure_ascii=False), encoding="utf-8")
    return fund
