"""数据层：东方财富主源 + 新浪备源（基金净值），provider 链装配点（V4 步 4，2026-09-17）。

设计契约（Obsidian《V4数据源层重构方案-SourceRegistry-20260917》硬约束 1/2/4）：
- 对外签名 `load_fund(code, force_refresh)`、`fetch_pingzhongdata(code)`、
  `fetch_lsjz(code)` **一字不改**；`_source` 三态 `fresh`/`cache`/`cache:fallback`
  语义不动（report_generator 按 `cache:fallback` 前缀出告警）。
- 净值抓取从「东财单源 + 失败读缓存」升级为「SourceRegistry 链：东财 → 新浪」；
  **全链失败才降级读缓存**（cache:fallback 语义保持，只是触发条件更宽：任一源失败
  不再直接 fallback，而是先换源）。链**严格串行**（硬约束 4），无自动并发。
- `fresh` 结果新增**附加键** `source`（'eastmoney'|'sina'，实际取数源，供步 5 展示层）；
  不改 `_source` 三态取值域。缓存文件因此多一个键——仅新增维度，消费方按具名键读取。
- `fetch_lsjz`（申赎状态，东财 f10 独有）不迁移：新浪无对应接口，失败仍不阻断
  （`_lsjz_error` 留痕，口径同前）。
- 已知失效接口（不要使用）：fundgz 实时估值（404）、fundmobapi（网络繁忙）。
本地缓存 data/klines/{code}.json，TTL 内复用，避免同一运行日重复抓取。
"""
import json
import time
from pathlib import Path

from . import netutil
from .datasource import SourceRegistry, run_chain
from .datasource.providers.fund_eastmoney import EastmoneyFundNavProvider
from .datasource.providers.fund_sina import SinaFundNavProvider

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
BASE_DIR = Path(__file__).resolve().parent.parent
CATEGORY = "fund_nav"

NAV_FALLBACK_PREFIX = "cache:fallback"
"""全链失败降级读缓存的 `_source` 前缀（实际值形如 `cache:fallback(<异常摘要>)`）。"""


FUND_STATUS_UNKNOWN = frozenset({"", "未知", "nan", "none"})
"""``purchase_status`` / ``redeem_status`` 视为「不可得」的取值（小写比较，容错空值）。"""


def is_fund_status_known(fund: dict) -> bool:
    """申购/赎回状态是否可得（V4.2，2026-09-18：动作层前置门禁）。

    ``lsjz`` 取数失败时 ``load_fund()`` 仍**正常返回**（对外签名不动，状态落「未知」并留
    ``_lsjz_error``）。这件事必须能被下游读到：是否可申购/可赎回是动作能否执行的硬前提，
    状态未知时绝不能输出加/减仓。与 ``is_nav_fallback`` 同属「三态之外的可得性谓词」，
    独立成函数作单一事实源，避免 run / 决策 / 报告各写一套判定。
    """
    f = fund or {}
    return all(str(f.get(k) or "").strip().lower() not in FUND_STATUS_UNKNOWN
               for k in ("purchase_status", "redeem_status"))


def is_nav_fallback(fund: dict) -> bool:
    """该基金本次是否走了「全链失败 → 退回旧缓存」降级路径（V4.1 ③）。

    单一事实源：run.py（运行状态机 / degraded / publish_gate）、report_generator
    （读者可见告警）都用本谓词，避免两处 `startswith` 判定漂移。

    语义要点：`cache:fallback` 时 `load_fund()` **正常返回、不抛异常**，所以调用方
    只 catch 异常是抓不到它的——这正是 2026-09-18 审查指出的漏网：全链失败会
    exit=0 且发布门禁看不见数据污染。
    """
    return str((fund or {}).get("_source", "")).startswith(NAV_FALLBACK_PREFIX)


_registry_singleton: SourceRegistry | None = None


def _registry() -> SourceRegistry:
    """装配点：登记净值两类源。进程内单例，保证健康度跨调用累积。"""
    global _registry_singleton
    if _registry_singleton is None:
        reg = SourceRegistry()
        for cls in (EastmoneyFundNavProvider, SinaFundNavProvider):
            reg.register(cls())
        _registry_singleton = reg
    return _registry_singleton


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
    """东财 pingzhongdata 单源抓取（公开面保留，内部走 provider）。

    返回 {name, navs: [(date, nav), ...]}；失败抛 ValueError（旧契约），
    错误文本带 `data:`/`network:` 失败前缀（比旧版多前缀，语义不变）。
    """
    cfg = _load_config()["data"]
    res = EastmoneyFundNavProvider().fetch(code=code,
                                           pz_url=cfg["pingzhongdata_url"])
    if not res.ok:
        raise ValueError(res.error)
    out = dict(res.payload)
    out.pop("source", None)
    out["navs"] = [tuple(r) for r in out["navs"]]
    return out


def fetch_lsjz(code: str) -> dict:
    """净值明细 + 申赎状态（东财 f10 独有，不入多源链）。返回 {records: [{date, nav, acc_nav, purchase, redeem}], latest_date}。"""
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


def _read_cache(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _fetch_fund_nav(code: str, pz_url: str, cached_name: str | None) -> dict:
    """真取数：按链（东财 → 新浪）串行换源。全灭抛 ValueError 逐源列原因。

    行形状统一为 tuple `(date, nav)`（与旧 fresh 一致；新浪 provider 返 list，
    这里归一），name 缺失（sina 源无名称字段）时回退缓存旧值或代码。
    """
    reg = _registry()
    result = run_chain(reg.chain_for(CATEGORY), health=reg.health,
                       code=code, pz_url=pz_url)
    if not result.ok:
        detail = " | ".join(f"{a.source}:{a.error}" for a in result.attempts) \
            or "无可用源（链为空）"
        raise ValueError(f"{code}: 全部净值源失败（{detail}）")
    fund = dict(result.payload)
    fund["navs"] = [tuple(r) for r in fund.get("navs") or []]
    if not fund.get("name"):
        fund["name"] = cached_name or code
    return fund


def load_fund(code: str, force_refresh: bool = False) -> dict:
    """带缓存的净值加载（多源链版）。缓存未过期则直接复用。

    _source 标记数据来源（三态口径一字不动）：
    - "fresh"          本次运行链上任一源成功（附加键 source 标实际源名）
    - "cache"          缓存未过期命中（数据可能非最新，报告层透传提示）
    - "cache:fallback" **全链失败**降级读缓存（报告层显式告警）
    """
    cfg = _load_config()["data"]
    cache = _cache_path(code)
    cached = None
    if cache.exists():
        cached = _read_cache(cache)   # 损坏缓存 → None，走网络（口径同前）
        if cached is not None and not force_refresh \
                and _cache_fresh(cache, cfg["cache_ttl_hours"]):
            cached["_source"] = "cache"
            # 步 5 source trace：缓存文件里的 source 是「上次取数时」的源，对本次
            # 加载已失真（本次实际来自 cache）→ 剥除，防止 stale 源名混进展示层。
            cached.pop("source", None)
            return cached
    try:
        fund = _fetch_fund_nav(code, cfg["pingzhongdata_url"],
                               (cached or {}).get("name"))
        fund["_source"] = "fresh"
    except Exception as e:
        fallback = cached if cached is not None \
            else (_read_cache(cache) if cache.exists() else None)
        if fallback is not None:
            fallback["_source"] = f"cache:fallback({e})"
            fallback.pop("source", None)   # 同上：降级态的源名失真，剥除
            return fallback
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
