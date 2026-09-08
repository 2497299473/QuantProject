#!/usr/bin/env python3
"""基金日频参谋 v2 · 主入口。

用法：
    python3 run.py                 # 自动按时钟判断时点
    python3 run.py --slot pre      # 盘前 08:30 / mid 盘中 14:30 / post 盘后 15:30
    python3 run.py --slot post --force     # 非交易日也强制执行
    python3 run.py --slot post --no-push   # 只落盘报告，不推送飞书
    python3 run.py --no-lookthrough        # 跳过重仓股穿透（省网络请求）
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from core import data_loader, signal_engine, account as account_mod
from core import report_generator, notify, lookthrough as lookthrough_mod, rotation as rotation_mod
from core import real_time as realtime_mod

_LOG: list[str] = []


def log(msg: str) -> None:
    """控制台输出 + 追加到运行日志（output/logs/）。"""
    print(msg)
    _LOG.append(f"{datetime.now():%H:%M:%S} {msg}")


def _write_log(now: datetime) -> None:
    log_dir = BASE_DIR / "output" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"run_{now:%Y%m%d_%H%M%S}.log").write_text("\n".join(_LOG), encoding="utf-8")
    logs = sorted(log_dir.glob("run_*.log"))
    for old in logs[:-180]:          # 只保留最近 180 次运行
        old.unlink(missing_ok=True)


def load_holidays() -> set[str]:
    """读 data/holidays.json → {"2026-01-01", ...}（仅工作日休市日，周末由代码过滤）。"""
    data = json.loads((BASE_DIR / "data" / "holidays.json").read_text(encoding="utf-8"))
    days: set[str] = set()
    for year in data.get("years", {}).values():
        for dates in year.values():
            days.update(dates)
    return days


def calendar_years() -> set[str]:
    """日历已覆盖的年份集合（防呆：未覆盖年份的节假日会被误当交易日）。"""
    data = json.loads((BASE_DIR / "data" / "holidays.json").read_text(encoding="utf-8"))
    return set(data.get("years", {}).keys())


HOLIDAYS = load_holidays()


def is_trading_day(d: datetime | None = None) -> bool:
    """周一~五 且 非法定休市日。每年 12 月更新 holidays.json 即可，本函数零改动。"""
    d = d or datetime.now()
    if d.weekday() >= 5:
        return False
    return d.strftime("%Y-%m-%d") not in HOLIDAYS


def auto_slot(now: datetime | None = None) -> str:
    """按本地时钟自动选时点：14:55 前为 mid（午盘），14:55 起为 post（收盘前）。"""
    now = now or datetime.now()
    return "post" if (now.hour, now.minute) >= (14, 55) else "mid"


OBSIDIAN_LOG = Path("/mnt/d/Obsidian/My-First-Obsidian/量化交易工具/基金日频参谋-信号流水-2026.md")


def append_obsidian_log(slot: str, signals: dict, account: dict, now,
                        path: Path = OBSIDIAN_LOG) -> bool:
    """盘后把当日信号追加一行到 Obsidian 信号流水（只记盘后、只记交易日）。

    设计（2026-08-23 用户确认）：盘前/盘中是过程态，盘后才是当日定论 → 一天 1 行。
    文件不存在时自动创建并写表头；已存在则只追加，不覆盖历史。
    """
    if slot != "post" or not is_trading_day(now):
        return False
    icon = report_generator.STANCE_ICON
    cells = []
    for code, s in signals.items():
        note = "（历史不足）" if s.get("insufficient_history") else ""
        cells.append(f"{icon[s['stance']]}{s['score']:+d}{note}")
    acct = f"浮盈 {account['total_pnl_pct']:+.2f}%" if account.get("positions") else "无持仓"
    row = f"| {now:%m-%d} 盘后 | " + " | ".join(cells) + f" | {acct} |\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        head = ("# 基金日频参谋 · 盘后信号流水（2026）\n\n"
                "> 只记盘后当日定论；🔴偏多 🟢偏空 ⚪中性；分数为三因子总分；不构成投资建议。\n\n"
                "| 日期 | " + " | ".join(str(c) for c in signals) + " | 账户面 |\n"
                "|:---" + "|---:" * (len(signals) + 1) + "|\n")
        path.write_text(head + row, encoding="utf-8")
    else:
        with open(path, "a", encoding="utf-8") as fp:
            fp.write(row)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="基金日频参谋 v3（融合版）")
    parser.add_argument("--slot", choices=["pre", "mid", "post"], help="时点（缺省按时钟自动判断）")
    parser.add_argument("--force", action="store_true", help="非交易日也强制执行")
    parser.add_argument("--no-push", action="store_true", help="不推送飞书，只落盘报告")
    parser.add_argument("--refresh", action="store_true", help="忽略净值缓存强制重新抓取")
    parser.add_argument("--no-lookthrough", action="store_true", help="跳过重仓股穿透观察（省网络请求）")
    args = parser.parse_args()

    now = datetime.now()
    try:
        return _run(args, now)
    finally:
        _write_log(now)


def _run(args, now: datetime) -> int:
    # 节假日日历防呆：未覆盖当前年份时显式告警（仍继续执行，周末过滤不受影响）
    if str(now.year) not in calendar_years():
        log(f"[warn] ⚠️ 节假日日历未覆盖 {now.year} 年！法定休市日将被误当交易日执行。")
        log("[warn]    每年 12 月下旬交易所公布次年休市安排后，请更新 data/holidays.json")

    if not args.force and not is_trading_day(now):
        log(f"[skip] {now:%Y-%m-%d} 非交易日（周末或法定休市日），跳过。如需强制执行加 --force")
        return 0

    slot = args.slot or auto_slot(now)
    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    log(f"[run ] slot={slot} 基金池={cfg['fund_pool']} 风险边界：{cfg['risk_boundary']}")

    funds = {}
    for code in cfg["fund_pool"]:
        try:
            funds[code] = data_loader.load_fund(code, force_refresh=args.refresh)
            log(f"[data] {code} {funds[code]['name']} 净值 {len(funds[code]['navs'])} 条，"
                f"最新 {funds[code]['navs'][-1]}，申购={funds[code].get('purchase_status')}")
        except Exception as e:
            log(f"[warn] {code} 数据获取失败：{e}")

    if not funds:
        log("[fail] 无任何基金数据，退出")
        return 1

    lookthrough = None
    if not args.no_lookthrough:
        log("[look] 拉取重仓股穿透数据（季报持仓 + 个股K线）...")
        try:
            lookthrough = lookthrough_mod.evaluate_lookthrough(cfg["fund_pool"])
            for code, a in lookthrough.items():
                log(f"[look] {code} 持仓截至 {a['snapshot_date']} 覆盖 {a['coverage']*100:.0f}% "
                    f"吻结构 {a['structure']:+.2f} 净背驰 {a['div_net']:+.2f} 综合 {a['composite']:+d}"
                    + (" ⚠️防狼术" if a.get("fanglang_alert") else ""))
        except Exception as e:
            log(f"[warn] 穿透数据不可用，报告将跳过该栏：{e}")

    realtime: dict[str, dict] = {}
    if lookthrough:
        for code, a in lookthrough.items():
            try:
                rt = realtime_mod.fetch_realtime(a["rows"])
                est = realtime_mod.weighted_estimate(a["rows"], rt)
                if est["est_change_pct"] is not None:
                    realtime[code] = {"quotes": rt, **est}
                    log(f"[rt  ] {code} 实时估算 {est['est_change_pct']:+.2f}%"
                        f"（前十大覆盖 {est['covered_pct']:.0f}%）")
            except Exception as e:
                log(f"[warn] {code} 实时行情不可用：{e}")

    signals = signal_engine.compute_signals(funds, lookthrough)
    for code, s in signals.items():
        log(f"[sig ] {code} {s['name']} 总分 {s['score']:+d} → {s['stance']}")

    rot = rotation_mod.evaluate_rotation(funds)
    if rot:
        log(f"[rot ] 池内轮动参考：{rot['ranking'][0]['code']} {rot['ranking'][0]['name']} "
            f"居首（{rot['ranking'][0]['score']:+d}）")

    account = account_mod.evaluate_account(signals)
    log(f"[acct] 市值 {account['total_market_value']:,.2f} 元，浮盈 {account['total_pnl_pct']:+.2f}%"
        + (f"，异常告警：{account['abnormal_alerts']}" if account["abnormal_alerts"] else ""))

    report = report_generator.generate_report(slot, signals, account, lookthrough, rot, realtime)
    log(f"[repo] 报告已生成 → output/report_{now:%Y%m%d}_{slot}.md")

    if append_obsidian_log(slot, signals, account, now):
        log(f"[obs ] 盘后信号已追加 → Obsidian 信号流水")

    if args.no_push:
        log("[push] --no-push 指定，跳过飞书推送")
    else:
        result = notify.push_feishu(slot, signals, account, realtime)
        if result.get("ok"):
            log("[push] 飞书推送成功")
        else:
            log(f"[push] 飞书推送未成功（{result.get('reason') or result.get('error') or result.get('response')}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
