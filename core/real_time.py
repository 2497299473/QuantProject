"""重仓股实时行情：腾讯 qt.gtimg.cn（盘中实时 / 收盘定格）。

用途：推送时点（午盘 11:30 / 收盘前 14:55）展示持仓股票实时涨跌，
并以前十大披露权重加权得到「底层持仓估算涨跌」（lookthrough_estimated_change）。

诚实边界（2026-08-25 定，措辞对齐 GPT-5.6 诊断第四节）：
- 场外基金当日净值约 20:00 后才公布 → 基金涨跌无法直接获取
- 估算 = 最新季报前十大 × 实时行情加权，是「滞后持仓的实时市场冲击估计」，
  不是基金经理今日真实持仓 → 报告/推送措辞统一用「当日估算」，避免与正式净值混淆
- 腾讯字段：~v_sh600519~名称~代码~现价~昨收~今开~...~时间~涨跌额~涨跌幅~
"""
from . import netutil

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_MKT = {"0": "sz", "1": "sh"}  # 东财市场前缀 → 腾讯


def fetch_realtime(holdings: list[dict]) -> dict:
    """holdings: [{market, code, name, pct}, ...] → {code: {name, price, change_pct, time, pct}}。

    失败返回空 dict（调用方降级：报告/卡片不展示实时栏）。
    """
    out: dict[str, dict] = {}
    if not holdings:
        return out
    syms = ",".join(f"{_MKT[h['market']]}{h['code']}" for h in holdings)
    url = f"https://qt.gtimg.cn/q={syms}"
    try:
        # 2026-09-02: 走 netutil（IPv4 优先 + 无视环境死代理 + 瞬断重试）。
        text = netutil.http_get_bytes(url, headers={"User-Agent": UA},
                                      timeout=10).decode("gbk", errors="replace")
    except Exception:
        return out
    # 腾讯原始格式：v_sz300308="51~中际旭创~300308~846.00~...~时间~涨跌额~涨跌幅~...
    # parts[0] 含前缀（v_sz300308="51 / \nv_sh688167="1），parts[2] 才是纯 6 位代码
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
            out[code] = {"name": parts[1], "price": price,
                         "change_pct": chg, "time": parts[30]}
        except (ValueError, IndexError):
            continue
    for h in holdings:
        q = out.get(h["code"])
        if q:
            q["pct"] = h["pct"]
            q["market"] = h["market"]
    return out


def weighted_estimate(holdings: list[dict], quotes: dict) -> dict:
    """按披露权重加权 → 基金估算涨跌。返回 {est_change_pct, covered_pct}；无覆盖返回 None。"""
    wsum = wchg = 0.0
    for h in holdings:
        q = quotes.get(h["code"])
        if q:
            wsum += h["pct"]
            wchg += h["pct"] * q["change_pct"]
    if wsum <= 0:
        return {"est_change_pct": None, "covered_pct": 0.0}
    return {"est_change_pct": wchg / wsum, "covered_pct": wsum}
