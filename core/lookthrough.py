"""穿透分析层：基金前十大重仓股的走势结构观察（默认第四因子，回测裁决开关）。

指标口径（诚实命名，2026-08-23 对齐 chanlun skill 后升级）：
- 「MACD 背驰」为**动力学口径的缠论背驰近似**，非严格缠论背驰（严格版见 core/chanlun.py）：
  ① 同向柱簇力度 = 柱面积 ÷ 时间（课24 趋势平均力度）
  ② B 段黄白线（DIF/DEA 差）须回拉 0 轴附近才允许比较（课24 A/B/C 三段结构前提）
  ③ C 端须创新高/新低（课61）
  ④ 比较对象为相邻两个同号柱簇（其间恰隔一个反号簇）——skill 课64 要求
     「围绕同一中枢的两同向段」，此处以 B 段回 0 轴近似「经过同一中枢」，为已知简化
- 「吻结构」= MA5/20/60 多空排列，属缠论前传「吻体系」的或然性辅助工具（课25：
  不能与中枢理论混为一谈），不称缠论指标
- 「防狼术」= 日线 DIF 与 DEA 双双位于 0 轴下方 → 彻底离场警告（课103）

个股指标全部**因果计算**（任意 t 时刻状态只依赖 ≤t 的 K 线），可诚实回测。
"""
import datetime
import json
import re
import time
from pathlib import Path

from . import netutil, stock_data

BASE_DIR = Path(__file__).resolve().parent.parent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def _cfg() -> dict:
    return json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))["lookthrough"]


# ---------------------------------------------------------------- 持仓数据

# 静默缺年修复（2026-09-09，Summer 拍板；由 experiments/forecast_lab/holdings_hardened.py
# 验证后合入生产）。同日两次只读冻结实测：002112/2020 从 4 期静默变 0 期**且无任何告警**——
# HTTP 200 的降级页被解析成 [] → holdings_history 视为成功、不计入 failed_years
# → 样本少一整年 → RankIC 漂移 ±0.03~0.05（08-29 记录的现象，今日现行复现）。
# 留档目录在 forecast_outputs/（已 gitignore）。
_RAW_DIR = BASE_DIR / "forecast_outputs" / "f10_raw"

# 东财 F10 正常空态标记。**宁可判窄**：命中才认定「真无披露」，未命中一律按可疑降级处理。
# 漏判代价只是多两次重试；误判代价是又一轮静默缺年。
_EMPTY_MARKERS = ("暂无数据", "没有相关数据", 'content:""', "no data")


class DegradedResponse(RuntimeError):
    """HTTP 200 但响应形态可疑（降级页/反爬/改版），不得当作「该年无持仓披露」。"""

    def __init__(self, msg: str, *, kind: str, raw_path: str | None = None,
                 text_len: int = 0):
        super().__init__(msg)
        self.kind = kind          # SUSPECT_DEGRADED | PARSE_MISMATCH
        self.raw_path = raw_path
        self.text_len = text_len


def _dump_raw(fund_code: str, year: int, text: str) -> str | None:
    """降级响应留档供根因回溯（08-29 那次无留档，查不下去）。写档失败绝不影响主流程。"""
    try:
        _RAW_DIR.mkdir(parents=True, exist_ok=True)
        p = _RAW_DIR / f"{fund_code}_{year}_{time.strftime('%Y%m%d_%H%M%S')}.html"
        p.write_text(text, encoding="utf-8", errors="replace")
        return str(p)
    except Exception:
        return None


def fetch_holdings_year(fund_code: str, year: int) -> list[dict]:
    """东财 F10 历史持仓（季频，前十大）。返回 [{date, holdings:[{market,code,name,pct}]}] 按日期升序。

    零快照时走三态判定：EMPTY_CONFIRMED（确证无披露 → 返 []）/ SUSPECT_DEGRADED /
    PARSE_MISMATCH（两者抛 DegradedResponse，由 holdings_history 的既有重试+缺年告警接住）。
    本函数内**不再重试**：避免与外层 3 次重试嵌套放大请求数（东财为 IP 级频控）。
    """
    url = (f"https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
           f"?type=jjcc&code={fund_code}&topline=10&year={year}")
    # 2026-09-02: 走 netutil（IPv4 优先 + 无视环境死代理 + 瞬断重试）。
    text = netutil.http_get(url, headers={"User-Agent": UA,
                                          "Referer": "https://fundf10.eastmoney.com/"})

    snapshots = []
    for block in text.split("<div class='boxitem")[1:]:
        dm = re.search(r"截止至：<font class='px12'>(\d{4}-\d{2}-\d{2})", block)
        if not dm:
            continue
        date = dm.group(1)
        holdings = []
        for row in block.split("<tr>")[1:]:
            cm = re.search(r"unify/r/([01])\.(\d{6})", row)
            nm = re.search(r"class='tol'><a[^>]*>([^<]+)</a>", row)
            pm = re.search(r"class='tor'>([\d.]+)%", row)
            if cm and nm and pm:
                holdings.append({"market": cm.group(1), "code": cm.group(2),
                                 "name": nm.group(1), "pct": float(pm.group(1))})
        if holdings:
            snapshots.append({"date": date, "holdings": holdings[:_cfg()["top_n"]]})
    if not snapshots:
        n_box = len(re.findall(r"<div class='boxitem", text))
        hits = [m for m in _EMPTY_MARKERS if m in text]
        if n_box == 0 and hits:
            return []                        # EMPTY_CONFIRMED：真无披露（新基金常见）
        kind = "PARSE_MISMATCH" if n_box else "SUSPECT_DEGRADED"
        path = _dump_raw(fund_code, year, text)
        raise DegradedResponse(
            f"F10 {fund_code}/{year} 返回 0 期但非确证空态（{kind}，n_boxitem={n_box}，"
            f"markers={hits}，len={len(text)}，留档={path or '写档失败'}）"
            "——按拉取失败处理，勿当缺年",
            kind=kind, raw_path=path, text_len=len(text))
    snapshots.sort(key=lambda s: s["date"])
    return snapshots


# PIT 生效日滞后（天）：披露法定时限的**保守下界**，**不是真实公告日**。
# 2026-09-17 实测核实：东财 F10 jjcc 响应（fundf10 …?type=jjcc&year=）**不含公告日字段**，
# 只有「截止至：<报告期>」；真实公告日无零网络来源，故本层以「报告期 + 披露滞后」
# 近似生效日，并保证滞后 ≥ 法定披露时限（季报 15 个交易日 / 半年报 60 自然日 /
# 年报 90 自然日）⇒ 对「卡法定时限才披露」的基金亦无前视。
# 下界（按本地交易日历逐期取 max）＝ {03-31:25, 06-30:60, 09-30:29, 12-31:90}。
# 09-30 由 25 修正为 30（2026-09-17）：原值小于法定下界（国庆假期后 15 个交易日
# 最晚落到 10-29，需 29 天），近 3 年 09-30 期存在 3~4 天前视；+1 天余量后无前视，
# 样本选择仅变动 9/899 交易日（1.0%）。量化依据与判据见
# output/ops_runs/2026-09-17-pit-lag-impact.md 及 audit_project.py check_pit()。
_QTR_LAG = {"03-31": 25, "06-30": 65, "09-30": 30, "12-31": 95}


def _add_days(date: str, days: int) -> str:
    t = time.strptime(date, "%Y-%m-%d")
    return (datetime.date(t.tm_year, t.tm_mon, t.tm_mday)
            + datetime.timedelta(days=days)).isoformat()


def holdings_history(fund_code: str) -> list[dict]:
    """全部可得季报持仓，附 effective_date（披露滞后后的生效日，回测防前视用）。

    网络可见性（2026-08-27 修复）：此前每年代 fetch 失败静默 continue，
    网络/代理异常时返回空列表不报错——下游会带着 0 期持仓照常跑并出报告。
    现改为：失败年份计入并在函数结束时 warnings.warn 显式告警（全部失败/部分失败都报）。

    v7 复现性加固（2026-08-29）：失败年份重试 2 次（退避 2s/5s）。背景：同日两次重跑
    中代理抖动曾致 002112 静默少拉 1 年（22 vs 26 期），特征向量漂移 → RankIC 漂移
    ±0.03~0.05。重试把「一次性抖动」与「真不可达」分开，减少静默缺年。

    三态判定合入（2026-09-09）：降级页抛 DegradedResponse → 走上面的重试与缺年告警通道，
    因此**告警覆盖面变大**（以前只有抛异常的失败才算缺年，现在 0 期非确证空态也算）；
    数据与模型一行未动，代价仅最坏情况多 2 次重试（只在真发生降级时付出）。
    """
    import time as _time
    import warnings
    snaps = []
    years = _cfg()["history_years"]
    failed_years: list[int] = []
    last_err: Exception | None = None
    for year in years:
        got = None
        for attempt in range(3):          # 首发 + 2 次重试（退避 2s/5s）
            try:
                got = fetch_holdings_year(fund_code, year)
                break
            except Exception as e:
                last_err = e
                if attempt < 2:
                    _time.sleep(2 * (attempt + 1))
        if got is None:
            failed_years.append(year)
            continue
        snaps += got
    if failed_years:
        # 降级页与断网的**原因和解法不同**（代理开没开 vs 服务端改版），告警必须分得开。
        hint = ("东财返回降级页/改版（非网络问题，原始响应已留档 forecast_outputs/f10_raw/）"
                if isinstance(last_err, DegradedResponse)
                else "多为网络受限/代理失效，请检查网络或系统代理设置")
        msg = (f"holdings_history({fund_code}): {len(failed_years)}/{len(years)} 年持仓拉取失败"
               f"（含 2 次重试；失败年份: {failed_years}；"
               f"最后错误: {type(last_err).__name__}: {last_err}）——{hint}")
        if not snaps:
            warnings.warn(msg)
        else:
            warnings.warn(msg + "（部分年份持仓缺失，样本量受影响）")
    snaps.sort(key=lambda s: s["date"])
    out, seen = [], set()
    for s in snaps:
        if s["date"] in seen:
            continue
        seen.add(s["date"])
        lag = _QTR_LAG.get(s["date"][5:], 95)
        s["effective_date"] = _add_days(s["date"], lag)
        out.append(s)
    return out


def effective_snapshot(history: list[dict], d: str) -> dict | None:
    """d 当日『当时已可知道』的持仓（生效日 ≤ d 的最近一期）。"""
    pick = None
    for s in history:
        if s["effective_date"] <= d:
            pick = s
        else:
            break
    return pick


# --------------------------------------------- 个股因果指标（动力学口径）

class MacdDynamics:
    """增量式 MACD 背驰近似检测（升级版：面积÷时间 + 黄白线回0轴过滤 + 防狼术）。

    柱簇 = 连续同号 MACD 柱。背驰在第二个同号柱簇【结束翻号】时确认（保守），
    有效性 N 根。B 段回 0 轴判定：间隔反号簇期间 |DIF-DEA 差的载体 DIF| 最小值
    足够接近 0（≤ zero_pull_ratio × 两比较簇的 max|DIF|）。
    """

    def __init__(self, area_ratio: float = 0.8, valid_bars: int = 15, min_len: int = 2,
                 zero_pull_ratio: float = 0.15):
        self.area_ratio, self.valid_bars, self.min_len = area_ratio, valid_bars, min_len
        self.zr = zero_pull_ratio
        self.ema12 = self.ema26 = self.dea = None
        self.dif = 0.0
        self.sign, self.area, self.length, self.end_close = 0, 0.0, 0, None
        self.max_abs_dif, self.min_abs_dif = 0.0, None   # 当前簇的 |DIF| 极值
        self.done: list[dict] = []   # 已完成柱簇：{sign, area, length, end_close, max_abs_dif, min_abs_dif}
        self.div_state, self.div_until_idx, self.idx = 0, -1, -1

    def _close_cluster(self):
        if self.length >= self.min_len and self.area > 0:
            cur = {"sign": self.sign, "area": self.area, "length": self.length,
                   "end_close": self.end_close, "momentum": self.area / self.length,
                   "max_abs_dif": self.max_abs_dif, "min_abs_dif": self.min_abs_dif}
            prev_same = [c for c in self.done if c["sign"] == self.sign]
            # done 尾部应为 [..., A(同号), B(反号)]：B 是紧邻的反号簇
            b_seg = self.done[-1] if self.done and self.done[-1]["sign"] == -self.sign else None
            if prev_same and b_seg and b_seg["min_abs_dif"] is not None:
                a_seg = prev_same[-1]
                zero_pull_ok = b_seg["min_abs_dif"] <= self.zr * max(
                    a_seg["max_abs_dif"], cur["max_abs_dif"])
                mom_div = cur["momentum"] < self.area_ratio * a_seg["momentum"]
                if zero_pull_ok and mom_div:
                    if self.sign > 0 and cur["end_close"] > a_seg["end_close"]:
                        self.div_state, self.div_until_idx = -1, self.idx + self.valid_bars  # 顶背驰
                    elif self.sign < 0 and cur["end_close"] < a_seg["end_close"]:
                        self.div_state, self.div_until_idx = +1, self.idx + self.valid_bars  # 底背驰
            self.done.append(cur)
            self.done = self.done[-6:]

    def update(self, close: float) -> int:
        """喂入收盘价（前复权），返回当前背驰信号：+1 底背驰 / -1 顶背驰 / 0 无。"""
        self.idx += 1
        if self.ema12 is None:
            self.ema12 = self.ema26 = self.dea = close
            hist = 0.0
        else:
            self.ema12 += (close - self.ema12) * 2 / 13
            self.ema26 += (close - self.ema26) * 2 / 27
            dif = self.ema12 - self.ema26
            self.dea += (dif - self.dea) * 2 / 10
            hist = 2 * (dif - self.dea)
        self.dif = self.ema12 - self.ema26

        s = 1 if hist > 1e-9 else (-1 if hist < -1e-9 else 0)
        if s == self.sign and s != 0:
            self.area += abs(hist)
            self.length += 1
            self.end_close = close
            self.max_abs_dif = max(self.max_abs_dif, abs(self.dif))
            self.min_abs_dif = min(self.min_abs_dif, abs(self.dif)) if self.min_abs_dif is not None else abs(self.dif)
        else:
            self._close_cluster()
            self.sign, self.area, self.length, self.end_close = s, abs(hist), 1 if s else 0, close
            self.max_abs_dif = abs(self.dif)
            self.min_abs_dif = abs(self.dif)

        return self.div_state if self.idx <= self.div_until_idx else 0

    @property
    def fanglang(self) -> int:
        """防狼术（课103）：日线黄白线双双在 0 轴下 = 1（彻底离场警告）。"""
        return 1 if (self.ema12 is not None and self.ema12 < self.ema26 and self.dea < 0) else 0


class StockSignals:
    """增量计算单只股票的指标序列：吻结构 / MACD背驰 / 距60日高点 / 防狼术。"""

    def __init__(self, cfg: dict):
        c = cfg["dynamics"]
        self.wins = c["ma"]
        self.dd_window = c["dd_window"]
        self.dyn = MacdDynamics(c["div_area_ratio"], c["div_valid_bars"],
                                zero_pull_ratio=c["zero_pull_ratio"])
        self.buf: list[tuple] = []
        self.series: dict[str, dict] = {}

    def update(self, date: str, close: float, high: float):
        div = self.dyn.update(close)
        self.buf.append((close, high))
        n = len(self.buf)
        ma = {w: sum(x[0] for x in self.buf[n - w:]) / w for w in self.wins if n >= w}
        if len(ma) == len(self.wins):
            a, b, c2 = (ma[w] for w in sorted(self.wins))  # a=短均线, c2=长均线
            ma_sig = 1 if a > b > c2 else (-1 if a < b < c2 else 0)
        else:
            ma_sig = 0
        dd = close / max(x[1] for x in self.buf[n - self.dd_window:]) - 1 if n >= self.dd_window else None
        self.series[date] = {"ma": ma_sig, "div": div, "dd": dd, "fanglang": self.dyn.fanglang}


def stock_signal_series(klines: list, cfg: dict) -> dict[str, dict]:
    """klines: [(date, open, close, high, low), ...] → {date: {ma, div, dd, fanglang}}（因果）。"""
    ss = StockSignals(cfg)
    for date, _o, close, high, _l in klines:
        ss.update(date, close, high)
    return ss.series


def signal_at(series: dict[str, dict], d: str) -> dict | None:
    """d 日信号；个股当日停牌则取 ≤d 的最近一日。"""
    if d in series:
        return series[d]
    ks = [k for k in series if k <= d]
    return series[ks[-1]] if ks else None


# ---------------------------------------------------------------- 聚合

def aggregate(snapshot: dict, series_map: dict[str, dict], d: str, cfg: dict) -> dict | None:
    """按披露权重聚合持仓个股信号 → 基金级穿透视图（吻结构/净背驰/防狼术/综合）。"""
    rows, wsum, w_valid = [], 0.0, 0.0
    struct_num, div_num, dd_num, fang_num = 0.0, 0.0, 0.0, 0.0
    for h in snapshot["holdings"]:
        w = h["pct"]
        wsum += w
        s = series_map.get(h["code"])
        sig = signal_at(s, d) if s else None
        if sig and sig["ma"] is not None:
            w_valid += w
            struct_num += w * sig["ma"]
            div_num += w * sig["div"]
            dd_num += w * (sig["dd"] or 0.0)
            fang_num += w * sig["fanglang"]
            rows.append({**h, **sig})
        else:
            rows.append({**h, "ma": None, "div": 0, "dd": None, "fanglang": 0})
    if w_valid < wsum * 0.5 or w_valid == 0:
        return None
    structure = struct_num / w_valid
    div_net = div_num / w_valid
    dd_avg = dd_num / w_valid
    fang_ratio = fang_num / w_valid
    a = cfg["aggregate"]
    if structure >= a["structure_bull"] and div_net > -a["div_net_guard"]:
        composite = 1
    elif structure <= a["structure_bear"] and div_net < a["div_net_guard"]:
        composite = -1
    else:
        composite = 0
    return {"date": d, "snapshot_date": snapshot["date"], "structure": structure,
            "div_net": div_net, "dd_avg": dd_avg, "composite": composite,
            "fanglang_ratio": fang_ratio, "fanglang_alert": fang_ratio >= 0.5,
            "coverage": w_valid / wsum, "rows": rows}


# ------------------------------------------------------------ 报告视图

def evaluate_lookthrough(fund_codes: list[str]) -> dict[str, dict]:
    """当前最新披露持仓的穿透观察（报告 + 可选第四因子 + 缠论结构事件）。失败降级返回空 dict。"""
    from . import chanlun as cl

    cfg = _cfg()
    out: dict[str, dict] = {}
    for code in fund_codes:
        try:
            history = holdings_history(code)
            if not history:
                continue
            # PIT（2026-09-23 一行修复）：取最近已生效期（effective_date ≤ 今日），
            # 不再盲目取 history[-1]——披露滞后窗内（年报最长 95 天）最新一期
            # 尚未可知，取了即让实时报告前视（回测侧一直用 effective_snapshot，
            # 报告侧补齐对齐）。无生效期 → 跳过，由 run.py lt_missing 探针显式降级。
            snap = effective_snapshot(history, time.strftime("%Y-%m-%d"))
            if not snap:
                continue
            series_map, events = {}, []
            for h in snap["holdings"]:
                try:
                    k = stock_data.fetch_stock_kline(h["code"], h["market"])
                    series_map[h["code"]] = stock_signal_series(k["klines"], cfg)
                    r = cl.analyze(k["klines"])
                    for e in r["events"]:
                        events.append({"type": e["type"], "confirm_date": e["confirm_date"],
                                       "pct": h["pct"], "name": h["name"]})
                except Exception:
                    continue
            last_dates = [max(s) for s in series_map.values() if s]
            d = min(last_dates) if last_dates else snap["date"]
            agg = aggregate(snap, series_map, d, cfg)
            if agg:
                # 近 30 个自然日内确认的缠论事件（按披露权重聚合）
                cutoff = _add_days(d, -30)
                recent = [e for e in events if e["confirm_date"] > cutoff]
                agg["chanlun_events"] = recent
                agg["fund"] = code
                out[code] = agg
        except Exception:
            continue
    return out
