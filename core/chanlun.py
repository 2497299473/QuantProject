"""缠论全量形态学（日线·段级别中枢）——按 chanlun skill 课62-78 顺序实现递归链：

K线包含处理 → 分型 → 笔 → 线段 → 中枢 → 走势类型 → 趋势背驰（一类买卖点）
+ 三类买卖点事件。

实现口径（诚实记录，与 skill 标准的差异均为已知简化）：
- 分型由第 3 根合并 K 确认；笔由端点分型确认且顶底间合并 K 间距 ≥4（「至少一根
  独立 K 线」的常用合并口径）
- 线段：≥3 笔且前三笔重叠成段；段终结用「反向前三笔重叠」近似特征序列分型，
  未实现课67「缺口第二种情况」
- 中枢：连续 3 段重叠区间 [ZD,ZG]；后续段触及区间则延伸；9 段升级仅标注不递归（课33）
- 走势类型：≥2 个同向且区间不重叠的中枢 = 趋势；1 个 = 盘整
- 趋势背驰（一买/一卖）：围绕最后中枢的进入段 vs 离开段，力度 = 段内 MACD 柱
  面积 ÷ 段长（课24），离开段须创趋势极值且力度 < ratio×进入段
- 三买/三卖：离开中枢的段回试不破 ZG / 不升破 ZD（第一次回试，课20）
- 二买/二卖：一买/一卖后首次次级别回探不破前低/前高（简化映射，课101）
- 无区间套、无多级别联立（仅日线单级别）

因果性：结构在完整序列上一次性计算，但每个事件携带 confirm_date（构成该事件
的结构被确认的 K 线日期），回测只在 confirm_date 之后消费事件（保守滞后）。
"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _macd_hist(closes: list[float]) -> list[float]:
    ema12 = ema26 = dea = closes[0]
    out = []
    for c in closes:
        ema12 += (c - ema12) * 2 / 13
        ema26 += (c - ema26) * 2 / 27
        dif = ema12 - ema26
        dea += (dif - dea) * 2 / 10
        out.append(2 * (dif - dea))
    return out


def merge_inclusion(klines: list) -> list[dict]:
    """包含处理（课62/65）：向上取高高点与较高低点，向下取低低点与较低高点。"""
    merged: list[dict] = []
    for date, _o, _c, high, low in klines:
        if not merged:
            merged.append({"date": date, "high": high, "low": low, "dir": 0})
            continue
        prev = merged[-1]
        contains = (high >= prev["high"] and low <= prev["low"]) or \
                   (high <= prev["high"] and low >= prev["low"])
        if contains:
            if prev["dir"] >= 0:
                prev["high"] = max(prev["high"], high)
                prev["low"] = max(prev["low"], low)
            else:
                prev["high"] = min(prev["high"], high)
                prev["low"] = min(prev["low"], low)
            prev["date"] = date          # 合并后取后根日期（确认口径）
        else:
            merged.append({"date": date, "high": high, "low": low,
                           "dir": 1 if high > prev["high"] else -1})
    return merged


def find_fractals(merged: list[dict]) -> list[dict]:
    """分型（课62/77）：合并后相邻三 K 中间极值；同类取更极值、顶底交替。"""
    raw = []
    for j in range(1, len(merged) - 1):
        h0, h1, h2 = merged[j - 1]["high"], merged[j]["high"], merged[j + 1]["high"]
        l0, l1, l2 = merged[j - 1]["low"], merged[j]["low"], merged[j + 1]["low"]
        if h1 > h0 and h1 > h2 and l1 > l0 and l1 > l2:
            raw.append({"type": "top", "idx": j, "price": h1,
                        "date": merged[j]["date"], "confirm_date": merged[j + 1]["date"]})
        elif l1 < l0 and l1 < l2 and h1 < h0 and h1 < h2:
            raw.append({"type": "bottom", "idx": j, "price": l1,
                        "date": merged[j]["date"], "confirm_date": merged[j + 1]["date"]})
    out: list[dict] = []
    for f in raw:   # 交替：同类取更极值
        if out and out[-1]["type"] == f["type"]:
            better = f["price"] > out[-1]["price"] if f["type"] == "top" else f["price"] < out[-1]["price"]
            if better:
                out[-1] = f
        else:
            out.append(f)
    return out


def build_strokes(fractals: list[dict]) -> list[dict]:
    """笔（课62/77）：顶底分型间 ≥1 根独立合并 K（间距≥4），顶须高于底。"""
    strokes: list[dict] = []
    last = None
    for f in fractals:
        if last is None:
            last = f
            continue
        if abs(f["idx"] - last["idx"]) < 4:
            continue
        if f["type"] == "top" and f["price"] <= last["price"]:
            continue                      # 顶不高于底，不成笔
        if f["type"] == "bottom" and f["price"] >= last["price"]:
            continue
        strokes.append({
            "dir": 1 if f["type"] == "top" else -1,
            "from": last, "to": f,
            "high": max(last["price"], f["price"]), "low": min(last["price"], f["price"]),
            "start_date": last["date"], "end_date": f["date"],
            "confirm_date": f["confirm_date"],
        })
        last = f
    return strokes


def build_segments(strokes: list[dict]) -> list[dict]:
    """线段（课65/77/78 简化）：≥3 笔且前三笔重叠；反向前三笔重叠终结本段。"""
    segs: list[dict] = []
    i = 0
    while i + 2 < len(strokes):
        d = strokes[i]["dir"]
        zg = min(strokes[k]["high"] for k in (i, i + 1, i + 2))
        zd = max(strokes[k]["low"] for k in (i, i + 1, i + 2))
        if zg <= zd:                      # 前三笔不重叠，滑窗
            i += 1
            continue
        end, confirm = len(strokes) - 1, strokes[-1]["confirm_date"]
        j = i + 3
        while j + 2 < len(strokes):
            rzg = min(strokes[k]["high"] for k in (j, j + 1, j + 2))
            rzd = max(strokes[k]["low"] for k in (j, j + 1, j + 2))
            if rzg > rzd and strokes[j]["dir"] == -d:
                end, confirm = j - 1, strokes[j + 2]["confirm_date"]
                break
            j += 1
        segs.append({
            "dir": d, "start_date": strokes[i]["start_date"], "end_date": strokes[end]["end_date"],
            "high": max(s["high"] for s in strokes[i:end + 1]),
            "low": min(s["low"] for s in strokes[i:end + 1]),
            "confirm_date": confirm, "stroke_span": (i, end),
        })
        i = end + 1
    return segs


def build_pivots(segs: list[dict]) -> list[dict]:
    """中枢（课17/20/33）：连续三段重叠 [ZD,ZG]；触及即延伸；段数≥9 标注升级。"""
    pivots: list[dict] = []
    i = 0
    while i + 2 < len(segs):
        zg = min(segs[k]["high"] for k in (i, i + 1, i + 2))
        zd = max(segs[k]["low"] for k in (i, i + 1, i + 2))
        if zg <= zd:
            i += 1
            continue
        k = i + 3
        while k < len(segs) and segs[k]["low"] <= zg and segs[k]["high"] >= zd:
            k += 1
        pivots.append({"ZG": zg, "ZD": zd, "seg_start": i, "seg_end": k - 1,
                       "n_segs": k - i, "upgrade": k - i >= 9,
                       "confirm_date": segs[i + 2]["confirm_date"]})
        i = k
    return pivots


def _seg_momentum(seg: dict, klines: list, date_idx: dict, hist: list[float]) -> float:
    a, b = date_idx.get(seg["start_date"], 0), date_idx.get(seg["end_date"], len(klines) - 1)
    span = klines[a:b + 1]
    return sum(abs(h) for h in hist[a:b + 1]) / max(1, len(span))   # 面积÷时间


def analyze(klines: list, div_ratio: float = 0.8) -> dict:
    """全量形态学分析。返回走势类型/中枢/事件列表。事件仅携带 confirm_date（因果消费）。"""
    if len(klines) < 60:
        return {"trend": None, "pivots": [], "events": []}
    merged = merge_inclusion(klines)
    fractals = find_fractals(merged)
    strokes = build_strokes(fractals)
    segs = build_segments(strokes)
    pivots = build_pivots(segs)
    closes = [k[2] for k in klines]
    hist = _macd_hist(closes)
    date_idx = {k[0]: i for i, k in enumerate(klines)}
    events: list[dict] = []

    trend = None
    if len(pivots) >= 2:
        p1, p2 = pivots[-2], pivots[-1]
        if p2["ZG"] > p1["ZG"] and p2["ZD"] > p1["ZD"]:
            trend = "up"
        elif p2["ZG"] < p1["ZG"] and p2["ZD"] < p1["ZD"]:
            trend = "down"
        else:
            trend = "expand"          # 中枢重叠 → 更大级别盘整/扩展
    elif len(pivots) == 1:
        trend = "consolidation"

    # 一类买卖点：趋势中围绕最后中枢的进入段 vs 离开段
    if trend in ("up", "down") and len(pivots) >= 2:
        p = pivots[-1]
        leave_idx, enter_idx = p["seg_end"] + 1, p["seg_start"] - 1
        if leave_idx < len(segs) and enter_idx >= 0:
            enter, leave = segs[enter_idx], segs[leave_idx]
            mom_e = _seg_momentum(enter, klines, date_idx, hist)
            mom_l = _seg_momentum(leave, klines, date_idx, hist)
            if leave["dir"] == (1 if trend == "up" else -1) and mom_l < div_ratio * mom_e:
                extreme_ok = (trend == "up" and leave["high"] >= enter["high"]) or \
                             (trend == "down" and leave["low"] <= enter["low"])
                if extreme_ok:
                    events.append({"type": "1sell" if trend == "up" else "1buy",
                                   "date": leave["end_date"], "confirm_date": leave["confirm_date"],
                                   "price": leave["high"] if trend == "up" else leave["low"]})
    # 三类买卖点：离开段回试不破 ZG / 不升破 ZD（第一次回试）
    for p in pivots[-2:]:
        li = p["seg_end"] + 1
        if li + 1 < len(segs):
            leave, back = segs[li], segs[li + 1]
            if leave["dir"] == 1 and back["low"] > p["ZG"]:
                events.append({"type": "3buy", "date": back["end_date"],
                               "confirm_date": back["confirm_date"], "price": back["low"]})
            elif leave["dir"] == -1 and back["high"] < p["ZD"]:
                events.append({"type": "3sell", "date": back["end_date"],
                               "confirm_date": back["confirm_date"], "price": back["high"]})
    # 二类买卖点：一类点后首次回探不破
    for e in [x for x in events if x["type"] in ("1buy", "1sell")]:
        after = [s for s in strokes if s["confirm_date"] > e["confirm_date"]]
        if e["type"] == "1buy":
            bottoms = [s for s in after if s["dir"] == -1 and s["low"] > e["price"]]
            if bottoms:
                b = bottoms[0]
                events.append({"type": "2buy", "date": b["end_date"],
                               "confirm_date": b["confirm_date"], "price": b["low"]})
        else:
            tops = [s for s in after if s["dir"] == 1 and s["high"] < e["price"]]
            if tops:
                t = tops[0]
                events.append({"type": "2sell", "date": t["end_date"],
                               "confirm_date": t["confirm_date"], "price": t["high"]})

    events.sort(key=lambda e: e["confirm_date"])
    return {"trend": trend, "pivots": pivots, "events": events,
            "n_merged": len(merged), "n_strokes": len(strokes), "n_segs": len(segs)}
