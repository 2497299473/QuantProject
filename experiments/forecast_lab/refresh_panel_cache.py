#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D-lite 面板受控批量刷新（预注册 §五 之 1；2026-09-16 开跑窗口）。

做的事只有一件：把「候选 D 面板」17 个成员的本地缓存，刷到**最近一个已收盘交易日**，
此后一切读数零网络（面板生成器与 K 线指纹随后离线跑）。

面板成员（预注册 §一，17 = 4 基金 + 12 代理 + 1 板块指数）：
  · 4 基金      002112 / 002207 / 022853 / 025687  -> data/klines/        core.data_loader.load_fund
  · 9 闸门代理 + 3 relaxed 代理 = 12 ETF/LOF        -> data/stock_klines/  core.stock_data.fetch_stock_kline
  · 1 板块指数  BK0457                              -> data/sector_klines/ pull_sector_klines.py --scope prod

时间盒（预注册 R3 之三，写死不得临场放宽）：
  09:30 晨检之后开跑；**11:20 起不得再发任何东财新请求**。本脚本在每次发请求前检查
  时钟，越过 11:20 立即停手；未完成部分留给下一交易日 09:30 后受控补刷（当日不续跑）。

频控纪律（继承 §五 之 4 的 RATE_MARKERS 口径）：
  · 逐序列串行，序列之间 >= SLEEP_BETWEEN 秒（B 组限速 >=2s）；
  · 降级 / 频控征兆、或某序列全部数据源失败 -> 立即停手不再发新请求，退出码 1；
  · 单序列失败如实记录、绝不静默，并在报告里写明「已完成到哪、缺口是什么」。

可逆性（八荣八耻 · 分步迭代）：
  动手前对每个将被覆盖的缓存文件留内存快照。若刷新结果里出现了晚于
  「最近已收盘交易日」的 bar（= 盘中半成品，会污染冻结样本），当场还原该文件，
  并在报告中标 REVERTED_SAME_DAY —— 宁可少刷一天，不把半成品冻进研究面板。

用法：
  .\\.venv\\Scripts\\python.exe -X utf8 experiments\\forecast_lab\\refresh_panel_cache.py --dry-run
  .\\.venv\\Scripts\\python.exe -X utf8 experiments\\forecast_lab\\refresh_panel_cache.py
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

HOLIDAYS_JSON = BASE_DIR / "data" / "holidays.json"
STOCK_DIR = BASE_DIR / "data" / "stock_klines"
FUND_DIR = BASE_DIR / "data" / "klines"
SECTOR_DIR = BASE_DIR / "data" / "sector_klines"
PULL_SECTOR = BASE_DIR / "pull_sector_klines.py"
REPORT_MD = BASE_DIR / "output" / "ops_runs" / f"{date.today().isoformat()}-dlite-refresh.md"

FUNDS = ("002112", "002207", "022853", "025687")
GATE_PROXIES = {"512480": "1", "512880": "1", "159915": "0", "512660": "1",
                "510880": "1", "512800": "1", "160225": "0", "512010": "1",
                "501030": "1"}
RELAXED_PROXIES = {"159611": "0", "515220": "1", "159825": "0"}
SECTOR = "BK0457"

SLEEP_BETWEEN = 2.0          # B 组纪律：序列间隔 >=2s
CUTOFF = dtime(11, 20)       # R3 硬停线：11:20 起零东财新请求
CLOSE_TIME = dtime(15, 0)    # 今日 bar 在此之前一律视为未完成

RATE_MARKERS = ("DegradedResponse", "SUSPECT_DEGRADED", "PARSE_MISMATCH",
                "持仓拉取失败", "年持仓拉取失败")


# ---------------------------------------------------------------- 日历 / 目标日

def _holiday_set() -> set[str]:
    """自包含读 holidays.json（不 import run，避免副作用；沿用 evening 脚本口径）。"""
    try:
        data = json.loads(HOLIDAYS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return set()
    days: set[str] = set()
    for year in data.get("years", {}).values():
        for dates in year.values():
            days.update(dates)
    return days


def _is_trading_day(d: date, holidays: set[str]) -> bool:
    return d.weekday() < 5 and d.isoformat() not in holidays


def latest_closed_trading_day(now: datetime) -> str:
    """最近一个「已收盘」交易日：今天已过 15:00 且是交易日 -> 今天；否则往前找。"""
    holidays = _holiday_set()
    d = now.date()
    if not (now.time() >= CLOSE_TIME and _is_trading_day(d, holidays)):
        d -= timedelta(days=1)
    while not _is_trading_day(d, holidays):
        d -= timedelta(days=1)
    return d.isoformat()


# ---------------------------------------------------------------- 缓存读

def _last_date(path: Path, key: str) -> str | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rows = doc.get(key) or []
    return str(rows[-1][0]) if rows else None


# ---------------------------------------------------------------- 单序列刷新

def _refresh_fund(code: str) -> tuple[bool, str, bool]:
    """(ok, detail, degraded)。degraded=True 表示降级/频控征兆 -> 必须停手。"""
    from core.data_loader import load_fund
    fund = load_fund(code, force_refresh=True)
    src = str(fund.get("_source", ""))
    warn = ""
    if fund.get("_lsjz_error"):
        warn = " lsjz_warn=1"          # 申赎状态拿不到不阻断取数
    if src.startswith("cache:fallback"):
        return False, f"降级命中缓存 _source={src}{warn}", True
    return True, f"_source={src}{warn}", False


def _refresh_stock(code: str, market: str) -> tuple[bool, str, bool]:
    from core.stock_data import fetch_stock_kline
    out = fetch_stock_kline(code, market, ttl_hours=0.0)
    k = out.get("klines") or []
    if not k:
        return False, "空 K 线", True
    return True, f"src={out.get('source')} bars={len(k)}", False


def _refresh_sector() -> tuple[bool, str, bool]:
    """板块走既有脚本（唯一共享逻辑出处），不重写取数。"""
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(PULL_SECTOR), "--scope", "prod"],
        cwd=str(BASE_DIR), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=600)
    tail = (proc.stdout or "").strip().splitlines()
    tail_s = tail[-1] if tail else ""
    if proc.returncode != 0:
        return False, f"exit={proc.returncode} stderr={((proc.stderr or '').strip()[-200:])}", True
    return True, tail_s, False


# ---------------------------------------------------------------- main

def _write_report(lines: list[str]) -> None:
    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印计划：零网络、零写入")
    args = ap.parse_args(argv)

    now = datetime.now()
    target = latest_closed_trading_day(now)
    cutoff = datetime.combine(now.date(), CUTOFF)

    # (kind, code, market, path, json_key)
    members: list[tuple[str, str, str, Path, str]] = []
    for c in sorted(GATE_PROXIES):
        members.append(("gate", c, GATE_PROXIES[c], STOCK_DIR / f"{c}.json", "klines"))
    for c in sorted(RELAXED_PROXIES):
        members.append(("relaxed", c, RELAXED_PROXIES[c], STOCK_DIR / f"{c}.json", "klines"))
    for c in FUNDS:
        members.append(("fund", c, "", FUND_DIR / f"{c}.json", "navs"))
    members.append(("sector", SECTOR, "", SECTOR_DIR / f"{SECTOR}.json", "klines"))

    head = [
        f"# D-lite 面板受控批量刷新 · {now.isoformat(timespec='seconds')}",
        "",
        f"- 目标末根日期（最近已收盘交易日）：**{target}**",
        f"- 面板成员：{len(members)} 序列（gate {len(GATE_PROXIES)} + relaxed "
        f"{len(RELAXED_PROXIES)} + fund {len(FUNDS)} + sector 1）",
        f"- R3 硬停线：{cutoff.isoformat(timespec='minutes')}（此后零东财新请求）",
        f"- dry-run：{'是' if args.dry_run else '否'}",
        "",
        "| 层 | 代码 | 指令 | 缓存末根（前） | 动作 |",
        "|---|---|---|---|---|",
    ]
    plan = []
    for kind, code, mkt, path, key in members:
        cur = _last_date(path, key)
        fresh = cur is not None and cur >= target
        plan.append((kind, code, mkt, path, key, cur, fresh))
        head.append(f"| {kind} | {code} | "
                    f"{'market=' + mkt if mkt else ('fund' if kind == 'fund' else 'prod')} | "
                    f"{cur or '（缺）'} | {'跳过（已到目标）' if fresh else '刷新'} |")
    if args.dry_run:
        head += ["", f"（dry-run：将刷新 {sum(1 for p in plan if not p[6])} 个序列，跳过 "
                 f"{sum(1 for p in plan if p[6])} 个）", ""]
        print("\n".join(head))
        return 0

    snapshots: dict[Path, bytes] = {}
    for *_, path, _key, _cur, fresh in plan:
        if path.exists():
            snapshots[path] = path.read_bytes()

    results: list[str] = []
    rows: list[str] = []
    sent = 0
    stopped = False
    stop_reason = ""

    for kind, code, mkt, path, key, cur, fresh in plan:
        if fresh:
            rows.append(f"| {kind} | {code} | {cur} | SKIP | 已到目标日 |")
            continue
        if datetime.now() >= cutoff:
            stopped, stop_reason = True, f"越过 R3 硬停线 {CUTOFF.strftime('%H:%M')}"
            rows.append(f"| {kind} | {code} | {cur or '（缺）'} | TIMEOUT_STOP | {stop_reason} |")
            break
        t0 = time.time()
        try:
            if kind == "fund":
                ok, detail, deg = _refresh_fund(code)
            elif kind == "sector":
                ok, detail, deg = _refresh_sector()
            else:
                ok, detail, deg = _refresh_stock(code, mkt)
            sent += 1
        except Exception as e:                      # 全源失败 = 频控/网络征兆
            ok, detail, deg = False, f"{type(e).__name__}: {e}", True
        if any(m in detail for m in RATE_MARKERS):
            deg = True
        new = _last_date(path, key)
        action = "REFRESHED" if ok else "FAIL"
        if ok and new is not None and new > target:
            if path in snapshots:                   # 盘中半成品 -> 当场还原
                path.write_bytes(snapshots[path])
            new = _last_date(path, key)
            action, deg = "REVERTED_SAME_DAY", True
            detail += f" | 出现晚于目标日的 bar({new}) 已还原"
        rows.append(f"| {kind} | {code} | {cur or '（缺）'} -> {new or '（缺）'} | {action} | "
                    f"{detail} ({time.time() - t0:.1f}s) |")
        if not ok or deg:
            stopped = True
            stop_reason = detail
            break
        time.sleep(SLEEP_BETWEEN)

    done = sum(1 for r in rows if "REFRESHED" in r or "SKIP" in r)
    tail = [
        "", "## 执行结果", "",
        "| 层 | 代码 | 末根 前 -> 后 | 结果 | 说明 |", "|---|---|---|---|---|",
        *rows, "",
        f"- 发出请求的序列数：{sent}",
        f"- 完成（刷新或已到目标）：{done}/{len(members)}",
        f"- 是否停手：{'是 —— ' + stop_reason if stopped else '否（全部按计划完成）'}",
        f"- 结束时时刻：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "> 本文件只记录数据层刷新；不含任何信号/持仓/建议措辞，不构成投资建议。",
    ]
    _write_report(head + tail)
    print("\n".join(head + tail))
    print(f"\n报告 -> {REPORT_MD.relative_to(BASE_DIR)}")
    return 1 if stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
