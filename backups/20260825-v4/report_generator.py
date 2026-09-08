"""报告生成：三时点 Markdown 报告（弱参考措辞，恒不出现买卖指令）。

- pre  (08:30) 盘前·今日决策：昨日净值 + 三因子弱参考 + 申赎状态 + 15:00 截止提醒
- mid  (14:30) 盘中·执行窗口：距截止倒计时 + 当前参考（净值仍是昨日口径）
- post (15:30) 盘后·次日评估：复盘 + 账户面 + 次日净值/市值区间预估 + 异常波动告警
"""
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STANCE_ICON = {"偏多": "🔴偏多", "偏空": "🟢偏空", "中性": "⚪中性"}


def _fund_table(signals: dict) -> str:
    rows = ["| 代码 | 名称 | 净值(日期) | 三因子 | 参考 | 申购 | 赎回 |",
            "|---|---|---|---:|---|---|---|"]
    for code, s in signals.items():
        icon = STANCE_ICON[s["stance"]]
        note = "（历史不足）" if s["insufficient_history"] else ""
        rows.append(f"| {code} | {s['name']} | {s['last_nav']:.4f} ({s['last_nav_date']}) "
                    f"| {s['score']:+d} | {icon}{note} | {s['purchase_status']} | {s['redeem_status']} |")
    return "\n".join(rows)


def _data_source_notice(signals: dict) -> str:
    """数据来源透明度提示（融合版增强：吸收 quant_test 母本的降级告警链路）。

    _source 口径（core/data_loader.py）：
    - fresh          本次运行抓取成功 → 不提示
    - cache          TTL 内缓存复用 → 轻提示（净值日期可见）
    - cache:fallback 接口失败降级读缓存 → ⚠️ 显式告警
    """
    warns, notes = [], []
    for code, s in signals.items():
        src = s.get("_source", "fresh")
        if src.startswith("cache:fallback"):
            warns.append(f"{code}（净值截至 {s['last_nav_date']}）")
        elif src == "cache":
            notes.append(f"{code}（截至 {s['last_nav_date']}）")
    lines = []
    if warns:
        lines.append(f"> ⚠️ **数据降级告警**：{'、'.join(warns)} 本次接口抓取失败，"
                     "已使用本地缓存，净值可能非最新，结论仅供参考。")
    if notes:
        lines.append(f"<sub>本地缓存复用（TTL 内未重新抓取）：{'、'.join(notes)}。</sub>")
    return "\n".join(lines)


def _factor_details(signals: dict) -> str:
    lines = []
    for code, s in signals.items():
        lines.append(f"- **{code} {s['name']}**（{s['wording']}）")
        for k, v in s["factors"].items():
            if isinstance(v, dict):
                lines.append(f"  - {k}: {v['score']:+d}（{v['detail']}）")
            elif isinstance(v, str):
                lines.append(f"  - {k}: {v}")
    return "\n".join(lines) if lines else "（无）"


def _account_section(account: dict) -> str:
    if not account["positions"]:
        return "_（holdings.json 无持仓记录）_"
    lines = [f"**账户合计**：市值 {account['total_market_value']:,.2f} 元 · "
             f"成本 {account['total_cost']:,.2f} 元 · 浮盈 {account['total_pnl_pct']:+.2f}%",
             "", "| 代码 | 名称 | 市值 | 浮盈 | 昨日涨跌 | μ20(%) | 次日净值区间 | 次日市值区间 |",
             "|---|---|---:|---:|---:|---:|---|---|"]
    for p in account["positions"]:
        flag = " ⚠️异常波动" if p["abnormal"] else ""
        lines.append(f"| {p['code']} | {p['name']}{flag} | {p['market_value']:,.2f} | "
                     f"{p['pnl_pct']:+.2f}% | {p['last_change_pct']:+.2f}% | "
                     f"{p['daily_mean']:+.3f}% | "
                     f"{p['next_day_range'][0]:.4f} ~ {p['next_day_range'][1]:.4f} | "
                     f"{p['next_day_mv_range'][0]:,.0f} ~ {p['next_day_mv_range'][1]:,.0f} |")
    lines += ["", "<sub>次日区间 = 净值 × (1 + μ₂₀ ± 1.5·σ₂₀)。μ₂₀ 为近 20 日日收益均值（融合版并入的均值项，"
              "μ≠0 时区间中心相对最新净值偏移），σ₂₀ 为近 20 日日收益标准差。</sub>"]
    return "\n".join(lines)


_CL_EVENT = {"1buy": "一买", "2buy": "二买", "3buy": "三买",
             "1sell": "一卖", "2sell": "二卖", "3sell": "三卖"}


def _lookthrough_section(lookthrough: dict | None) -> str:
    """重仓股结构观察（穿透）。口径诚实命名：吻体系/MACD动力学背驰近似/缠论形态学事件。"""
    if not lookthrough:
        return "_（穿透数据不可用，本栏跳过）_"
    lines = ["| 基金 | 持仓截至 | 覆盖 | 吻结构 | MACD背驰(底/顶/净) | 距60日高 | 缠论事件(30日) | 综合 |",
             "|---|---|---:|---:|---|---:|---|---|"]
    alerts = []
    for code, a in lookthrough.items():
        rows = a["rows"]
        bull_div = sum(1 for r in rows if r.get("div") == 1)
        bear_div = sum(1 for r in rows if r.get("div") == -1)
        comp = {"1": "🔴偏多", "-1": "🟢偏空", "0": "⚪中性"}[str(a["composite"])]
        ev: dict[str, int] = {}
        for e in a.get("chanlun_events", []):
            ev[_CL_EVENT.get(e["type"], e["type"])] = ev.get(_CL_EVENT.get(e["type"], e["type"]), 0) + 1
        ev_txt = "/".join(f"{k}×{v}" for k, v in ev.items()) if ev else "—"
        lines.append(f"| {code} | {a['snapshot_date']} | {a['coverage']*100:.0f}% | "
                     f"{a['structure']:+.2f} | {bull_div}/{bear_div}/{a['div_net']:+.2f} | "
                     f"{a['dd_avg']*100:+.1f}% | {ev_txt} | {comp} |")
        if a.get("fanglang_alert"):
            alerts.append(f"{code}（{a['fanglang_ratio']*100:.0f}% 权重黄白线 0 轴下）")
    lines += ["", "<sub>吻结构=前十大按披露权重聚合的 MA5/20/60 多空排列（缠论前传·吻体系，或然性辅助）；"
              "MACD背驰=面积÷时间力度 + 黄白线回 0 轴过滤的缠论背驰**近似**（动力学口径）；"
              "缠论事件=core/chanlun.py 全量形态学（包含→分型→笔→线段→中枢→走势→买卖点）在近 30 日确认的一~三类买卖点。"
              "持仓为季报快照（滞后 1~3 个月），穿透视图仅供观察，不进入打分引擎。</sub>"]
    if alerts:
        lines += ["", f"⚠️ **防狼术告警（课103·黄白线 0 轴下）**：{('、'.join(alerts))}——按纪律应彻底离场观察。"]
    lines += ["", "> ⚠️ **常驻警示（证据已裁决）**：本栏穿透/事件信号存在**时段依赖**——2020-04~2023-11 区间"
              "+1 超额为 -1.53%（无证据），仅 2023-11 后有效（+3.40%）。据此已退出打分引擎，"
              "此处仅供结构观察，**勿单独作为操作依据**。"]
    return "\n".join(lines)


def _rotation_section(rot: dict | None) -> str:
    """池内轮动参考（横截面·弱参考，不构成调仓指令）。"""
    if not rot:
        return "_（轮动数据不可用）_"
    lines = [f"净值截至 {rot['as_of']}，横截面综合分 = 20日收益秩 + MACD柱秩", "",
             "| 排名 | 基金 | 名称 | 20日收益 | MACD柱 | 综合分 |", "|---:|---|---|---:|---:|---:|"]
    for i, r in enumerate(rot["ranking"], 1):
        lines.append(f"| {i} | {r['code']} | {r['name']} | {r['r20']*100:+.2f}% | "
                     f"{r['macd_hist']:+.4f} | {r['score']} |")
    lines += ["", "<sub>横截面因子本身有信息（重建版 2016-2026 复验：MACD柱/20日收益/距60日高的横截面 IC 均显著，"
              "t=+2.99~+3.96），**但 top1 轮动超额未复现**（-0.07%，t=-0.64；原笔记 +0.41%/t=3.44 系"
              "3只池·2023-2026 短样本口径）。本栏仅作相对强弱观察，不构成任何轮动/调仓依据。</sub>"]
    return "\n".join(lines)


def _realtime_section(realtime: dict | None) -> str:
    """重仓股实时行情 + 基金当日估算涨跌（前十大披露权重加权）。"""
    if not realtime:
        return ""
    lines = ["## 📡 重仓股实时行情（估算）", ""]
    for code, r in realtime.items():
        est = r["est_change_pct"]
        icon = "🔴" if est > 0 else ("🟢" if est < 0 else "⚪")
        lines.append(f"**{code} 当日估算 {icon} {est:+.2f}%**（前十大加权，覆盖 {r['covered_pct']:.0f}%）")
        lines.append("")
        lines.append("| 股票 | 代码 | 比例 | 现价 | 涨跌 |")
        lines.append("|---|---|---:|---:|---:|")
        for code_, q in sorted(r["quotes"].items(), key=lambda kv: -kv[1].get("pct", 0)):
            lines.append(f"| {q['name']} | {code_} | {q.get('pct', 0):.2f}% | "
                         f"{q['price']:.2f} | {q['change_pct']:+.2f}% |")
        lines.append("")
    lines += ["<sub>估算=最新季报前十大持仓按披露权重加权，持仓滞后 1~3 个月；"
              "场外基金当日净值约 20:00 后公布，实时估算仅供参考。</sub>", ""]
    return "\n".join(lines)


def generate_report(slot: str, signals: dict, account: dict, lookthrough: dict | None = None,
                    rotation: dict | None = None, realtime: dict | None = None) -> str:
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    header = {"mid": "⏰ 午盘 · 实时参考", "post": "🌙 收盘前 · 最终参考"}.get(
        slot, "☀️ 盘前 · 今日决策")

    parts = [f"# 基金日频参谋 · {header}", "",
             f"> 生成时间：{now.strftime('%Y-%m-%d %H:%M')} · 风险边界：**只出参考建议，绝不自动下单**", ""]

    parts += [f"## 📊 基金池弱参考（净值截至 {account['as_of_nav_date']}）", "", _fund_table(signals), ""]
    rt_sec = _realtime_section(realtime)
    if rt_sec:
        parts += [rt_sec]
    parts += ["## 🔍 因子分解", "", _factor_details(signals), ""]
    notice = _data_source_notice(signals)
    if notice:
        parts += [notice, ""]
    parts += ["## 🔎 重仓股结构观察（穿透）", "", _lookthrough_section(lookthrough), ""]
    parts += ["## 🔁 池内轮动参考（横截面）", "", _rotation_section(rotation), ""]

    if slot == "pre":
        parts += ["## 📌 今日操作参考", "",
                  "- 场外基金 **T+1**，当日 15:00 前提交的申赎按当日净值成交，此后按次日",
                  "- 如有申赎计划：请在 **15:00 前** 完成（盘中 14:30 会再提醒一次）",
                  "- 以上弱参考仅为观察描述，不构成买卖指令", ""]
    elif slot == "mid":
        parts += ["## ⏰ 执行窗口提醒", "",
                  f"- 当前时间 {now.strftime('%H:%M')}，距 **15:00 申赎截止** 不足 {max(0, (15 - now.hour) * 60 + (0 - now.minute))} 分钟",
                  "- 场外基金按提交时间是否过 15:00 划分当日/次日净值",
                  "- 弱参考不变（场外基金日内无新净值），如需行动请回到盘前结论", ""]
    else:
        alerts = account["abnormal_alerts"]
        alert_line = (f"- ⚠️ **异常波动告警**：{', '.join(alerts)} 单日涨跌超过 2σ，请关注相关市场消息"
                      if alerts else "- 无异常波动告警")
        parts += ["## 💰 账户面与次日预估", "", _account_section(account), "",
                  "## 🔔 告警与复盘", "", alert_line,
                  "- 明日 08:30 盘前报告将更新最新净值与弱参考", ""]

    parts += ["---", "", "*基金日频参谋 v3（融合版）· 三因子弱参考 · 不构成投资建议*"]
    report = "\n".join(parts)

    out_dir = BASE_DIR / "output"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"report_{now.strftime('%Y%m%d')}_{slot}.md"
    path.write_text(report, encoding="utf-8")
    return report
