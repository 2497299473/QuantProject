#!/usr/bin/env python3
"""缠论最小实现 POC — 形态学验证（可行性实验，非正式接入）

规则来源: chanlun skill ch02(分型/笔/线段) + ch03(中枢/买卖点) + cheatsheet量化映射节
数据源: 东方财富 push2his 免费日K（前复权），与主项目同域

实现层级: K线包含处理 → 分型 → 笔 → 线段(简化) → 中枢 → 三类买卖点(简化)

严格说明（v0 近似）:
  - 线段用简化规则（三笔重叠+延伸），非完整特征序列分型（课67/71/78 需细化）
  - 背驰已升级为 MACD 柱面积比较（课24/37/56/64），a+A+b+B+c + c创新高/低必要条件；无MACD数据时退化幅度×0.8
  - 最小中枢用笔构建，缠论严格定义应由线段构成（课83）
  结论仅用于验证「重仓股代理K线能否跑通缠论结构」，不构成投资建议。
"""

import json
import requests
from typing import List, Optional, Tuple

HEADERS = {
    "Referer": "https://finance.eastmoney.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


# ==================== 数据获取 ====================

def fetch_kline(code: str, datalen: int = 250) -> List[dict]:
    """腾讯日K（前复权），WSL 网络环境可用。code: 6位代码，如 300308（深市sz/沪市sh 自动判断）
    返回: [{date, open, close, high, low, volume}, ...] 升序
    注: 东财 push2his 在 WSL 下 TLS 握手异常（2026-08-23 实测 HTTP 000），改用腾讯 gtimg
    """
    market = "sh" if code.startswith("6") else "sz"
    symbol = f"{market}{code}"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{datalen},qfq"
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json().get("data", {}).get(symbol, {})
    klines = data.get("qfqday") or data.get("day") or []
    result = []
    for row in klines[-datalen:]:
        # 除权日行末尾可能带 dict 分红信息，volume 始终是 row[5]
        result.append({
            "date": row[0],
            "open": float(row[1]),
            "close": float(row[2]),
            "high": float(row[3]),
            "low": float(row[4]),
            "volume": float(row[5]) if len(row) > 5 and isinstance(row[5], (str, int, float)) else 0.0,
        })
    return result


# ==================== 1. K线包含处理（课62/65） ====================

class MergedK:
    """合并后的K线"""
    def __init__(self, date, high, low, open_, close, direction=0):
        self.date = date
        self.high = high
        self.low = low
        self.open = open_
        self.close = close
        self.direction = direction  # 1=向上 -1=向下 0=初始
        self.raw_dates = [date]


def merge_klines(klines: List[dict]) -> List[MergedK]:
    """包含关系合并：向上取高高点+较高低点，向下取低低点+较低高点；无传递性按顺序两两合并"""
    if not klines:
        return []
    merged = []
    for i, k in enumerate(klines):
        mk = MergedK(k["date"], k["high"], k["low"], k["open"], k["close"])
        if i == 0:
            merged.append(mk)
            continue
        prev = merged[-1]
        has_include = (mk.high <= prev.high and mk.low >= prev.low) or \
                      (prev.high <= mk.high and prev.low >= mk.low)
        if has_include:
            direction = prev.direction
            if direction == 0:
                direction = 1 if mk.close >= prev.close else -1
            if direction == 1:
                prev.high = max(prev.high, mk.high)
                prev.low = max(prev.low, mk.low)
            else:
                prev.low = min(prev.low, mk.low)
                prev.high = min(prev.high, mk.high)
            prev.raw_dates.append(mk.date)
        else:
            mk.direction = 1 if mk.high > prev.high else -1
            merged.append(mk)
    return merged


# ==================== 2. 分型（课62/77） ====================

class Fractal:
    def __init__(self, index, date, ftype, value):
        self.index = index      # 在 merged 中的位置
        self.date = date
        self.type = ftype       # "top" / "bottom"
        self.value = value      # 顶=最高点 底=最低点


def find_fractals(merged: List[MergedK]) -> List[Fractal]:
    """顶/底分型：合并后相邻三K，中间高点最高且低点最高=顶；对称=底"""
    fracs = []
    for i in range(1, len(merged) - 1):
        a, b, c = merged[i - 1], merged[i], merged[i + 1]
        if b.high > a.high and b.high > c.high and b.low > a.low and b.low > c.low:
            fracs.append(Fractal(i, b.date, "top", b.high))
        elif b.low < a.low and b.low < c.low and b.high < a.high and b.high < c.high:
            fracs.append(Fractal(i, b.date, "bottom", b.low))
    return fracs


# ==================== 3. 笔（课62/65/77） ====================

class Stroke:
    def __init__(self, start, end, direction):
        self.start = start
        self.end = end
        self.direction = direction  # "up"(底→顶) / "down"(顶→底)
        self.start_date = start.date
        self.end_date = end.date
        self.start_value = start.value
        self.end_value = end.value


def build_strokes(fractals: List[Fractal]) -> List[Stroke]:
    """划笔三步法：①同类取更极值 ②顶底交替 ③间隔≥2根合并K线(至少1根独立K) ④顶必高于底"""
    if len(fractals) < 2:
        return []
    cleaned = []
    for f in fractals:
        if cleaned and cleaned[-1].type == f.type:
            if (f.type == "top" and f.value > cleaned[-1].value) or \
               (f.type == "bottom" and f.value < cleaned[-1].value):
                cleaned[-1] = f
        else:
            cleaned.append(f)
    strokes = []
    i = 0
    while i < len(cleaned) - 1:
        f1, f2 = cleaned[i], cleaned[i + 1]
        if f1.type == f2.type:
            i += 1
            continue
        if abs(f2.index - f1.index) < 2:
            i += 1
            continue
        top = f1 if f1.type == "top" else f2
        bottom = f2 if f1.type == "top" else f1
        if top.value <= bottom.value:
            i += 1
            continue
        strokes.append(Stroke(f1, f2, "up" if f1.type == "bottom" else "down"))
        i += 1
    return strokes


# ==================== 4. 线段·简化版（课65/67/77/78） ====================

class Segment:
    def __init__(self, start_idx, end_idx, direction, start_date, end_date, high, low):
        self.start_idx = start_idx
        self.end_idx = end_idx
        self.direction = direction
        self.start_date = start_date
        self.end_date = end_date
        self.high = high
        self.low = low


def build_segments(strokes: List[Stroke]) -> List[Segment]:
    """线段（简化）：≥3笔、前三笔重叠、向上段以向上笔开始结束（奇数笔）、延伸同向笔。
    注：非完整特征序列分型判定（课67），仅作结构验证。"""
    if len(strokes) < 3:
        return []
    segs = []
    i = 0
    while i < len(strokes) - 2:
        s1, s2, s3 = strokes[i], strokes[i + 1], strokes[i + 2]
        if not (s1.direction == s3.direction):
            i += 1
            continue
        direction = s1.direction
        s1_hi, s1_lo = max(s1.start_value, s1.end_value), min(s1.start_value, s1.end_value)
        s3_hi, s3_lo = max(s3.start_value, s3.end_value), min(s3.start_value, s3.end_value)
        overlap_lo, overlap_hi = max(s1_lo, s3_lo), min(s1_hi, s3_hi)
        if overlap_hi <= overlap_lo:
            i += 1
            continue
        end_idx = i + 2
        high, low = max(s1.end_value, s3.end_value), min(s1.start_value, s3.start_value)
        if direction == "up":
            while end_idx + 2 < len(strokes) and strokes[end_idx + 2].direction == "up":
                if strokes[end_idx + 2].end_value > high:
                    high = strokes[end_idx + 2].end_value
                    end_idx += 2
                else:
                    break
        else:
            while end_idx + 2 < len(strokes) and strokes[end_idx + 2].direction == "down":
                if strokes[end_idx + 2].end_value < low:
                    low = strokes[end_idx + 2].end_value
                    end_idx += 2
                else:
                    break
        segs.append(Segment(i, end_idx, direction, strokes[i].start_date,
                            strokes[end_idx].end_date, high, low))
        i = end_idx + 1
    return segs


# ==================== 5. 中枢（课17/18/20） ====================

class Zhongshu:
    def __init__(self, ZG, ZD, start_date, end_date, entry_index):
        self.ZG = ZG
        self.ZD = ZD
        self.ZZZ = (ZG + ZD) / 2
        self.start_date = start_date
        self.end_date = end_date
        self.entry_index = entry_index


def build_zhongshu(strokes: List[Stroke]) -> List[Zhongshu]:
    """笔中枢：连续3笔重叠 [ZD=max(3低), ZG=min(3高)]（课17/18；严格应用线段构建，课83）"""
    if len(strokes) < 3:
        return []
    zs_list = []
    i = 0
    while i < len(strokes) - 2:
        trio = strokes[i:i + 3]
        highs = [max(s.start_value, s.end_value) for s in trio]
        lows = [min(s.start_value, s.end_value) for s in trio]
        ZG, ZD = min(highs), max(lows)
        if ZG > ZD:
            zs_list.append(Zhongshu(ZG, ZD, trio[0].start_date, trio[2].end_date, i))
            i += 3
        else:
            i += 1
    return zs_list


# ==================== 6. MACD 背驰辅助（课24/37/56/64） ====================

def calc_macd(klines: List[dict], fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD 指标：返回 (dif, dea, hist)，hist 为柱值 2×(dif-dea)，长度与 klines 一致。"""
    closes = [k["close"] for k in klines]
    n = len(closes)
    if n == 0:
        return [], [], []
    kf, ks, kg = 2.0 / (fast + 1), 2.0 / (slow + 1), 2.0 / (signal + 1)
    ema_fast, ema_slow, dif, dea, hist = [0.0] * n, [0.0] * n, [0.0] * n, [0.0] * n, [0.0] * n
    ema_fast[0] = ema_slow[0] = closes[0]
    for i in range(1, n):
        ema_fast[i] = closes[i] * kf + ema_fast[i - 1] * (1 - kf)
        ema_slow[i] = closes[i] * ks + ema_slow[i - 1] * (1 - ks)
    for i in range(n):
        dif[i] = ema_fast[i] - ema_slow[i]
    dea[0] = dif[0]
    for i in range(1, n):
        dea[i] = dif[i] * kg + dea[i - 1] * (1 - kg)
    for i in range(n):
        hist[i] = 2.0 * (dif[i] - dea[i])
    return dif, dea, hist


def _macd_area(klines: List[dict], hist: List[float], start_date: str, end_date: str) -> float:
    """区间 [start_date, end_date] 的 MACD 柱面积（|柱| 求和），用于背驰比较（课24）。"""
    return sum(abs(h) for k, h in zip(klines, hist) if start_date <= k["date"] <= end_date)


def _append_2buy(points, strokes, c_idx):
    """一买后第一次次级别回调不破一买位 → 二买（课53）"""
    if c_idx + 2 < len(strokes):
        pb = strokes[c_idx + 2]
        if pb.direction == "down" and pb.end_value > strokes[c_idx].end_value:
            points.append(BuySellPoint(pb.end_date, "2买", pb.end_value,
                f"一买后回抽不破({strokes[c_idx].end_value:.2f})"))


def _append_2sell(points, strokes, c_idx):
    """一卖后第一次次级别反弹不创新高 → 二卖（课53）"""
    if c_idx + 2 < len(strokes):
        pb = strokes[c_idx + 2]
        if pb.direction == "up" and pb.end_value < strokes[c_idx].end_value:
            points.append(BuySellPoint(pb.end_date, "2卖", pb.end_value,
                f"一卖后回抽不创新高({strokes[c_idx].end_value:.2f})"))


# ==================== 7. 三类买卖点·简化（课17/20/21/53） ====================

class BuySellPoint:
    def __init__(self, date, ptype, price, detail):
        self.date = date
        self.type = ptype
        self.price = price
        self.detail = detail


def find_buy_sell_points(strokes: List[Stroke], zhongshus: List[Zhongshu],
                         klines: Optional[List[dict]] = None,
                         hist: Optional[List[float]] = None) -> List[BuySellPoint]:
    """三类买卖点（简化）：
      - 三买/三卖: 离开中枢后回试不破 ZG/ZD
      - 一买/一卖: ≥2同向中枢趋势 + a/c 段 MACD 柱面积比较（课24/37；无MACD时退化幅度×0.8）
      - 二买/二卖: 一买后第一次回抽不破一买位
    """
    points = []
    if not zhongshus:
        return points

    # 三买/三卖
    for zs in zhongshus:
        after = zs.entry_index + 3
        if after >= len(strokes):
            continue
        leave = strokes[after]
        if leave.direction == "up" and leave.end_value > zs.ZG:
            if after + 1 < len(strokes):
                pb = strokes[after + 1]
                if pb.direction == "down" and pb.end_value > zs.ZG:
                    points.append(BuySellPoint(pb.end_date, "3买", pb.end_value,
                        f"离开中枢[{zs.ZD:.2f},{zs.ZG:.2f}]后回试不破ZG，低点{pb.end_value:.2f}"))
        elif leave.direction == "down" and leave.end_value < zs.ZD:
            if after + 1 < len(strokes):
                pb = strokes[after + 1]
                if pb.direction == "up" and pb.end_value < zs.ZD:
                    points.append(BuySellPoint(pb.end_date, "3卖", pb.end_value,
                        f"离开中枢[{zs.ZD:.2f},{zs.ZG:.2f}]后回抽不破ZD，高点{pb.end_value:.2f}"))

    # 一买/一卖 + 二买/二卖（趋势背驰：a+A+b+B+c 的 A/C 段面积比较，课37）
    for i in range(len(zhongshus) - 1):
        z1, z2 = zhongshus[i], zhongshus[i + 1]
        # 下跌趋势（ZD递减）
        if z2.ZD < z1.ZD:
            a_idx, c_idx = z1.entry_index - 1, z2.entry_index + 3
            if 0 <= a_idx and c_idx < len(strokes) and strokes[c_idx].direction == "down" \
                    and strokes[a_idx].direction == "down":
                a_s, c_s = strokes[a_idx], strokes[c_idx]
                if klines is not None and hist is not None:
                    area_a = _macd_area(klines, hist, a_s.start_date, a_s.end_date)
                    area_c = _macd_area(klines, hist, c_s.start_date, c_s.end_date)
                    if c_s.end_value < a_s.end_value and area_c < area_a:
                        points.append(BuySellPoint(c_s.end_date, "1买", c_s.end_value,
                            f"下跌背驰(MACD面积 {area_c:.1f}<{area_a:.1f}，c创新低)"))
                        _append_2buy(points, strokes, c_idx)
                else:
                    lr = abs(c_s.end_value - c_s.start_value)
                    pr = abs(a_s.end_value - a_s.start_value)
                    if pr > 0 and lr < pr * 0.8:
                        points.append(BuySellPoint(c_s.end_date, "1买", c_s.end_value,
                            f"下跌趋势背驰(幅度{lr:.2f}<{pr:.2f}×0.8)"))
                        _append_2buy(points, strokes, c_idx)
        # 上涨趋势（ZG递增）
        if z2.ZG > z1.ZG:
            a_idx, c_idx = z1.entry_index - 1, z2.entry_index + 3
            if 0 <= a_idx and c_idx < len(strokes) and strokes[c_idx].direction == "up" \
                    and strokes[a_idx].direction == "up":
                a_s, c_s = strokes[a_idx], strokes[c_idx]
                if klines is not None and hist is not None:
                    area_a = _macd_area(klines, hist, a_s.start_date, a_s.end_date)
                    area_c = _macd_area(klines, hist, c_s.start_date, c_s.end_date)
                    if c_s.end_value > a_s.end_value and area_c < area_a:
                        points.append(BuySellPoint(c_s.end_date, "1卖", c_s.end_value,
                            f"上涨背驰(MACD面积 {area_c:.1f}<{area_a:.1f}，c创新高)"))
                        _append_2sell(points, strokes, c_idx)
                else:
                    lr = abs(c_s.end_value - c_s.start_value)
                    pr = abs(a_s.end_value - a_s.start_value)
                    if pr > 0 and lr < pr * 0.8:
                        points.append(BuySellPoint(c_s.end_date, "1卖", c_s.end_value,
                            f"上涨趋势背驰(幅度{lr:.2f}<{pr:.2f}×0.8)"))
                        _append_2sell(points, strokes, c_idx)
    return points


# ==================== 主入口 ====================

def analyze(code: str, name: str = "", datalen: int = 300) -> dict:
    print(f"拉取 {code} {name} 日K（腾讯前复权）...")
    klines = fetch_kline(code, datalen)
    if not klines:
        print("  获取失败")
        return {}
    print(f"  {len(klines)} 根日K ({klines[0]['date']} ~ {klines[-1]['date']})")

    merged = merge_klines(klines)
    print(f"  包含处理后: {len(merged)} 根合并K线")
    fracs = find_fractals(merged)
    tops = sum(1 for f in fracs if f.type == "top")
    print(f"  分型: {len(fracs)} 个 (顶{tops} + 底{len(fracs)-tops})")
    strokes = build_strokes(fracs)
    print(f"  笔: {len(strokes)} 笔")
    segs = build_segments(strokes)
    print(f"  线段(简化): {len(segs)} 段")
    zs = build_zhongshu(strokes)
    print(f"  中枢: {len(zs)} 个")
    dif, dea, macd_hist = calc_macd(klines)
    points = find_buy_sell_points(strokes, zs, klines=klines, hist=macd_hist)
    print(f"  买卖点(MACD背驰): {len(points)} 个")

    return {
        "code": code, "name": name,
        "kline_count": len(klines),
        "date_range": f"{klines[0]['date']} ~ {klines[-1]['date']}",
        "merged_count": len(merged),
        "fractal_count": len(fracs),
        "stroke_count": len(strokes),
        "segment_count": len(segs),
        "zhongshu_count": len(zs),
        "strokes": [{"dir": s.direction, "start": s.start_date, "end": s.end_date,
                     "from": round(s.start_value, 2), "to": round(s.end_value, 2)}
                    for s in strokes[-12:]],
        "zhongshus": [{"ZG": round(z.ZG, 2), "ZD": round(z.ZD, 2), "ZZZ": round(z.ZZZ, 2),
                       "start": z.start_date, "end": z.end_date} for z in zs],
        "buy_sell_points": [{"date": p.date, "type": p.type, "price": round(p.price, 2),
                             "detail": p.detail} for p in points],
        "latest_close": klines[-1]["close"],
        "latest_date": klines[-1]["date"],
    }


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "300308"
    name_map = {"300308": "中际旭创(002112第一重仓9.92%)",
                "300502": "新易盛(002112第二重仓9.81%)"}
    name = name_map.get(code, "")
    result = analyze(code, name)
    if not result:
        sys.exit(1)

    print(f"\n{'='*64}")
    print(f"  缠论结构分析: {code} {result['name']}")
    print(f"  数据范围: {result['date_range']} ({result['kline_count']}根日K)")
    print(f"{'='*64}")

    print(f"\n📊 结构统计:")
    print(f"  合并K线 {result['merged_count']} | 分型 {result['fractal_count']} | "
          f"笔 {result['stroke_count']} | 线段 {result['segment_count']} | 中枢 {result['zhongshu_count']}")

    print(f"\n📈 最近笔（末12笔）:")
    for s in result["strokes"]:
        arrow = "↑" if s["dir"] == "up" else "↓"
        print(f"  {arrow} {s['start']} ~ {s['end']}  {s['from']} → {s['to']}")

    if result["zhongshus"]:
        print(f"\n🎯 中枢:")
        for z in result["zhongshus"]:
            print(f"  {z['start']} ~ {z['end']}  ZG={z['ZG']} ZD={z['ZD']} 中轴={z['ZZZ']}")
        last_z = result["zhongshus"][-1]
        lc = result["latest_close"]
        pos = (f"上方(>ZG)" if lc > last_z["ZG"] else
               f"下方(<ZD)" if lc < last_z["ZD"] else "内部")
        print(f"\n📍 最新收盘 {lc} ({result['latest_date']}) 相对最近中枢: {pos}")
    else:
        print("\n📍 无中枢（趋势延伸或数据不足）")

    print(f"\n⚡ 买卖点事件(简化):")
    if result["buy_sell_points"]:
        for p in result["buy_sell_points"]:
            print(f"  {p['date']}  {p['type']}  @ {p['price']}  {p['detail']}")
    else:
        print("  无")

    print("\n[注] v0 验证版：线段/背驰为简化实现，详见脚本 docstring")
