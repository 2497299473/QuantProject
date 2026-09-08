#!/usr/bin/env python3
"""T1 候选池探测（2026-09-02）：验证候选板块 K 线历史深度能否覆盖 OOS（2020-01-01 起）。

候选来源：output/sector_name_mapping_20260902.md
- 精确匹配中的 微盘股 + 概念类（用户重点：微盘、概念类）
- 未匹配项中已有明确候选的（人工定标）
走 core.netutil（IPv4 优先 + 绕死代理 + 瞬断重试），每码独立 try + 限速。
产物: output/probe_t1_pool_20260902.md（tee）
"""
import json
import sys
import time
from pathlib import Path

from core import netutil

BASE = Path(__file__).resolve().parent
OUT = BASE / "output" / "probe_t1_pool_20260902.md"

# (BK 代码, 板块名, 类型, 来源)
CANDIDATES = [
    # ---- 精确匹配 · 微盘 + 概念类（用户重点） ----
    ("BK1158", "微盘股", "概念", "exact"),
    ("BK1629", "AI应用", "概念", "exact"),
    ("BK1128", "CPO概念", "概念", "exact"),
    ("BK1137", "存储芯片", "概念", "exact"),
    ("BK1166", "低空经济", "概念", "exact"),
    ("BK0968", "固态电池", "概念", "exact"),
    ("BK1163", "可控核聚变", "概念", "exact"),
    ("BK0706", "脑机接口", "概念", "exact"),
    ("BK0963", "商业航天", "概念", "exact"),
    ("BK0714", "5G概念", "概念", "exact"),
    ("BK0579", "云计算", "概念", "exact"),
    ("BK0800", "人工智能", "概念", "exact"),
    ("BK1104", "信创", "概念", "exact"),
    ("BK0989", "储能概念", "概念", "exact"),
    ("BK0900", "新能源车", "概念", "exact"),
    ("BK0680", "智能家居", "概念", "exact"),
    ("BK0588", "光伏概念", "概念", "exact"),
    ("BK1173", "锂矿概念", "概念", "exact"),
    ("BK0615", "中药概念", "概念", "exact"),
    ("BK0490", "军工", "概念", "exact"),
    # ---- 未匹配/模糊项 · 已人工定标的明确候选 ----
    ("BK1259", "养殖业(畜牧养殖)", "行业", "manual"),
    ("BK1170", "AI制药(AI医疗)", "概念", "manual"),
    ("BK1134", "算力概念(国产算力)", "概念", "manual"),
    ("BK1641", "红利股(红利低波)", "概念", "manual"),
    ("BK1301", "游戏Ⅲ(动漫游戏)", "行业", "manual"),
    ("BK1330", "模拟芯片设计(芯片产业)", "行业", "manual"),
]

OOS_START = "2020-01-01"


def main() -> None:
    (BASE / "output").mkdir(exist_ok=True)
    rows: list[str] = []
    rows.append("# T1 候选池探测 2026-09-02（OOS 覆盖 2020-01-01 起）")
    rows.append("")
    rows.append("| BK 代码 | 板块名 | 类型 | 来源 | K线根数 | 首根 | 末根 | OOS覆盖 |")
    rows.append("|---|---|---|---|---:|---|---|---|")

    for code, name, typ, src in CANDIDATES:
        url = (
            "http://push2his.eastmoney.com/api/qt/stock/kline/get"
            f"?secid=90.{code}&klt=101&fqt=1&beg=20150101&end=20500101"
            "&fields1=f1&fields2=f51,f53"
        )
        try:
            j = netutil.http_get_json(url, timeout=15)
            d = j.get("data") or {}
            kl = d.get("klines") or []
            first = kl[0].split(",")[0] if kl else "-"
            lastd = kl[-1].split(",")[0] if kl else "-"
            covers = "是" if first and first <= OOS_START else "否"
            rows.append(f"| {code} | {name} | {typ} | {src} | {len(kl)} | "
                        f"{first} | {lastd} | {covers} |")
            print(f"[ok] {code} {name}: {len(kl)} bars {first}~{lastd} covers={covers}")
        except Exception as e:
            rows.append(f"| {code} | {name} | {typ} | {src} | "
                        f"FAIL {type(e).__name__}:{str(e)[:50]} | - | - | - |")
            print(f"[FAIL] {code} {name}: {type(e).__name__} {str(e)[:80]}")
        time.sleep(0.5)

    rows.append("")
    rows.append("== done ==")
    OUT.write_text("\n".join(rows), encoding="utf-8")
    print(f"\n写入 {OUT}")


if __name__ == "__main__":
    main()
