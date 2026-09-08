"""用户 80 个板块名 → 东财板块体系（行业 t:2 / 概念 t:3）实测映射。
产出：output/sector_name_mapping_20260902.md
"""
import time
from pathlib import Path

from core import netutil

NAMES = ["AI应用", "CPO", "白酒", "半导体材料设备", "存储芯片", "农业", "畜牧养殖",
         "煤炭", "PCB", "微盘股", "创新药", "MLCC", "半导体", "传媒", "黄金股",
         "电力", "韩国综合", "红利低波", "有色金属", "港股创新药", "银行",
         "金融科技", "黄金", "软件", "通信技术", "锂矿", "保险", "算力租赁",
         "机器人", "云计算", "5G", "商业航天", "食品饮料", "消费电子", "国产算力",
         "消费", "CXO", "红利", "脑机接口", "人工智能", "化工", "动漫游戏", "军工",
         "电网设备", "证券", "贵金属", "固态电池", "证券保险", "医药", "AI医疗",
         "光伏", "房地产", "可控核聚变", "中药", "低空经济", "锂电池", "港股医药",
         "稀土", "新能源", "信创", "体育", "电子", "储能", "芯片产业", "钢铁",
         "港股科技", "医疗", "汽车", "建材", "港股红利", "计算机", "智能家居",
         "交通运输", "机械设备", "环保", "港股消费", "家电", "基建", "国企改革",
         "新能源车"]


def fetch_boards(fs: str) -> dict:
    out, pn = {}, 1
    while pn <= 12:
        url = ("https://push2.eastmoney.com/api/qt/clist/get"
               f"?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
               f"&fs={fs}&fields=f12,f14")
        j = netutil.http_get_json(url, timeout=15, retries=2)
        diff = ((j.get("data") or {}).get("diff")) or []
        if not diff:
            break
        for d in diff:
            out[d["f14"]] = d["f12"]
        if len(diff) < 100:
            break
        pn += 1
        time.sleep(0.8)
    return out


ind = fetch_boards("m:90+t:2+f:!50")
time.sleep(1.5)
con = fetch_boards("m:90+t:3+f:!50")
allb = {**{k: (v, "行业") for k, v in ind.items()},
        **{k: (v, "概念") for k, v in con.items()}}
print(f"行业板块 {len(ind)} 个, 概念板块 {len(con)} 个, 共 {len(allb)}")


def bigrams(s: str) -> set:
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else {s}


exact, fuzzy, unmatched = [], [], []
for n in NAMES:
    hit = None
    if n in allb:
        hit = (n,)
    else:
        for suf in ("概念", "指数"):
            if n + suf in allb:
                hit = (n + suf,)
                break
    if hit:
        bn = hit[0]
        exact.append((n, allb[bn][0], bn, allb[bn][1]))
        continue
    # 方向1: 板块名 ⊂ 用户名（如 半导体 ⊂ 半导体材料设备）
    best = None
    for bn, (bc, bt) in allb.items():
        if bn in n:
            score = (len(bn), bt == "行业")
            if best is None or score > best[0]:
                best = (score, bn, bc, bt)
    # 方向2: 用户名 ⊂ 板块名（如 光伏 ⊂ 光伏设备），取最短
    if best is None:
        c2 = [(bn, bc, bt) for bn, (bc, bt) in allb.items() if n in bn]
        if c2:
            c2.sort(key=lambda x: (len(x[0]), x[2] != "行业"))
            best = ((-len(c2[0][0]),), c2[0][0], c2[0][1], c2[0][2])
    if best:
        fuzzy.append((n, best[2], best[1], best[3]))
        continue
    ng = bigrams(n)
    cands = [(bn, bc, bt) for bn, (bc, bt) in allb.items() if bigrams(bn) & ng]
    cands.sort(key=lambda x: -len(bigrams(x[0]) & ng))
    unmatched.append((n, cands[:3]))

lines = ["# 板块名映射表（用户 80 项 → 东财板块）2026-09-02", "",
         f"- 行业板块 {len(ind)} 个、概念板块 {len(con)} 个，共 {len(allb)}",
         f"- 精确/加后缀 {len(exact)}、包含式模糊 {len(fuzzy)}、未匹配 {len(unmatched)}",
         "", "## 精确匹配", "| 用户名 | BK 代码 | 板块名 | 类型 |", "|---|---|---|---|"]
for n, c, bn, bt in exact:
    lines.append(f"| {n} | {c} | {bn} | {bt} |")
lines += ["", "## 模糊匹配", "| 用户名 | BK 代码 | 板块名 | 类型 |", "|---|---|---|---|"]
for n, c, bn, bt in fuzzy:
    lines.append(f"| {n} | {c} | {bn} | {bt} |")
lines += ["", "## 未匹配（附最近候选）", "| 用户名 | 最近候选 |", "|---|---|"]
for n, cands in unmatched:
    cs = "、".join(f"{bn}({bc},{bt})" for bn, bc, bt in cands) or "无"
    lines.append(f"| {n} | {cs} |")

Path("output/sector_name_mapping_20260902.md").write_text(
    "\n".join(lines), encoding="utf-8")
print(f"\n精确 {len(exact)} / 模糊 {len(fuzzy)} / 未匹配 {len(unmatched)}")
print("已写 output/sector_name_mapping_20260902.md")
print("\n未匹配明细:")
for n, cands in unmatched:
    print(f"  {n}: " + ("、".join(f"{b}({c})" for b, c, _ in cands) or "无候选"))
