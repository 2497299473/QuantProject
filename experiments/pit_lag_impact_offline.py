"""PIT 滞后口径离线影响量化（零网络 / 只读，2026-09-17）。

背景：审计 P0-1 判 FAIL —— core/lookthrough.py 用固定滞后 _QTR_LAG 近似公告日。
2026-09-17 实测：东财 F10 jjcc 响应**不含公告日字段**（只有「截止至」报告期），
故真实公告日无零网络来源。候选路线＝把 _QTR_LAG 显式定义并声明为
「披露法定时限的**保守滞后下界**」而不是「真实公告日」。

本脚本用数字回答三问（全程不发任何网络请求）：
  Q1 现行滞后 vs 其他候选口径，对**样本选择**（effective_snapshot 选到哪一期）的实际影响有多大？
  Q2 现行滞后相对「法定最晚披露日」是否真有安全余量？即它是否**真的是保守下界**
     （假定生效日 ≥ 法定最晚披露日 ⇒ 不存在前视；反之存在前视风险）？
  Q3 各季滞后取值敏感性：滞后每变动 ±N 天，样本选择分歧多少？

另附：审计 check_pit() 判级规则的**字面量模拟**（透明展示改名的后果，不作建议）。

用法：
    python experiments/pit_lag_impact_offline.py            # 打印 + 写 md 报告
    python experiments/pit_lag_impact_offline.py --no-write # 只打印
"""
from __future__ import annotations

import argparse
import bisect
import datetime
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core import lookthrough as lt  # noqa: E402

TODAY = datetime.date(2026, 9, 17)
STUDY_START = "2023-01-01"                 # 观察窗：覆盖近 3 年可回测样本
OUT_MD = BASE / "output" / "ops_runs" / f"{TODAY.isoformat()}-pit-lag-impact.md"
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))["lookthrough"]

# 法定披露时限（公开规则，用于构造「法定最晚披露日」参照）
LEGAL_QTR_WORKING = 15     # 季报：季度结束之日起 15 个工作日
LEGAL_HALF_CAL = 60        # 半年报：60 个自然日
LEGAL_YEAR_CAL = 90        # 年报：90 个自然日

CANDIDATES = {
    "现行 _QTR_LAG": dict(lt._QTR_LAG),
    "法定时限折自然日(21/60/21/90)": {"03-31": 21, "06-30": 60, "09-30": 21, "12-31": 90},
    "现行-7d（更激进）": {k: v - 7 for k, v in lt._QTR_LAG.items()},
    "现行+7d（更保守）": {k: v + 7 for k, v in lt._QTR_LAG.items()},
}


# ---------------------------------------------------------------- 数据准备

def load_trading_days() -> tuple[list[str], str]:
    """真实 A 股交易日（取自本地面板 K 线）；取不到则退回工作日近似并标注来源。"""
    for name in ("510300", "510050", "159915"):
        p = BASE / "data" / "stock_klines" / f"{name}.json"
        if not p.is_file():
            continue
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        kl = raw.get("klines") if isinstance(raw, dict) else raw
        if not isinstance(kl, list):
            continue
        out: list[str] = []
        for r in kl:
            if isinstance(r, (list, tuple)) and r and isinstance(r[0], str):
                out.append(r[0][:10])
            elif isinstance(r, dict):
                d = r.get("date") or r.get("day") or r.get("datetime")
                if isinstance(d, str):
                    out.append(d[:10])
        if out:
            return sorted(set(out)), f"本地 K 线 {name}.json"
    d, out = datetime.date(2020, 1, 1), []
    while d <= TODAY:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out, "工作日近似（本地 K 线缺失，结论精度下降）"


def period_grid() -> list[str]:
    """配置覆盖年份 × 四个季末，截到今日（未来报告期不算已披露）。"""
    out = []
    for y in CFG["history_years"]:
        for mmdd in ("03-31", "06-30", "09-30", "12-31"):
            d = f"{y}-{mmdd}"
            if d <= TODAY.isoformat():
                out.append(d)
    return sorted(out)


def history_under(periods: list[str], lag_map: dict) -> list[dict]:
    """与 core.lookthrough.holdings_history 同一口径构造 effective_date。"""
    hist = [{"date": d, "effective_date": lt._add_days(d, lag_map.get(d[5:], 95))}
            for d in periods]
    hist.sort(key=lambda s: s["date"])
    return hist


def pick(hist: list[dict], day: str) -> str | None:
    s = lt.effective_snapshot(hist, day)
    return s["date"] if s else None


def legal_deadline(period: str, tdays: list[str]) -> tuple[str | None, str]:
    """法定最晚披露日：季报按 15 个交易日，半年报/年报按自然日。"""
    mmdd = period[5:]
    if mmdd in ("03-31", "09-30"):
        i = bisect.bisect_right(tdays, period)          # 期后第一个交易日
        j = i + LEGAL_QTR_WORKING - 1
        if j < len(tdays):
            return tdays[j], "15 个交易日"
        return None, "15 个交易日（样本不足）"
    days = LEGAL_HALF_CAL if mmdd == "06-30" else LEGAL_YEAR_CAL
    return lt._add_days(period, days), f"{days} 个自然日"


# ---------------------------------------------------------------- 主分析

def main() -> int:
    ap = argparse.ArgumentParser(description="PIT 滞后口径离线影响量化（零网络）")
    ap.add_argument("--no-write", action="store_true", help="只打印，不写 md")
    args = ap.parse_args()

    tdays, cal_src = load_trading_days()
    periods = period_grid()
    base_hist = history_under(periods, lt._QTR_LAG)
    days = [d for d in tdays if STUDY_START <= d <= TODAY.isoformat()]

    L: list[str] = []
    L.append("# PIT 滞后口径离线影响量化（P0-1 决策支持）")
    L.append("")
    L.append(f"- 生成时间：{TODAY.isoformat()}（**零网络**，只读，未改动 core/）")
    L.append(f"- 交易日历：{cal_src}，共 {len(tdays)} 日")
    L.append(f"- 报告期网格：{len(periods)} 期（{periods[0]} … {periods[-1]}）")
    L.append(f"- 观察窗：{STUDY_START} … {TODAY.isoformat()}，{len(days)} 个交易日")
    L.append(f"- 现行 `_QTR_LAG` = {lt._QTR_LAG}")
    L.append("")

    # ---- Q1：候选口径 vs 现行，样本选择分歧 ----
    L.append("## Q1 候选口径对样本选择的影响（vs 现行）")
    L.append("")
    L.append("| 口径 | 滞后(天) | 选到不同期的天数 | 占比 | 最大陈旧度变化(天) |")
    L.append("|---|---|---|---|---|")
    base_pick = {d: pick(base_hist, d) for d in days}

    def staleness(hist, d, day):
        s = pick(hist, d)
        if not s:
            return None
        return (datetime.date.fromisoformat(day) - datetime.date.fromisoformat(s)).days

    for label, lag_map in CANDIDATES.items():
        if label == "现行 _QTR_LAG":
            continue
        h = history_under(periods, lag_map)
        diff = 0
        max_ds = 0
        for day in days:
            p = pick(h, day)
            if p != base_pick[day]:
                diff += 1
                sb, sn = staleness(base_hist, day, day), staleness(h, day, day)
                if sb is not None and sn is not None:
                    max_ds = max(max_ds, abs(sn - sb))
        pct = 100.0 * diff / len(days) if days else 0.0
        lag_txt = "/".join(f"{lag_map[k]}" for k in ("03-31", "06-30", "09-30", "12-31"))
        L.append(f"| {label} | {lag_txt} | {diff} | {pct:.1f}% | {max_ds} |")
    L.append("")

    # ---- Q2：现行滞后是否为「保守下界」 ----
    L.append("## Q2 现行滞后相对法定最晚披露日的安全余量")
    L.append("")
    L.append("余量 = 假定生效日 − 法定最晚披露日。**余量 < 0 ⇒ 假定生效日早于法定最晚披露日**，")
    L.append("对「卡在法定时限才披露」的基金存在前视风险；余量 ≥ 0 才是保守下界。")
    L.append("")
    L.append("| 报告期 | 法定最晚披露日 | 依据 | 假定生效日 | 余量(天) |")
    L.append("|---|---|---|---|---|")
    neg = 0
    for p in periods[-10:]:                      # 近期 10 期足够说明口径
        dl, basis = legal_deadline(p, tdays)
        eff = lt._add_days(p, lt._QTR_LAG.get(p[5:], 95))
        if dl is None:
            L.append(f"| {p} | — | {basis} | {eff} | 无法判定 |")
            continue
        slack = (datetime.date.fromisoformat(eff) - datetime.date.fromisoformat(dl)).days
        if slack < 0:
            neg += 1
        mark = "" if slack >= 0 else " ←⚠ 前视"
        L.append(f"| {p} | {dl} | {basis} | {eff} | {slack}{mark} |")
    L.append("")
    L.append(f"**判定：近 10 期中余量 < 0 的有 {neg} 期**"
             + ("（现行口径在观察窗内不构成保守下界，需复核）" if neg
                else "（现行口径可视为法定时限下的保守下界）"))
    L.append("")

    # ---- Q3：敏感性 ----
    L.append("## Q3 滞后取值敏感性（样本选择分歧天数）")
    L.append("")
    keys = ("03-31", "06-30", "09-30", "12-31")
    L.append("| 滞后偏移 | " + " | ".join(keys) + " |")
    L.append("|---|" + "---|" * len(keys))
    for off in (-10, -5, 0, 5, 10):
        row = [f"{off:+d}d"]
        for k in keys:
            if off == 0:
                row.append("0（基准）")
                continue
            lag_map = dict(lt._QTR_LAG)
            lag_map[k] = lt._QTR_LAG[k] + off
            h = history_under(periods, lag_map)
            diff = sum(1 for day in days if pick(h, day) != base_pick[day])
            row.append(f"{diff} ({100.0 * diff / len(days):.1f}%)" if days else "—")
        L.append("| " + " | ".join(row) + " |")
    L.append("")

    # ---- 附：审计判级字面量规则模拟 ----
    L.append("## 附：审计 check_pit() 判级规则模拟（字面量匹配，非语义）")
    L.append("")
    L.append("规则：命中 `_QTR_LAG =` 且无「公告日/披露日/ann_date」→ FAIL；两者都有 → WARN；")
    L.append("两者都无 → PASS。PASS 条目标题为「PIT 使用真实公告日（非固定滞后）」。")
    L.append("")
    src = (BASE / "core" / "lookthrough.py").read_text(encoding="utf-8")
    import re as _re
    fixed = bool(_re.search(r"_QTR_LAG =", src))
    ann = bool(_re.search(r"ann_date|announce_date|公告日|披露日", src))
    verdict = "FAIL" if (fixed and not ann) else ("WARN" if (fixed and ann) else "PASS")
    L.append(f"- 当前源码：`_QTR_LAG =` 命中={fixed}，公告日字样命中={ann} → 判级 **{verdict}**")
    L.append("- ⚠ 语义提示：若仅把常量改名以避开字面量匹配，审计会判 PASS，")
    L.append("  但该 PASS 的标题声称「使用真实公告日」——**与实际口径不符**，属语义失真，本脚本不建议。")
    L.append("")
    L.append("> 本文件仅记录数据层口径分析；不含任何信号/持仓/建议措辞，不构成投资建议。")

    print("\n".join(L))

    if not args.no_write:
        OUT_MD.parent.mkdir(parents=True, exist_ok=True)
        OUT_MD.write_text("\n".join(L) + "\n", encoding="utf-8")
        print(f"\n[written] {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
