"""板块数据源复测（2026-09-02）——替代 v5 笔记（2026-08-26）「申万源不可用」阻断结论。

全部走 core.netutil（IPv4 优先 + 绕过环境死代理 + 瞬断重试），本身即验证修复链路。
A: f127 个股→行业映射（申万行业名）
B: 东财行业板块（90.BKxxx）板块列表 + K 线深度（2015 起）
C: 代表性 ETF 走项目 fetch_stock_kline（腾讯主源/东财备源），强制实时拉取
产物: output/probe_sector_sources_20260902.md（tee）+ data/sector_bk_map.json
"""
import json
import time
from pathlib import Path

from core import netutil
from core.stock_data import fetch_stock_kline

BASE = Path(__file__).resolve().parent

# ---------------------------------------------------------------- A: 行业映射
STOCKS = [
    ("0.002112", "002112"), ("0.300502", "300502"), ("1.601398", "601398"),
    ("1.600519", "600519"), ("0.000001", "000001"), ("0.300750", "300750"),
    ("1.600030", "600030"), ("1.601088", "601088"), ("0.300760", "300760"),
    ("1.601899", "601899"),
]
mapping: dict[str, dict] = {}
print("# 板块数据源复测 2026-09-02（netutil 修复后）")
print("\n## A: 个股 → 行业（f127 申万行业名 / f128 地域板块，禁用于行业）")
print("| 代码 | 名称 | f127 行业 | f128(地域,勿用) |")
print("|---|---|---|---|")
for secid, code in STOCKS:
    try:
        j = netutil.http_get_json(
            f"https://push2.eastmoney.com/api/qt/stock/get"
            f"?secid={secid}&fields=f57,f58,f127,f128&invt=2&fltt=2")
        d = j.get("data") or {}
        ind = d.get("f127")
        region = d.get("f128")
        mapping[code] = {"name": d.get("f58"), "industry": ind,
                         "region_field_f128": region}
        print(f"| {code} | {d.get('f58', '')} | {ind} | {region} |")
    except Exception as e:
        print(f"| {code} | FAIL {type(e).__name__}:{str(e)[:60]} | - | - |")
    time.sleep(0.3)

# ---------------------------------------------------------------- B: 行业板块
boards: list[tuple[str, str]] = []
print("\n## B1: 东财行业板块列表（fs=m:90 t:2，注意：东财板块，非申万官方目录）")
try:
    j = netutil.http_get_json(
        "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=200&po=1&np=1"
        "&fltt=2&invt=2&fid=f3&fs=m:90+t:2+f:!50&fields=f12,f14")
    diff = ((j.get("data") or {}).get("diff")) or []
    for it in diff:
        boards.append((str(it.get("f12")), str(it.get("f14"))))
    print(f"板块总数: {len(boards)}")
except Exception as e:
    print(f"clist FAIL: {type(e).__name__}: {str(e)[:80]}")

(BASE / "data").mkdir(exist_ok=True)
(BASE / "data" / "sector_bk_map.json").write_text(
    json.dumps({"stocks": mapping, "boards": {c: n for c, n in boards}},
               ensure_ascii=False, indent=1),
    encoding="utf-8")

PICK_NAMES = {"证券", "半导体", "银行", "白酒", "医药商业", "电池", "煤炭行业",
              "有色金属", "房地产开发", "生物制品", "电力", "汽车整车"}
print("\n## B2: 行业板块 K 线深度（beg=20150101，验证 OOS 覆盖）")
print("| 板块码 | 板块名 | K线根数 | 首根 | 末根 |")
print("|---|---|---:|---|---|")
pick = [b for b in boards if b[1] in PICK_NAMES][:10] or boards[:10]
for code, name in pick:
    try:
        j = netutil.http_get_json(
            f"https://push2his.eastmoney.com/api/qt/stock/kline/get"
            f"?secid=90.{code}&klt=101&fqt=1&beg=20150101&end=20500101"
            f"&fields1=f1&fields2=f51,f53", timeout=15)
        d = j.get("data") or {}
        kl = d.get("klines") or []
        first = kl[0].split(",")[0] if kl else "-"
        lastd = kl[-1].split(",")[0] if kl else "-"
        print(f"| {code} | {d.get('name', name)} | {len(kl)} | {first} | {lastd} |")
    except Exception as e:
        print(f"| {code} | {name} | FAIL {type(e).__name__} | - | - |")
    time.sleep(0.5)

# ---------------------------------------------------------------- C: ETF 走项目主链路
ETFS = [
    ("512480", "1", "半导体ETF"), ("510300", "1", "沪深300ETF"),
    ("159915", "0", "创业板ETF"), ("512880", "1", "证券ETF"),
    ("515080", "1", "红利ETF"),
]
print("\n## C: ETF K 线（项目 fetch_stock_kline 主链路，ttl=0 强制实时拉取）")
print("| 代码 | 名称 | 实际源 | K线根数 | 首根 | 末根 |")
print("|---|---|---|---:|---|---|")
for code, mkt, nm in ETFS:
    try:
        d = fetch_stock_kline(code, mkt, ttl_hours=0)
        kl = d["klines"]
        print(f"| {code} | {nm} | {d['source']} | {len(kl)} | "
              f"{kl[0][0]} | {kl[-1][0]} |")
    except Exception as e:
        print(f"| {code} | {nm} | FAIL {type(e).__name__}:{str(e)[:60]} | - | - | - |")
    time.sleep(0.3)

print("\n== done ==")
