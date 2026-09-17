r"""V4 步 2 验收 · 真实逐根对拍（2026-09-17，Summer 显式授权发东财请求）。

背景：步 2 把 stock_data.py 三源链迁入 core/datasource/providers，离线层已钉
（provider 21 测 + fast 252 + 回读 247 缓存恒等）。本脚本关闭最后一项：
「与旧实现同参数对拍、K 线逐根一致」——旧实现 = f0668e5 的函数体（原样复制，
仅去掉缓存写盘），新实现 = providers 真实拉数，逐根比 (date, o, c, h, l) 恒等。

标的（2~3 只非科创板，用户口径）：600519 沪 / 000651 深 / 000858 深。
东财请求预算：每标的旧 1 发 + 新 1 发 = 共 6 发，单发不重试、任一失败即停手上报。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from core import netutil  # noqa: E402
from core.datasource.providers.stock_tencent import TencentKlineProvider  # noqa: E402
from core.datasource.providers.stock_eastmoney import EastmoneyKlineProvider  # noqa: E402

# ---------------- 旧实现（git show f0668e5:core/stock_data.py 原样，去写盘） ----------------

PAGE = 640
MAX_PAGES = 5
_MKT = {"0": "sz", "1": "sh"}          # 东财市场前缀 → 腾讯
_EM_SECID = {"0": "0", "1": "1"}


def _old_http_json(url: str, timeout: int = 12) -> dict:
    return netutil.http_get_json(url, timeout=timeout)


def _old_fetch_page(symbol: str, end_date: str) -> list[list[str]]:
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={symbol},day,,{end_date},{PAGE},qfq")
    data = _old_http_json(url).get("data") or {}
    node = data.get(symbol) or {}
    return node.get("qfqday") or node.get("day") or []


def _old_fetch_em(code: str, market: str, min_start: str = "2015-01-01") -> dict:
    beg = min_start.replace("-", "")
    url = ("http://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={_EM_SECID[market]}.{code}"
           "&klt=101&fqt=1"
           f"&beg={beg}&end=20500101"
           "&fields1=f1,f2,f3,f4,f5,f6"
           "&fields2=f51,f52,f53,f54,f55,f56")
    data = _old_http_json(url, timeout=15)
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


def old_tencent(code: str, market: str, min_start: str = "2015-01-01") -> dict:
    """旧实现腾讯主源（分页拼接），逐行对照 f0668e5 的 fetch_stock_kline 主源段。"""
    symbol = f"{_MKT[market]}{code}"
    pages = []
    end_date = "2050-01-01"
    for _ in range(MAX_PAGES):
        try:
            page = _old_fetch_page(symbol, end_date)
        except Exception:  # noqa: BLE001 —— 旧实现即在此吞异常出空页
            page = []
        if not page:
            break
        pages.append(page)
        oldest = page[0][0]
        if len(page) < PAGE or oldest <= min_start:
            break
        end_date = oldest
        time.sleep(0.12)
    if not pages:
        raise ValueError(f"{symbol}: 旧腾讯源无数据")
    rows: dict[str, tuple] = {}
    for page in pages:
        for r in page:
            rows[r[0]] = (r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]))
    klines = [rows[d] for d in sorted(rows)]
    return {"code": code, "market": market, "klines": klines, "source": "tencent"}


# ---------------- 比对 ----------------

def compare(tag: str, a: list, b: list) -> bool:
    if len(a) != len(b):
        print(f"  [{tag}] ✗ 根数不等 old={len(a)} new={len(b)}")
        return False
    bad = 0
    for i, (ra, rb) in enumerate(zip(a, b)):
        if tuple(ra) != tuple(rb):
            if bad < 5:
                print(f"  [{tag}] ✗ L{i} old={ra} new={rb}")
            bad += 1
    if bad:
        print(f"  [{tag}] ✗ 同根数({len(a)})但 {bad} 根不一致")
        return False
    print(f"  [{tag}] ✓ {len(a)} 根逐根恒等（首 {a[0][0]} 末 {a[-1][0]}）")
    return True


def main() -> int:
    codes = [("600519", "1"), ("000651", "0"), ("000858", "0")]
    new_tx, new_em = TencentKlineProvider(), EastmoneyKlineProvider()
    all_ok = True
    for code, market in codes:
        print(f"== {code} (market {market}) ==")
        # 1) 腾讯：旧实现 vs 新 provider（各真实拉一轮）
        try:
            old = old_tencent(code, market)
        except Exception as exc:  # noqa: BLE001
            print(f"  [tencent] ✗ 旧实现拉取失败: {type(exc).__name__}: {exc}")
            return 2
        res = new_tx.fetch(code=code, market=market)
        if not res.ok:
            print(f"  [tencent] ✗ 新 provider 失败: {res.error}")
            return 2
        ok_tx = compare("tencent", old["klines"], res.payload["klines"])
        # 2) 东财：旧函数 vs 新 provider（各真实拉一轮；本脚本仅此 3×2 发打东财）
        try:
            old_em = _old_fetch_em(code, market)
        except Exception as exc:  # noqa: BLE001
            print(f"  [eastmoney] ✗ 旧实现拉取失败: {type(exc).__name__}: {exc}")
            return 2
        res_em = new_em.fetch(code=code, market=market)
        if not res_em.ok:
            print(f"  [eastmoney] ✗ 新 provider 失败: {res_em.error}")
            return 2
        ok_em = compare("eastmoney", old_em["klines"], res_em.payload["klines"])
        all_ok = all_ok and ok_tx and ok_em
        time.sleep(1.0)  # 标的间隔，温和节流

    print("\n== 结论 ==", "PASS：新旧实现真实拉数逐根恒等" if all_ok else "FAIL：见上")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
