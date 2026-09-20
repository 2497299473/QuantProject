#!/usr/bin/env python3
"""方向差细分回测：验证「加 vs 减」组合决策的稳定性是否够格当弱参考。

背景（2026-08-25）：组合回测 backtest_fusion.py 跑出 加仓+0.15% / 减仓-0.27%（差 0.42%），
但时间分段 D 失败（前半翻转）。本脚本深挖：
1. 后半段（≈2023-09 后）方向差是否跨年稳定？
2. 按基金拆分——是否只在部分基金上有效？
3. 多预测窗口（5/10/20 日）一致性
4. 统计显著性：bootstrap 置信区间
5. 规则变体：去掉缠论（已知 B 失败）后是否更稳？加权评分 vs AND 逻辑？

判定标准（事先写死）：
- E. 后半段方向差>0 且每年同向（允许 1 年例外）
- F. ≥2 只基金单独方向差>0
- G. bootstrap 95% CI 下界>0
- H. 去缠论变体方向差 ≥ 原版（缠论无增量贡献则去掉更简洁）
E+F+G 全过 → 够格当弱参考（措辞偏加/不动/偏减）；H 决定是否保留缠论。

用法：python3 backtest_spread.py [--snapshot PATH | --fresh]

V4.3 P0-1（2026-09-20）：样本入口统一走 frozen_dataset.resolve_samples ——
默认自动选最新冻结件（G-A 硬 / G-B 软），无冻结件且非 --fresh ⇒ exit 4
fail-closed；--fresh 显式活拉（报告标 FRESH，数字与冻结基线不可比）。
"""
import json
import random
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from core import data_loader, lookthrough, stock_data
from core.signal_engine import macd_hist, factor_pool_rank_20d
from frozen_dataset import resolve_samples

FWD_LIST = (5, 10, 20)
# v5 多周期预测补充标签：fwd1/2/3 是 forecast_engine 的 T+1/T+3/T+5 训练标签
# （数据都在 navs 里，仅需额外逐日计算；诊断确认样本量后再决定是否纳入 load_samples 保留条件）
EXTRA_FWD = (1, 2, 3)
SPLIT_DATE = "2023-09-21"  # 与 backtest_fusion 一致


def _nav_state_at(i: int, navs: list, r20_win: int, dd_win: int) -> dict:
    """PIT 净值系因子：任意时点只能用已公布净值。

    泄漏背景（2026-08-27 审计修正）：此前 rows[d] 直接用 navs[i]——但用户在 14:55
    做决策时，d 日净值尚未公布（约当日 21~22 点才出）。MACD 用 navs[i] 作 EMA 输入、
    回撒用 navs[i] 当分母/高点，等于让模型看到未来；pool_rank_20d 同理。全部改为截至 i-1。

    - macd_hist：截至 i-1 的净值的 MACD 直方图（hist_full[i-1]，因果序列本身无前视）
    - r20：navs[i-1]/navs[i-1-r20_win]（原为 /navs[i-r20_win] 并含 navs[i]）
    - dd：navs[i-1] 相对含自身的前 dd_win 已公布净值窗口最高点的回撒
    """
    k = i - 1                      # 最后一个已公布净值下标
    if k < r20_win or k < dd_win:
        return {"macd": None, "r20": None, "dd": None}
    prev_close = float(navs[k][1])
    denom = float(navs[k - r20_win][1])
    r20 = prev_close / denom - 1 if denom > 0 else None
    hh = max(float(v) for _, v in navs[k - dd_win + 1:k + 1])
    dd = prev_close / hh - 1 if hh > 0 else None
    return {"macd": k, "r20": r20, "dd": dd}


def fetch_stock_klines(stock_universe: dict, lt_cfg: dict) -> tuple[dict, dict, list[dict]]:
    """拉取全部个股K线（含缓存）→ (series_map, close_map, failures)。

    V4.3 P0-3 失败契约（2026-09-20，GPT 评审 P0-3，逐行核实）：旧实现
    `except Exception: pass` 静默跳过——不完整样本被当成完整样本冻结，
    covered_pct 靠不完整数据算，事后无迹可查。现每次失败都记入
    {code, market, error}（fetch_stock_kline 的 ValueError 已逐源列因）
    并返回给调用方做门禁：

    - freeze_samples.py：任一失败 ⇒ 快照 SNAPSHOT_INVALID（隔离，不发布 canonical）；
    - 回测/报告层：打印失败清单（数据质量门禁），报告必须记录。

    成功路径行为零变更（series_map/close_map 口径与旧实现逐字一致）。
    """
    series_map, close_map, failures = {}, {}, []
    for i, (scode, mkt) in enumerate(sorted(stock_universe.items()), 1):
        try:
            k = stock_data.fetch_stock_kline(scode, mkt)
            series_map[scode] = lookthrough.stock_signal_series(k["klines"], lt_cfg)
            dates = [r[0] for r in k["klines"]]
            closes = [float(r[2]) for r in k["klines"]]
            close_map[scode] = (dates, closes)
        except Exception as exc:  # noqa: BLE001
            failures.append({"code": scode, "market": mkt,
                             "error": f"{type(exc).__name__}: {exc}"})
        if i % 40 == 0:
            print(f"  进度 {i}/{len(stock_universe)}")
    return series_map, close_map, failures


def load_samples(return_universe: bool = False):
    """复用 backtest_fusion 的数据加载逻辑，返回逐日样本。

    V4.3 P0-1/P0-3（2026-09-20）：return_universe=True →
    (samples, stock_universe, stock_failures)，供 freeze_samples.py 的
    as-of KFP universe 口径与数据质量门禁。既有调用方（无参）行为不变。
    """
    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    fcfg = cfg["signal"]["factors"]
    lt_cfg = cfg["lookthrough"]
    wcfg = cfg["signal"]["weak_signal"]
    funds = cfg["fund_pool"]

    print("== [1] 拉取基金净值 + 历史持仓 ==")
    fund_data, stock_universe = {}, {}
    for code in funds:
        fund = data_loader.load_fund(code)
        history = lookthrough.holdings_history(code)
        for snap in history:
            for h in snap["holdings"]:
                stock_universe[h["code"]] = h["market"]
        fund_data[code] = (fund, history)
        print(f"  {code}: 净值 {len(fund['navs'])} 条，持仓快照 {len(history)} 期")

    print(f"== [2] 拉取 {len(stock_universe)} 只个股K线（含缓存；V4.3 失败契约：不静默跳过）==")
    series_map, close_map, stock_failures = fetch_stock_klines(stock_universe, lt_cfg)
    if stock_failures:
        print(f"  [数据质量门禁] {len(stock_failures)} 只个股K线拉取失败（已记录；冻结将因这些码 INVALID）：")
        for f_ in stock_failures:
            print(f"    ! {f_['code']} ({f_['market']}): {f_['error'][:160]}")

    print("== [3] 构建三因子 + 逐日重放（PIT 口径：特征只用 ≤14:55 可得信息）==")
    dd_cfg = fcfg["drawdown_from_60d_high"]
    per_fund_rows = {}
    for code, (fund, _h) in fund_data.items():
        navs = fund["navs"]
        hist_full = macd_hist([v for _, v in navs], fcfg["macd_hist_trend"]["ema_fast"],
                              fcfg["macd_hist_trend"]["ema_slow"], fcfg["macd_hist_trend"]["dea"])
        rows = {}
        for i in range(len(navs)):
            d, nav = navs[i]
            if i + 1 < wcfg["min_history"]:
                continue                  # 与原口径一致：历史不足不生成样本行
            st = _nav_state_at(i, navs,
                               fcfg["pool_rank_20d"]["window"],
                               dd_cfg["window"])
            if st["macd"] is not None:
                st["macd"] = hist_full[st["macd"]]
            rows[d] = st
        per_fund_rows[code] = rows

    samples = []
    for code, (fund, history) in fund_data.items():
        navs = fund["navs"]
        for i in range(len(navs)):
            d = navs[i][0]
            if d not in per_fund_rows[code]:
                continue
            snap = lookthrough.effective_snapshot(history, d)
            if not snap:
                continue
            agg = lookthrough.aggregate(snap, series_map, d, lt_cfg)
            if not agg:
                continue

            est_chg, wsum = 0.0, 0.0
            up_w = down_w = 0.0
            contribs = []
            for h in snap["holdings"]:
                cm = close_map.get(h["code"])
                if not cm:
                    continue
                dates, closes = cm
                j = bisect_right(dates, d) - 1
                if j <= 0 or closes[j] <= 0:
                    continue
                chg = closes[j] / closes[j - 1] - 1 if closes[j - 1] > 0 else 0.0
                est_chg += h["pct"] * chg
                wsum += h["pct"]
                contribs.append(h["pct"] * chg)
                if chg > 0:
                    up_w += h["pct"]
                elif chg < 0:
                    down_w += h["pct"]
            if wsum <= 0:
                continue
            est_chg /= wsum
            breadth = (up_w - down_w) / (up_w + down_w) if (up_w + down_w) > 0 else None
            abs_sum = sum(abs(c) for c in contribs)
            concentration = max(abs(c) for c in contribs) / abs_sum if abs_sum > 1e-9 else None

            # PIT：est_chg 用 d 日收盘（14:55 实时估算的有界近似，仅差尾盘漂移）；
            # NAV 系因子在 _nav_state_at 已统一错位至 T-1 公布口径
            r = per_fund_rows[code][d]
            f1 = 1 if (r["macd"] or 0) > 0 else (-1 if (r["macd"] or 0) < 0 else 0)
            pool_returns = {c: rows[d]["r20"] for c, rows in per_fund_rows.items()
                            if d in rows and rows[d]["r20"] is not None}
            f2 = factor_pool_rank_20d(pool_returns, code, fcfg["pool_rank_20d"])["score"] \
                if code in pool_returns else 0
            f3 = (1 if r["dd"] <= dd_cfg["pullback_threshold"]
                  else (-1 if r["dd"] >= dd_cfg["near_high_threshold"] else 0)) if r["dd"] is not None else 0
            score = f1 + f2 + f3

            row = {"fund": code, "date": d, "est_chg": est_chg,
                   "composite": agg["composite"], "score": score,
                   "est_sign": 1 if est_chg > 0 else (-1 if est_chg < 0 else 0),
                   "breadth": breadth, "concentration": concentration,
                   "covered_pct": wsum}
            # 注（v5.2 实验已回退，2026-08-26）：曾加 rel_str20（基金20日收益−沪深300 20日收益，
            # 申万行业免费源探测失败退化的市场级基准），OOS T+1 从 +0.021 翻负至 -0.005、
            # T+3 恶化至 -0.042、T+5 减半至 +0.044 → 判定过拟合，已撤销。详见
            # output/v5_forecast_relstr.txt。
            # 注（v5.1 实验已回退，2026-08-26）：曾给样本补 mom1/mom5/mom20/vol20/dd60
            # 动量/波动特征想让 T+3 RankIC 转正，OOS 三周期全部恶化（T+1 -0.024 /
            # T+3 -0.028 / T+5 -0.100，详见 output/v5_forecast_diag_v51.txt）→ 判定为
            # 特征过拟合（vol20 排列重要性第一但 OOS 不成立），已撤销。fwd1/2/3 标签保留。
            for fwd in FWD_LIST:
                if i + fwd < len(navs):
                    row[f"fwd{fwd}"] = navs[i + fwd][1] / navs[i][1] - 1
            # v5 补充：T+1/T+3/T+5 训练标签（独立计算，不参与下方 all() 保留判定，
            # 避免短历史基金因 fwd20 缺失被连坐丢失全部短周期样本）
            for fwd in EXTRA_FWD:
                if i + fwd < len(navs):
                    row[f"fwd{fwd}"] = navs[i + fwd][1] / navs[i][1] - 1
            # v7 P2：MDD/MFE 真标签（2026-08-29，GPT 三审）——历史 NAV 窗口内的
            # 真实最大回撤/最大有利波动（非模型估计）：
            #   mdd5 = 未来 5 日窗口内，任意时点 t 起的最大后续跌幅（≤0）
            #   mfe5 = 未来 5 日窗口内，任意时点 t 起的最大后续涨幅（≥0）
            # 口径含起点（d 日本身）；供 path_forecast 预测侧的真值对照（RankIC）。
            for fwd in (1, 3, 5):
                if i + fwd < len(navs):
                    window = [float(v) for _, v in navs[i:i + fwd + 1]]
                    mdd = 0.0
                    mfe = 0.0
                    for t in range(len(window)):
                        tail = window[t:]
                        mdd = min(mdd, min(tail) / window[t] - 1 if window[t] > 0 else 0.0)
                        mfe = max(mfe, max(tail) / window[t] - 1 if window[t] > 0 else 0.0)
                    row[f"mdd{fwd}"] = mdd
                    row[f"mfe{fwd}"] = mfe
            if all(f"fwd{f}" in row for f in FWD_LIST):
                samples.append(row)

    # 超额基准
    for c in funds:
        fwds = [s["fwd10"] for s in samples if s["fund"] == c]
        base = sum(fwds) / len(fwds) if fwds else 0.0
        for s in samples:
            if s["fund"] == c:
                s["excess10"] = s["fwd10"] - base
    print(f"  总样本: {len(samples)}")
    if return_universe:
        return samples, stock_universe, stock_failures
    return samples


def decision_and(s: dict) -> str:
    """原版 AND 逻辑。"""
    if s["est_sign"] > 0 and s["composite"] >= 0 and s["score"] >= 0:
        return "加仓"
    if s["est_sign"] < 0 and s["composite"] <= 0 and s["score"] <= 0:
        return "减仓"
    return "不动"


def decision_no_chan(s: dict) -> str:
    """去缠论变体：est + score only。"""
    if s["est_sign"] > 0 and s["score"] >= 0:
        return "加仓"
    if s["est_sign"] < 0 and s["score"] <= 0:
        return "减仓"
    return "不动"


def decision_weighted(s: dict) -> str:
    """加权评分变体：est_sign*2 + composite + score_sign，top/bottom 三分位。"""
    sc = s["est_sign"] * 2 + s["composite"] + (1 if s["score"] > 0 else (-1 if s["score"] < 0 else 0))
    if sc >= 3:
        return "加仓"
    if sc <= -3:
        return "减仓"
    return "不动"


def spread(rows: list[dict], dec_fn, fwd_key="excess10") -> tuple[float, int, int] | None:
    """返回 (加仓超额 - 减仓超额, 加仓n, 减仓n)。"""
    adds = [s for s in rows if dec_fn(s) == "加仓"]
    cuts = [s for s in rows if dec_fn(s) == "减仓"]
    if not adds or not cuts:
        return None
    a = sum(s[fwd_key] for s in adds) / len(adds) * 100
    c = sum(s[fwd_key] for s in cuts) / len(cuts) * 100
    return (a - c, len(adds), len(cuts))


def bootstrap_spread(rows, dec_fn, n_boot=5000) -> tuple[float, float]:
    """bootstrap 95% CI 的方向差下界/上界。"""
    rng = random.Random(42)
    vals = []
    for _ in range(n_boot):
        boot = [rng.choice(rows) for _ in range(len(rows))]
        sp = spread(boot, dec_fn)
        if sp:
            vals.append(sp[0])
    if not vals:
        return (0.0, 0.0)
    vals.sort()
    lo = vals[int(len(vals) * 0.025)]
    hi = vals[int(len(vals) * 0.975)]
    return (lo, hi)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=None,
                    help="冻结样本 jsonl（默认自动选最新 forecast_outputs/samples_frozen_*.jsonl）")
    ap.add_argument("--fresh", action="store_true",
                    help="显式活拉样本（数字与冻结基线不可比；报告标 FRESH）")
    args = ap.parse_args()
    samples, snap_info = resolve_samples(args.snapshot, args.fresh, BASE_DIR, load_samples)
    if snap_info["mode"] in ("MISSING", "INVALID"):
        return 4
    if not samples:
        print("[fail] 无样本"); return 1

    post = [s for s in samples if s["date"] >= SPLIT_DATE]
    pre = [s for s in samples if s["date"] < SPLIT_DATE]
    funds = sorted({s["fund"] for s in samples})

    lines = [
        "# 方向差细分回测：加 vs 减 稳定性验证", "",
        snap_info["report_line"],
        f"> 生成：{datetime.now():%Y-%m-%d %H:%M} · 总样本 {len(samples)} · "
        f"后半段（≥{SPLIT_DATE}）{len(post)} 样本 · 基金 {len(funds)} 只", "",
        "## 〇、判定标准（事先写死）", "",
        "- **E** 后半段方向差>0 且每年同向（允许 1 年例外）",
        "- **F** ≥2 只基金单独方向差>0",
        "- **G** bootstrap 95% CI 下界>0",
        "- **H** 去缠论变体方向差 ≥ 原版（缠论无增量则去掉）",
        "",
        "## 一、后半段方向差（原版 AND 规则）", "",
    ]

    # ---- E: 后半段 + 逐年 ----
    sp_post = spread(post, decision_and)
    lines += ["| 区间 | 方向差(加-减) | 加仓n | 减仓n |", "|---|---:|---:|---:|"]
    if sp_post:
        lines.append(f"| 后半段整体 | {sp_post[0]:+.2f}% | {sp_post[1]} | {sp_post[2]} |")
    else:
        lines.append("| 后半段整体 | — | — | — |")

    by_year = defaultdict(list)
    for s in post:
        by_year[s["date"][:4]].append(s)
    lines += ["", "| 年份 | 方向差 | 加仓n | 减仓n |", "|---|---:|---:|---:|"]
    years_pos = 0
    for y in sorted(by_year):
        sp = spread(by_year[y], decision_and)
        if sp:
            lines.append(f"| {y} | {sp[0]:+.2f}% | {sp[1]} | {sp[2]} |")
            if sp[0] > 0:
                years_pos += 1
        else:
            lines.append(f"| {y} | 样本不足 | — | — |")
    e_pass = sp_post and sp_post[0] > 0 and years_pos >= len(by_year) - 1
    lines += ["", f"**判定 E**（后半段>0 且每年同向，允许1年例外）：{'✅' if e_pass else '❌'}"
              f"（{years_pos}/{len(by_year)} 年为正）", ""]

    # ---- F: 按基金拆分 ----
    lines += ["## 二、按基金拆分（后半段）", "",
              "| 基金 | 方向差 | 加仓n | 减仓n |", "|---|---:|---:|---:|"]
    funds_pos = 0
    for c in funds:
        fr = [s for s in post if s["fund"] == c]
        sp = spread(fr, decision_and)
        if sp:
            lines.append(f"| {c} | {sp[0]:+.2f}% | {sp[1]} | {sp[2]} |")
            if sp[0] > 0:
                funds_pos += 1
        else:
            lines.append(f"| {c} | 样本不足 | — | — |")
    f_pass = funds_pos >= 2
    lines += ["", f"**判定 F**（≥2 只基金方向差>0）：{'✅' if f_pass else '❌'}"
              f"（{funds_pos}/{len(funds)} 只为正）", ""]

    # ---- G: bootstrap ----
    print("== [4] bootstrap 5000 次（后半段）==")
    ci_lo, ci_hi = bootstrap_spread(post, decision_and)
    g_pass = ci_lo > 0
    lines += ["## 三、统计显著性（bootstrap 5000 次，后半段）", "",
              f"方向差 95% CI：[{ci_lo:+.2f}%, {ci_hi:+.2f}%]", "",
              f"**判定 G**（CI 下界>0）：{'✅' if g_pass else '❌'}", ""]

    # ---- H: 规则变体对比 ----
    lines += ["## 四、规则变体对比（后半段方向差）", "",
              "| 规则 | 方向差 | 加仓n | 减仓n | CI下界 |", "|---|---:|---:|---:|---:|"]
    for name, fn in [("原版AND(est+chanlun+score)", decision_and),
                     ("去缠论(est+score)", decision_no_chan),
                     ("加权评分", decision_weighted)]:
        sp = spread(post, fn)
        if sp:
            ci = bootstrap_spread(post, fn, n_boot=2000)
            lines.append(f"| {name} | {sp[0]:+.2f}% | {sp[1]} | {sp[2]} | {ci[0]:+.2f}% |")
        else:
            lines.append(f"| {name} | 样本不足 | — | — | — |")

    no_ch_sp = spread(post, decision_no_chan)
    h_pass = no_ch_sp and sp_post and no_ch_sp[0] >= sp_post[0] * 0.8
    lines += ["", f"**判定 H**（去缠论方向差 ≥ 原版80%）：{'✅' if h_pass else '❌'}"
              "（缠论{'无增量贡献，建议去掉' if h_pass else '有增量贡献，建议保留'}）", ""]

    # ---- 多窗口 ----
    lines += ["## 五、多预测窗口一致性（后半段，原版规则）", "",
              "| 窗口 | 方向差 | 加仓n | 减仓n |", "|---|---:|---:|---:|"]
    for fwd in FWD_LIST:
        # 重新算超额基准
        for c in funds:
            fwds = [s[f"fwd{fwd}"] for s in post if s["fund"] == c]
            base = sum(fwds) / len(fwds) if fwds else 0.0
            for s in post:
                if s["fund"] == c:
                    s[f"excess{fwd}"] = s[f"fwd{fwd}"] - base
        sp = spread(post, decision_and, fwd_key=f"excess{fwd}")
        if sp:
            lines.append(f"| 未来{fwd}日 | {sp[0]:+.2f}% | {sp[1]} | {sp[2]} |")
        else:
            lines.append(f"| 未来{fwd}日 | 样本不足 | — | — |")
    lines.append("")

    # ---- 结论 ----
    weak_pass = e_pass and f_pass and g_pass
    verdict = ("✅ 够格当弱参考 —— 可实现保守版「偏加/不动/偏减」参考档位"
               "（措辞弱化，不构成指令，需标注「仅后半段验证、前半段失效」）"
               if weak_pass else
               "❌ 不够格 —— 方向差在后半段不稳定或统计不显著，不输出操作档位")
    lines += ["## 六、结论", "", f"**{verdict}**", "",
              f"判定汇总：E={'✅' if e_pass else '❌'} F={'✅' if f_pass else '❌'} "
              f"G={'✅' if g_pass else '❌'} H={'✅' if h_pass else '❌'}（H仅决定是否保留缠论）", "",
              "<sub>局限：①11:30 午盘未回测；②持仓季报滞后 1~3 月；③缠论为吻结构+背驰近似；"
              "④bootstrap 用固定种子可复现；⑤前半段（2023-09 前）已知失效，弱参考仅覆盖后半段。</sub>"]

    report = "\n".join(lines)
    out = BASE_DIR / "output" / "backtest_spread_report.md"
    out.write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"\n[done] 报告 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
