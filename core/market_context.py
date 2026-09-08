# -*- coding: utf-8 -*-
"""Market Context 观察层 v0.1（2026-09-03，自 v0 2026-09-02 演进）。

从进攻/防守篮子代理的已完成交易日收盘价，计算市场环境快照：
- 每主题代理 1D / 5D / 20D 收益与 MA20 相对位置
- offensive_score / defensive_score：各篮子内代理 5D 收益均值（等权）
- spread = offensive - defensive（>0 偏进攻）
- breadth 三口径（v0.1 统一命名；all = 全部 13 主题篮子，非仅进攻侧）：
  breadth_all_above_ma20 / breadth_original_above_ma20 / breadth_relaxed_above_ma20（0~1）
- regime 标签（纯描述，四档）：risk-on / neutral / risk-off / unknown

纪律（与 t1_watchlist / basket 文件一致）：
- 仅观察（paper-only），不接 Forecast、不接 Policy、不改变任何门禁
- PIT：只用 ≤ 今日已完成交易日的收盘价；缓存当日不重复拉
- 代理失败逐项跳过并记录，绝不抛异常阻断主流程
- gate=relaxed 与 BK 承接项按 basket 原样标记，分层留待增量实验
"""
import json
import time
from datetime import datetime, date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
BASKET_PATH = BASE_DIR / "data" / "basket_20260902.json"
SNAP_DIR = BASE_DIR / "data" / "market_context"
_STOCK_CACHE = BASE_DIR / "data" / "stock_klines"
_BK_CACHE = BASE_DIR / "data" / "sector_klines"
_TTL_HOURS = 12.0
from core import stock_data  # noqa: E402  （复用 fetch_stock_kline 主链路）


def _load_basket() -> dict:
    return json.loads(BASKET_PATH.read_text(encoding="utf-8"))


def _closes_for(code: str, is_bk: bool) -> list[tuple[str, float]]:
    """返回 [(date, close)] 升序。ETF 走 fetch_stock_kline 主链路，
    BK 走 data/sector_klines/{code}.json 缓存。"""
    if is_bk:
        p = _BK_CACHE / f"{code}.json"
        if not p.exists():
            raise FileNotFoundError(f"BK 缓存缺失：{p.name}")
        rec = json.loads(p.read_text(encoding="utf-8"))
        # fields: date, open, close, high, low, volume → (date, close)
        return [(row[0], float(row[2])) for row in rec.get("klines", [])]
    p = _STOCK_CACHE / f"{code}.json"
    if p.exists() and time.time() - p.stat().st_mtime < _TTL_HOURS * 3600:
        rec = json.loads(p.read_text(encoding="utf-8"))
    else:
        market = "1" if code[0] in "56" else "0"
        rec = stock_data.fetch_stock_kline(code, market, ttl_hours=_TTL_HOURS)
    out = []
    for k in rec.get("klines", []):
        d, c = (k[0], k[2]) if isinstance(k, (list, tuple)) else (k["date"], k["close"])
        out.append((str(d), float(c)))
    return out


def _pct(a: float, b: float) -> float | None:
    return (a / b - 1.0) * 100.0 if b else None


def _theme_metrics(closes: list[tuple[str, float]], as_of_date: str | None = None) -> dict | None:
    """从单代理收盘序列算观察指标。严格 PIT：只用 < as_of 的已完成交易日 bar
    （盘中缓存的当日半根 bar 一律丢弃，pre/mid/post 三时点口径一致）。

    as_of_date 默认今天（实时运行）；提供显式日期即用于历史重演/OOS，
    保证过去某天的快照用当时能看到的数据重算，口径一致。"""
    today = as_of_date or datetime.now().strftime("%Y-%m-%d")
    closes = [(d, c) for d, c in closes if d < today]
    if len(closes) < 21:
        return None
    dates = [d for d, _ in closes]
    px = [c for _, c in closes]
    ma20 = sum(px[-20:]) / 20.0
    last = px[-1]
    r1 = _pct(last, px[-2])
    r5 = _pct(last, px[-6]) if len(px) >= 6 else None
    r20 = _pct(last, px[-21])
    return {"as_of": dates[-1], "r1d": r1, "r5d": r5, "r20d": r20,
            "above_ma20": bool(last > ma20), "bars": len(px)}


def classify_regime(spread: float | None, breadth: float | None) -> str:
    """纯函数：由 spread 与 breadth 得出四档 regime 标签（描述性，非预测）。

    - 任一缺失 → unknown
    - spread>0 且 breadth>=0.6 → risk-on
    - spread<0 且 breadth<=0.4 → risk-off
    - 其它有效组合 → neutral
    """
    if spread is None or breadth is None:
        return "unknown"
    if spread > 0 and breadth >= 0.6:
        return "risk-on"
    if spread < 0 and breadth <= 0.4:
        return "risk-off"
    return "neutral"


def compute(slot: str = "post", save: bool = True, snap_dir: Path | None = None,
            as_of_date: str | None = None) -> dict:
    """计算市场环境快照（v0.1：全链路支持历史重演）。

    as_of_date 给定即重算那一天：PIT 截断、顶层 as_of、快照文件名都用该日期；
    缺省 = 今天（实时运行）。任何异常都被吞掉并记入 errors，不阻断主流程。
    """
    as_of = as_of_date or datetime.now().strftime("%Y-%m-%d")
    errors: list[str] = []
    try:
        basket = _load_basket()
    except Exception as e:
        return {"ok": False, "errors": [f"basket 读取失败: {e}"], "slot": slot,
                "as_of": as_of}

    themes: dict[str, dict] = {}
    for side in ("offensive", "defensive"):
        for theme in basket.get(side, []):
            proxies = (basket.get("proxies") or {}).get(theme) or []
            if not proxies:
                errors.append(f"{theme}: 无代理")
                continue
            picked = None
            # 代理固定策略（2026-09-02 P0）：primary = 列表首个，fallback 仅在 primary
            # 失败时按序启用；一旦启用 fallback 就打 proxy_switch 标记并记录选用代理，
            # 避免时间序列在不同代理间静默切换（增量实验必须知道信号来自哪只）。
            primary = proxies[0]
            for idx, pr in enumerate(proxies):
                code = pr.get("code", "")
                try:
                    m = _theme_metrics(_closes_for(code, pr.get("type") == "bk_push2his"),
                                       as_of_date=as_of)
                except Exception as e:
                    errors.append(f"{theme}/{code}: {type(e).__name__} {str(e)[:60]}")
                    continue
                if m:
                    ds = "bk_push2his" if pr.get("type") == "bk_push2his" else "tencent_etf"
                    picked = {"code": code, "name": pr.get("name", code),
                              "gate": pr.get("gate", "original"),
                              "proxy_switch": idx != 0,
                              "proxy_primary": primary.get("code", ""),
                              "data_source": ds, **m}
                    break
            if picked:
                themes[theme] = {"side": side, **picked}

    if not themes:
        return {"ok": False, "errors": errors or ["无任何主题数据"], "slot": slot,
                "as_of": as_of}

    def _avg(side: str, key: str, gate: str | None = None) -> float | None:
        vals = [t[key] for t in themes.values()
                if t["side"] == side and t.get(key) is not None
                and (gate is None or t.get("gate") == gate)]
        return sum(vals) / len(vals) if vals else None

    off5, def5 = _avg("offensive", "r5d"), _avg("defensive", "r5d")
    # original / relaxed 分层（2026-09-02 P0）：观察层全部给出，增量实验可回答
    # "结论由长期代理得出，还是被短历史 relaxed 代理影响"。relaxed 不含 BK 承接项。
    off5_o = _avg("offensive", "r5d", "original")
    def5_o = _avg("defensive", "r5d", "original")
    off5_r = _avg("offensive", "r5d", "relaxed")
    def5_r = _avg("defensive", "r5d", "relaxed")
    spread_o = (off5_o - def5_o) if (off5_o is not None and def5_o is not None) else None
    spread_r = (off5_r - def5_r) if (off5_r is not None and def5_r is not None) else None
    spread = (off5 - def5) if (off5 is not None and def5 is not None) else None
    above_all = [t for t in themes.values() if t.get("above_ma20")]
    breadth_all = len(above_all) / len(themes) if themes else None
    # original / relaxed 分层广度（2026-09-02 P1）：与 off/def 分层同口径，
    # 区分结论受 relaxed 短历史代理影响的程度。
    above_o = [t for t in themes.values() if t.get("above_ma20") and t.get("gate") == "original"]
    above_r = [t for t in themes.values() if t.get("above_ma20") and t.get("gate") == "relaxed"]
    n_o = sum(1 for t in themes.values() if t.get("gate") == "original")
    n_r = sum(1 for t in themes.values() if t.get("gate") == "relaxed")
    breadth_original = len(above_o) / n_o if n_o else None
    breadth_relaxed = len(above_r) / n_r if n_r else None
    regime = classify_regime(spread, breadth_all)

    # Data Quality 汇总（2026-09-03 v0.1 升级）：coverage + usable_for_oos，
    # 供 OOS/增量实验直接按 usable_for_oos 过滤，而不是重新解释一堆字段。
    n_themes = len(themes)
    n_original = n_o
    n_relaxed = n_r
    n_switch = sum(1 for d in themes.values() if d.get("proxy_switch"))
    n_bk = sum(1 for d in themes.values() if d.get("data_source") == "bk_push2his")
    themes_expected = len(basket.get("offensive", [])) + len(basket.get("defensive", []))
    coverage = (n_themes / themes_expected) if themes_expected else None
    usable = bool(coverage == 1.0 and n_switch == 0 and not errors)
    # v0.1 封版补充（2026-09-04）：降级原因机器可读，60 日实验直接按 reason 过滤，
    # 不必重新解析规则；代理切换清单进顶层，研究"失效日是否恰好是切换日"零成本。
    reasons: list[str] = []
    if coverage is None or coverage < 1.0:
        reasons.append("coverage<1")
    if n_switch > 0:
        reasons.append("proxy_switch")
    if errors:
        reasons.append("errors>0")
    switched_themes = sorted(t for t, d in themes.items() if d.get("proxy_switch"))
    quality = {
        "themes_total": n_themes, "themes_expected": themes_expected,
        "coverage": coverage,
        "themes_original": n_original,
        "themes_relaxed": n_relaxed, "themes_switched": n_switch,
        "themes_bk_source": n_bk, "errors": len(errors),
        # OOS 可用性（v0.1）：coverage 缺口 / fallback 切换 / 任何错误 → degraded。
        # 生产观察照常继续，仅标记该日样本不进严格 OOS 口径。
        "usable_for_oos": usable,
        "usable_for_oos_reason": reasons,
        "oos_quality": "ok" if usable else "degraded",
    }
    proxy_switch_themes = switched_themes or None

    snap = {
        "ok": True, "slot": slot, "as_of": as_of,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "themes": themes, "errors": errors,
        "offensive_score_5d": off5, "defensive_score_5d": def5,
        "offensive_original_5d": off5_o, "defensive_original_5d": def5_o,
        "offensive_relaxed_5d": off5_r, "defensive_relaxed_5d": def5_r,
        "spread_5d": spread, "spread_original_5d": spread_o,
        "spread_relaxed_5d": spread_r,
        "breadth_all_above_ma20": breadth_all,
        "breadth_original_above_ma20": breadth_original,
        "breadth_relaxed_above_ma20": breadth_relaxed,
        "proxy_switch_markers": {t: d.get("proxy_switch") for t, d in themes.items() if d.get("proxy_switch")} or None,
        "proxy_switch_themes": proxy_switch_themes,
        "regime": regime,
        "market_context_quality": quality,
        "disclaimer": "观察层 v0.1 · 仅描述，不接 Forecast/Policy，不构成投资建议",
    }

    snap_dir = snap_dir or SNAP_DIR
    if save:
        try:
            snap_dir.mkdir(exist_ok=True, parents=True)
            # v0.1：文件名跟随 as_of（历史重演写历史日期，不污染/不覆盖实时序列）
            path = snap_dir / f"{as_of}.json"
            path.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
            # v0.1：优先报告相对路径；snap_dir 不在 BASE_DIR 下（测试/自定义目录）时退回绝对路径
            try:
                shown = str(Path(path).relative_to(BASE_DIR))
            except ValueError:
                shown = str(path)
            snap["saved_to"] = shown
            # v0.1 封版补充（2026-09-04）：每日一行追加 history.jsonl（重演/测试不写入，
            # 只在实时 save 时追加；已有同日行不覆盖——首算为准）。
            if as_of_date is None:
                hist = snap_dir / "history.jsonl"
                rec = {"date": as_of, "slot": slot, "regime": regime,
                       "offensive_5d": off5, "defensive_5d": def5,
                       "spread_5d": spread, "spread_original_5d": spread_o,
                       "spread_relaxed_5d": spread_r,
                       "breadth_all": breadth_all,
                       "breadth_original": breadth_original,
                       "breadth_relaxed": breadth_relaxed,
                       "themes_total": n_themes, "themes_expected": themes_expected,
                       "coverage": coverage,
                       "themes_switched": n_switch,
                       "themes_bk_source": n_bk,
                       "errors": len(errors),
                       "usable_for_oos": usable,
                       "oos_quality": "ok" if usable else "degraded",
                       "usable_for_oos_reason": reasons or None,
                       "proxy_switch_themes": proxy_switch_themes}
                existing = ""
                if hist.exists():
                    existing = hist.read_text(encoding="utf-8")
                if f'"date": "{as_of}"' not in existing:
                    with hist.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            errors.append(f"快照落盘失败: {e}")
    return snap


def render_section(snap: dict | None) -> str | None:
    """报告 markdown 段（供 report_generator 调用）。无数据返回 None。

    v0.1：展示全篮子 breadth 口径 + Data Quality 一行（含 OOS 降级标记）；
    兼容 v0 旧快照的 breadth_above_ma20 键名。"""
    if not snap or not snap.get("ok"):
        return None
    reg = snap.get("regime", "unknown")
    reg_cn = {"risk-on": "偏进攻", "risk-off": "偏防守", "neutral": "均衡"}.get(reg, "未知")
    lines = [f"- **风格判读（描述性标签）**：{reg_cn}"
             f"（offensive 5D {snap['offensive_score_5d']:+.2f}% / defensive 5D "
             f"{snap['defensive_score_5d']:+.2f}% / spread {snap['spread_5d']:+.2f}%）"]
    b_all = snap.get("breadth_all_above_ma20", snap.get("breadth_above_ma20"))
    if b_all is not None:
        lines.append(f"- **广度（全篮子 MA20）**：{b_all*100:.0f}% 代理站上 MA20")
    q = snap.get("market_context_quality") or {}
    if q.get("themes_total") is not None:
        oos_flag = "" if q.get("usable_for_oos", True) else " · ⚠️ OOS 降级"
        exp = q.get("themes_expected") or q.get("themes_total")
        lines.append(f"- **数据质量**：{q['themes_total']}/{exp} 主题有效"
                     f" · original {q.get('themes_original', 0)} · relaxed {q.get('themes_relaxed', 0)}"
                     f" · fallback切换 {q.get('themes_switched', 0)} · 错误 {q.get('errors', 0)}{oos_flag}")
    rows = ["| 主题 | 侧 | 代理 | 1D | 5D | 20D | MA20上 |", "|---|---|---|---:|---:|---:|---|"]
    for theme, t in snap.get("themes", {}).items():
        f = lambda v: "—" if v is None else f"{v:+.2f}%"
        rows.append(f"| {theme} | {'进攻' if t['side']=='offensive' else '防守'} "
                    f"| {t['name']} | {f(t['r1d'])} | {f(t['r5d'])} | {f(t['r20d'])} "
                    f"| {'✓' if t['above_ma20'] else '✗'} |")
    lines += [""] + rows
    if snap.get("errors"):
        lines.append("")
        lines.append(f"- ⚠️ 缺失项：{'；'.join(snap['errors'][:6])}"
                     + ("…" if len(snap["errors"]) > 6 else ""))
    lines.append("")
    lines.append(f"*{snap.get('disclaimer', '观察层 · 不构成投资建议')}*")
    return "\n".join(lines)
