#!/usr/bin/env python3
"""基金日频参谋 v4 · 主入口（盘中决策辅助系统）。

用法：
    python3 run.py                 # 自动按时钟判断时点（14:55 分界）
    python3 run.py --slot mid      # 11:30 午盘·趋势状态扫描
    python3 run.py --slot post     # 14:55 收盘前·决策窗口
    python3 run.py --slot post --force     # 非交易日也强制执行
    python3 run.py --slot post --no-push   # 只落盘报告，不推送飞书
    python3 run.py --no-publish-gate       # 调试逃生口：跳过发布资格门禁照常推
    python3 run.py --no-lookthrough        # 跳过重仓股穿透（省网络请求）

v4 数据流（2026-08-25，GPT-5.6 诊断落地 + 项目回测铁律融合）：
    基金净值 → 穿透（季报前十大）→ 实时行情 → 日内特征引擎
        → [mid: 快照落盘] / [post: 读快照算变化量] → 决策引擎（倾向分 + 四道门槛）
        → 报告 / 飞书
    动作层受 config.decision.gates.history_validated 硬门禁：
        当前 false → 只输出倾向分 + 候选动作，实际动作恒为「保持不动/观察」，
        待 backtest_action.py 证据裁决后由人工解锁。
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from core import data_loader, signal_engine, account as account_mod
from core import report_generator, notify, lookthrough as lookthrough_mod, rotation as rotation_mod
from core import real_time as realtime_mod
from core import intraday_features as feat_mod
from core import intraday_store as snap_mod
from core import intraday_feature_store as feat_store
from core import forecast_engine
from core import decision_engine
from core import market_context
import audit_project as audit_project_mod                             # 审计指针刷新入口
from audit_project import (assert_no_fund_codes, mask_manifest_funds,   # 清单掩码 + 发布资格
                           publish_gate)                            # 单一来源（本文件只调不裁决）

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


def _write_run_manifest(now: datetime, manifest: dict, fund_pool) -> bool:
    """机器可读运行清单（V4-A 证据底座，2026-09-16）。

    退出码只说「成 / 降级 / 败」，本文件说「哪几环证据链是坏的」——供 Windows
    计划任务与监控判读「程序没崩但数据坏了」的静默降级。落盘
    output/run_manifest/run_manifest_<run_id>.json。

    原子写（2026-09-17）：tmp + ``os.replace``，半截文件不会被读成「证据完整」。
    返回是否落盘成功；失败不阻断主流程，由调用方转成 DEGRADED（exit=2）。
    本函数不把自身失败写进清单——清单都没写成，收据无从自证；监控侧改以
    「exit=2 且本次 run_id 无对应清单」识别 ``run_manifest_write_failed``。

    ``fund_pool``（V4.1 ④，2026-09-18）：别名基准表，**必传**。output/run_manifest/ 随
    仓库跟踪，故落盘前把逐基金字段与降级项代码段统一换成位置别名（``F1``…），并在文件
    里留基准表指纹供门禁反查；掩码后若仍残留 6 位代码则拒绝落盘（转 DEGRADED），
    宁可少一份证据，也不写出一份泄露持仓的清单（P0-2）。
    """
    run_id = f"{now:%Y%m%d_%H%M%S}_{manifest.get('slot', 'na')}"
    out_dir = BASE_DIR / "output" / "run_manifest"
    final = out_dir / f"run_manifest_{run_id}.json"
    tmp = out_dir / f"{final.name}.tmp"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = mask_manifest_funds(
            {"run_id": run_id, "ts": now.strftime("%Y-%m-%dT%H:%M:%S%z"), **manifest},
            fund_pool)
        assert_no_fund_codes(payload)
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, final)
        log(f"[mani] 运行清单 → output/run_manifest/run_manifest_{run_id}.json"
            f"（status={manifest.get('status')}）")
        return True
    except Exception as e:                      # noqa: BLE001
        log(f"[warn] 运行清单写入失败（本次运行判为 DEGRADED）：{type(e).__name__} {e}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def nav_fallback_funds(funds: dict) -> list[str]:
    """本次加载中走了 `cache:fallback`（全链失败退旧缓存）的基金代码（V4.1 ③）。

    单独成函数只为可单测——判定语义本身归 `data_loader.is_nav_fallback`（唯一事实源），
    本函数不做第二套 `startswith`。
    """
    return [str(c) for c, f in (funds or {}).items() if data_loader.is_nav_fallback(f)]


def _finalize_run(now: datetime, manifest_payload: dict, degraded: list[str],
                 fund_pool) -> int:
    """落清单 + 定退出码（V4-A，2026-09-17）。

    清单写失败 ⇒ 本次不得声称「证据链完整」：改为追加 ``run_manifest_write_failed``
    并返回 2。清单本身不记录自身失败（文件都没写成，收据无从自证），监控侧以
    「exit=2 且本次 run_id 无对应清单」识别。``fund_pool`` 同 ``_write_run_manifest``。
    """
    if not _write_run_manifest(now, manifest_payload, fund_pool):
        degraded.append("run_manifest_write_failed")
    if degraded:
        log(f"[exit] DEGRADED（{len(degraded)} 项降级：{'; '.join(degraded)}）")
        return 2
    return 0


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


OBSIDIAN_LOG = Path(r"D:\Obsidian\My-First-Obsidian\量化交易工具\基金日频参谋-信号流水-2026.md")


def _configured_obsidian_log() -> Path | None:
    """P0-3（2026-09-23）：Obsidian 流水路径配置化。

    单一事实来源 = config.notify.obsidian_signal_log（绝对路径，或相对项目根）；
    留空/删键 = 禁用 Obsidian 流水追加（静默跳过，不算 degraded）；
    config.json 缺失/损坏 → 回退旧默认路径（保持原有行为）。模块级 OBSIDIAN_LOG
    仅作为该回退值保留，不再是运行时事实来源。
    """
    try:
        cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
        raw = str((cfg.get("notify") or {}).get("obsidian_signal_log") or "").strip()
    except (OSError, json.JSONDecodeError, AttributeError):
        return OBSIDIAN_LOG
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_absolute() else BASE_DIR / p


def _write_text_atomic(path: Path, text: str) -> None:
    """原子写（tmp + os.replace）：只替换这一个文件，绝不整目录/整文件重排。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def append_obsidian_log(slot: str, signals: dict, account: dict, now,
                        path: Path | None = None) -> bool:
    """盘后把当日信号追加一行到 Obsidian 信号流水（只记盘后、只记交易日）。

    路径解析（P0-3，2026-09-23）：path 未指定 → config.notify.obsidian_signal_log
    （留空 = 禁用，静默跳过不算 degraded；config 缺失/损坏 → 回退 OBSIDIAN_LOG）。

    设计（2026-08-23 用户确认）：盘前/盘中是过程态，盘后才是当日定论 → 一天 1 行。
    文件不存在时自动创建并写表头。

    V4.2 幂等（2026-09-18）：同一交易日**只保留一行**。重复运行不再追加第二行，而是
    就地更新当日行（last-run-wins，与发布门禁同口径）——否则修 bug 后的重跑会与旧行
    并存，读者无从判断哪行才是当日定论。实现只做**单行替换 + 原子写**：其余行逐字节
    不动（09-18 已发生过「读-改-写整文件」把历史小节抹掉的事故，此处不再给那种机会）。
    """
    if path is None:
        path = _configured_obsidian_log()
        if path is None:
            log("[obs ] 未配置 Obsidian 信号流水路径（notify.obsidian_signal_log 留空）→ 跳过追加")
            return False
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
    key = f"| {now:%m-%d} 盘后 |"
    if not path.exists():
        head = ("# 基金日频参谋 · 盘后信号流水（2026）\n\n"
                "> 只记盘后当日定论；🔴偏多 🟢偏空 ⚪中性；分数为三因子总分；不构成投资建议。\n\n"
                "| 日期 | " + " | ".join(str(c) for c in signals) + " | 账户面 |\n"
                "|:---" + "|---:" * (len(signals) + 1) + "|\n")
        _write_text_atomic(path, head + row)
        return True
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    hits = [i for i, ln in enumerate(lines) if ln.startswith(key)]
    if not hits:
        _write_text_atomic(path, (text if text.endswith("\n") else text + "\n") + row)
        return True
    last_hit = hits[-1]
    merged: list[str] = []
    for i, ln in enumerate(lines):
        if i not in hits:
            merged.append(ln)
        elif i == last_hit:
            merged.append(row)          # 当日行就地更新（last-run-wins）
    _write_text_atomic(path, "".join(merged))
    log(f"[obs ] 当日流水行已就地更新（幂等：不追加第二行"
        + (f"，归并 {len(hits)} 行重复" if len(hits) > 1 else "") + "）")
    return True


def _collect_realtime(lookthrough: dict | None) -> tuple[dict[str, dict], list[str], list[str]]:
    """实时行情采集 → (realtime, failures, degraded_funds)。

    V4-A（2026-09-17）：两种缺位必须可判读——
    - ``failures``：请求异常，行情根本没拿到；
    - ``degraded_funds``：行情到手但估值不可用（``est_change_pct is None``）。
    二者由调用方转成降级项，确保「部分基金缺位」不会伪装成 exit=0。抽成独立
    函数是为了在零网络条件下测得到（否则只能拉起真实抓取）。
    """
    realtime: dict[str, dict] = {}
    failures: list[str] = []
    degraded_funds: list[str] = []
    for code, a in (lookthrough or {}).items():
        try:
            rt = realtime_mod.fetch_realtime(a["rows"])
            est = realtime_mod.weighted_estimate(a["rows"], rt)
            if est["est_change_pct"] is not None:
                realtime[code] = {"quotes": rt, **est}
                log(f"[rt  ] {code} 当日估算 {est['est_change_pct']:+.2f}%"
                    f"（前十大覆盖 {est['covered_pct']:.0f}%，持仓截至 {a['snapshot_date']}）")
            else:
                degraded_funds.append(code)
                log(f"[warn] {code} 实时行情可得但估值不可用（est_change_pct=None，"
                    f"覆盖 {est.get('covered_pct')}%）——该基金今日证据缺位")
        except Exception as e:
            failures.append(code)
            log(f"[warn] {code} 实时行情不可用：{e}")
    return realtime, failures, degraded_funds


def realtime_degraded_reasons(failures: list[str], degraded_funds: list[str]) -> list[str]:
    """实时行情缺位 → 降级项（纯函数，便于逐字校验降级措辞）。"""
    out: list[str] = []
    if failures:
        out.append("realtime_failed:" + ",".join(failures))
    if degraded_funds:
        out.append("realtime_degraded:" + ",".join(degraded_funds))
    return out


def build_features(slot: str, lookthrough: dict | None, realtime: dict[str, dict],
                   now: datetime) -> dict[str, dict]:
    """实时行情 → 日内特征（mid 存快照；post 读同日 mid 快照算变化量）。返回 {code: feats}。"""
    if not lookthrough or not realtime:
        return {}
    date_str = now.strftime("%Y-%m-%d")
    mid_snap = snap_mod.load_snapshot(date_str, "mid") if slot == "post" else None
    feats: dict[str, dict] = {}
    for code, rt in realtime.items():
        agg = lookthrough.get(code)
        if not agg:
            continue
        prev_est = None
        if mid_snap and mid_snap.get("funds", {}).get(code):
            prev_est = mid_snap["funds"][code].get("est_return")
        f = feat_mod.compute_features(
            agg["rows"], rt["quotes"],
            {"est_change_pct": rt["est_change_pct"], "covered_pct": rt["covered_pct"]},
            agg["snapshot_date"], prev_est)
        if f:
            feats[code] = f
    return feats


def build_account_states(account: dict) -> dict[str, dict]:
    """账户面 → 决策引擎账户约束输入（每只持仓基金）。"""
    if not account.get("total_market_value"):
        return {}
    states = {}
    for p in account["positions"]:
        states[p["code"]] = {
            "current_weight": p.get("position_pct")
            or (p["market_value"] / account["total_market_value"]),
            "max_weight": p.get("max_position_pct", 0.8),
            "cost_nav": p.get("cost_nav"),
            "last_nav": p.get("last_nav"),
            "consecutive_adds": p.get("consecutive_adds", 0),
        }
    return states


def build_forecast_meta(code: str, f_rt: dict, lookthrough: dict | None,
                        signals: dict) -> dict:
    """v5 预测特征字典（B1 协议：缺失 = None，不强制零化；纯函数，可零网络测例）。

    2026-09-22 评审修复（①）：旧实现（_run 内联）三处缺失强制 0——
    ``est_sign``（est 缺失 → 0，破坏 B1 mask）、``composite``（穿透缺失 → 0，而 0
    是合法业务值「中性」）、``score``（signals 缺失 → 0）。训练侧（forecast_engine）
    对缺失做 (值, missing_mask) 双列，live 侧却把「缺失」写成「真 0」喂给模型。
    现：有值 → 原值；缺失 → None，由 ForecastEngine.predict() 统一打 mask
    （训练/推理协议真正对齐）。

    2026-09-22 V4.4 步 2（量纲接入）：est_return 是百分数（腾讯行情口径），
    est_chg 特征按契约过唯一桥 ``est_chg_from_pct`` 转 fraction——训练样本
    （backtest_spread closes 比值）本就是 fraction，接入前 live 侧 100× 错位
    （72 条 post 行 66 条落在训练支撑域外）。切换日常量与读取端判别函数见
    pit1455_contract.EST_CHG_LIVE_FRACTION_SINCE。
    """
    from core.pit1455_contract import est_chg_from_pct
    est = est_chg_from_pct(f_rt.get("est_return"))
    lt = (lookthrough or {}).get(code)
    sig = signals.get(code)
    return {
        "est_chg": est,
        "est_sign": (1 if est > 0 else (-1 if est < 0 else 0)) if est is not None else None,
        "breadth": f_rt.get("breadth"),
        "concentration": f_rt.get("concentration"),
        "covered_pct": f_rt.get("covered_pct"),
        "composite": lt.get("composite") if isinstance(lt, dict) else None,
        "score": sig.get("score") if isinstance(sig, dict) else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="基金日频参谋 v4（盘中决策辅助系统）")
    parser.add_argument("--slot", choices=["pre", "mid", "post"], help="时点（缺省按时钟自动判断）")
    parser.add_argument("--force", action="store_true", help="非交易日也强制执行")
    parser.add_argument("--no-push", action="store_true", help="不推送飞书，只落盘报告")
    parser.add_argument("--no-publish-gate", action="store_true",
                        help="调试逃生口：跳过发布资格门禁照常推送（审计可查，须慎用）")
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

    # V4-A 证据底座（2026-09-16）：本次运行的证据链状态。
    # 退出码 0=SUCCESS / 2=DEGRADED / 1=FAILED；降级项一并写 output/run_manifest/。
    degraded: list[str] = []

    funds = {}
    fund_failures: list[str] = []
    fund_fallbacks: list[str] = []      # V4.1 ③：全链失败→退回旧缓存的基金
    for code in cfg["fund_pool"]:
        try:
            funds[code] = data_loader.load_fund(code, force_refresh=args.refresh)
            log(f"[data] {code} {funds[code]['name']} 净值 {len(funds[code]['navs'])} 条，"
                f"最新 {funds[code]['navs'][-1]}，申购={funds[code].get('purchase_status')}")
        except Exception as e:
            fund_failures.append(code)
            log(f"[warn] {code} 数据获取失败：{e}")
    # V4.1 ③（2026-09-18）：cache:fallback 是「load_fund 正常返回」的降级态，只 catch
    # 异常抓不到它。旧实现漏判 ⇒ 全链失败仍 exit=0、发布门禁看不见数据污染，与
    # 「数据降级 → DEGRADED → 必要时阻止发布」契约不一致。
    fund_fallbacks = nav_fallback_funds(funds)
    for code in fund_fallbacks:
        log(f"[warn] {code} 净值链全链失败，已退回旧缓存"
            f"（_source={funds[code].get('_source')}）→ 计为降级")

    if not funds:
        log("[fail] 无任何基金数据，退出")
        return 1
    # V4.2（2026-09-18）：申购/赎回状态可得性。状态未知**不**进 degraded（它不影响报告
    # 其余栏的可信度，只影响「动作能不能执行」），而是① 记进清单 data.status_unknown
    # ② 由 decision_engine 的硬门禁把动作锁成 HOLD。
    status_unknown = [c for c in cfg["fund_pool"]
                      if c in funds and not data_loader.is_fund_status_known(funds[c])]
    if status_unknown:
        log(f"[warn] {len(status_unknown)} 只基金申购/赎回状态未知（_lsjz 取数失败）："
            f"{status_unknown}——动作层按硬门禁锁 HOLD，报告其余栏不受影响")

    if fund_failures:
        degraded.append("fund_data_partial:" + ",".join(fund_failures))
    if fund_fallbacks:
        # V4.1 ③：与 fund_data_partial 同级——都是「本次净值不可信」，只是成因不同
        # （前者全链抛异常，后者全链失败后静默退回旧缓存）。
        degraded.append("fund_data_fallback:" + ",".join(fund_fallbacks))

    lookthrough = None
    lt_missing: list[str] = []          # P0 探针：穿透静默缺失基金（进报告显式化）
    if not args.no_lookthrough:
        log("[look] 拉取重仓股穿透数据（季报持仓 + 个股K线）...")
        try:
            lookthrough = lookthrough_mod.evaluate_lookthrough(cfg["fund_pool"])
            for code, a in lookthrough.items():
                log(f"[look] {code} 持仓截至 {a['snapshot_date']} 覆盖 {a['coverage']*100:.0f}% "
                    f"吻结构 {a['structure']:+.2f} 净背驰 {a['div_net']:+.2f} 综合 {a['composite']:+d}"
                    + (" ⚠️防狼术" if a.get("fanglang_alert") else ""))
            lt_missing = [c for c in cfg["fund_pool"] if c not in lookthrough]
            if lt_missing:
                log(f"[warn] ⚠️ 穿透缺失基金：{lt_missing}——持仓快照拉取失败（多为网络/代理受限），"
                    f"受影响：实时估算/日内特征/中期趋势维度；请检查网络后重跑")
                degraded.append("lookthrough_missing:" + ",".join(lt_missing))
        except Exception as e:
            degraded.append(f"lookthrough_unavailable:{type(e).__name__}")
            log(f"[warn] 穿透数据不可用，报告将跳过该栏：{e}")

    realtime, realtime_failures, realtime_degraded = _collect_realtime(lookthrough)
    # V4-A（2026-09-17）：实时行情部分失败必须显式降级。
    # 部分基金缺位时报告仍会照常生成，不记降级则 exit=0 会谎称「证据链完整」。
    degraded.extend(realtime_degraded_reasons(realtime_failures, realtime_degraded))

    # ---- 日内特征（观察层）+ 快照持久化 ----
    feats = build_features(slot, lookthrough, realtime, now)
    if not feats:
        degraded.append("intraday_features_empty")
        log("[warn] ⚠️ 无日内特征（穿透或实时行情为空）——倾向分/预测/动作评分今日缺位，报告决策栏不完整")
    if feats:
        if slot == "mid":
            snap_mod.save_snapshot(now.strftime("%Y-%m-%d"), "mid", feats)
            log(f"[snap] 11:30 特征快照已存盘（{len(feats)} 只基金）→ 14:55 时计算变化量")
        for code, f in feats.items():
            extra = ""
            if f.get("close_phase_change") is not None:
                extra = f" Δ{f['close_phase_change']:+.2f}%"
            log(f"[feat] {code} 估算 {f['est_return']:+.2f}% 同向 {f['breadth']:+.2f}"
                f" 覆盖 {f['covered_pct']:.0f}% 可信 {f['reliability']:.2f}{extra}")

    # ---- 三因子弱参考（保留原口径，作为决策引擎「中期趋势」维度）----
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

    # ---- Market Context 观察层 v0（2026-09-02）：进攻/防守篮子环境快照 ----
    # 仅观察：不进 decision_engine、不进 forecast、不改任何门禁；失败不阻断主流程。
    mc_snap = None
    try:
        mc_snap = market_context.compute(slot)  # 观察层 v0：只计算+落盘每日快照，不接 Forecast/Policy
        if mc_snap.get("ok"):
            log(f"[mc  ] 市场环境 {mc_snap['regime']}｜off5 {mc_snap['offensive_score_5d']:+.2f}% "
                f"def5 {mc_snap['defensive_score_5d']:+.2f}%｜spread {mc_snap['spread_5d']:+.2f}% "
                f"breadth {mc_snap['breadth_all_above_ma20']*100:.0f}%"
                + (f"（缺失 {len(mc_snap['errors'])} 项）" if mc_snap['errors'] else ""))
        else:
            degraded.append("market_context_unavailable")
            log(f"[warn] 市场环境层不可用：{mc_snap.get('errors')}")
    except Exception as e:
        degraded.append(f"market_context_error:{type(e).__name__}")
        log(f"[warn] 市场环境层异常（已跳过）：{type(e).__name__} {e}")

    # ---- 决策倾向层（实时数据进决策；动作层受 history_validated 硬门禁）----
    decisions: dict[str, dict] = {}
    if feats:
        pool_est = {code: feats[code]["est_return"] for code in feats}
        acct_states = build_account_states(account)
        mid_snap = snap_mod.load_snapshot(now.strftime("%Y-%m-%d"), "mid") if slot == "post" else None
        for code in cfg["fund_pool"]:
            f1130 = (mid_snap or {}).get("funds", {}).get(code) if slot == "post" else None
            # v5 多周期预测特征：由实时日内特征合成（est_chg/breadth/composite/score）
            feat_meta = None
            if code in feats:
                # 2026-09-22 评审修复（①）：缺失 = None（B1 mask），不强制零化（旧三处强制 0 已废除）
                feat_meta = build_forecast_meta(code, feats[code], lookthrough, signals)
            d = decision_engine.evaluate_with_forecast(decision_engine.DecisionInput(
                code=code, name=signals.get(code, {}).get("name", code), slot=slot,
                technical_score=signals.get(code, {}).get("score", 0),
                feat_1130=f1130,
                feat_1455=feats.get(code),
                pool_est=pool_est,
                account_state=acct_states.get(code),
                # V4.2：状态未知 ⇒ 动作层强制 HOLD（决策层硬门禁，不靠调用方自觉）
                fund_status_known=data_loader.is_fund_status_known(funds.get(code)),
            ), feature_meta=feat_meta)
            decisions[code] = decision_engine.decision_to_cn(d)
            # v7 P1：预测特征日级累积存储（state→forecast 实验 / 联合回测的数据基建）
            if feat_meta:
                fc_ctx = d.forecast or {}
                sr_state = (getattr(d, "state_ref", None) or {}).get("state")
                feat_store.append_features(
                    now.strftime("%Y-%m-%d"), slot, code, feat_meta,
                    context={"state": sr_state,
                             "overall_confidence": fc_ctx.get("overall_confidence"),
                             "model_ready": fc_ctx.get("model_ready")},
                    model_version=forecast_engine.MODEL_VERSION)
            gate_note = f" | 门槛未过×{len(d.invalid_conditions)}" if d.invalid_conditions else ""
            fc_note = ""
            if d.forecast and d.forecast.get("model_ready"):
                t1 = d.forecast.get("T1", {})
                fc_note = f" | 预测↑{t1.get('p_up', 0)*100:.0f}%↓{t1.get('p_down', 0)*100:.0f}%"
            log(f"[dec ] {code} 倾向 {d.score:+d}（置信 {d.confidence:.2f}）→ {decisions[code]['action']}{gate_note}{fc_note}")
            # State Engine 只读参考日志（2026-08-27）：历史同名状态的续走统计，非模型预测
            sr = getattr(d, "state_ref", None)
            if sr and sr.get("horizons"):
                p1 = (sr["horizons"].get("T1") or {}).get("p_up")
                p1_txt = f"{p1*100:.0f}%" if isinstance(p1, (int, float)) else "—"
                tag = "✓" if sr.get("all_horizons_stable") else "×"
                log(f"[stat] {code} 结构态 {sr['state']}｜历史T+1↑{p1_txt}｜全周期稳定 {tag}")
                _append_state_ref_history(now, slot, code, sr)

    # 发布资格门禁（V4-A 论域 A，2026-09-22）：报告生成**之前**评估，报告/卡片才能
    # 标注「观察基金降级·仅标注不拦」；推送阶段再评估一次（degraded 其后可能新增
    # 项目，以推送阶段那次为拦截裁决依据）。用闭包延迟求值：post 推送时本次清单尚未
    # 落盘，extra 必须带上当下 degraded，否则只审上午旧账。
    # 注：论域由 audit_project 侧从 config.fund_pool ∪ holdings.json 取（论域 A：全池
    # 受审）。门禁的**内存**侧用真实代码归因；落盘清单统一掩码为位置别名（V4.1 ④）
    # ——output/run_manifest/ 随仓库跟踪，直接写 6 位代码违反 P0-2。
    gate_structured = {
        "data": {"failed": fund_failures, "fallback": fund_fallbacks},
        "lookthrough": {"missing": lt_missing},
        "realtime": {"failed": realtime_failures, "degraded": realtime_degraded},
    }

    def gate_eval():
        current = [r for r in degraded if r != "feishu_push_failed"]
        return publish_gate(now, extra=[(current, gate_structured)])

    gate_report = gate_eval()

    report = report_generator.generate_report(slot, signals, account, lookthrough, rot,
                                              realtime, decisions, market_context=mc_snap,
                                              lt_missing=lt_missing, gate=gate_report)
    log(f"[repo] 报告已生成 → output/report_{now:%Y%m%d}_{slot}.md")

    if append_obsidian_log(slot, signals, account, now):
        log(f"[obs ] 盘后信号已追加 → Obsidian 信号流水")

    if args.no_push:
        log("[push] --no-push 指定，跳过飞书推送")
        push_status = {"ok": None, "reason": "skipped_no_push"}
    elif args.no_publish_gate:
        gate = gate_eval()
        log(f"[gate] --no-publish-gate 指定，跳过发布资格门禁（本次评估：{gate['detail']}）")
        push_status = {"ok": None, "reason": "skipped_gate_flag",
                       "gate": {"ok": gate["ok"], "reason": gate["reason"]}}
        degraded.append("publish_gate_bypassed")   # 不静默绕闸：exit=2 留痕可审计
    else:
        gate = gate_eval()
        if not gate["ok"]:
            log(f"[gate] 发布资格拦截：{gate['detail']}")
            # 清单会随仓库跟踪：只落计数与原因，真实代码只进本地日志（P0-2）。
            push_status = {"ok": None, "reason": "blocked_publish_gate",
                           "gate": {"ok": False, "reason": gate["reason"],
                                    "n_contaminated": len(gate["contaminated"]),
                                    "n_universe": len(gate["universe"])}}
            degraded.append(f"publish_gate_blocked:{gate['reason']}")
        else:
            result = notify.push_feishu(slot, signals, account, realtime, decisions, gate=gate)
            if result.get("ok"):
                log("[push] 飞书推送成功")
                push_status = {"ok": True, "reason": None,
                               "gate": {"ok": True, "reason": None}}
            else:
                push_status = {"ok": False,
                               "reason": str(result.get("reason") or result.get("error")
                                             or result.get("response"))[:200]}
                degraded.append("feishu_push_failed")
                log(f"[push] 飞书推送未成功（{push_status['reason']}）")

    # Shadow Policy 日记录（2026-09-01，P1-⑦）：post 时点跑一次，只记录不执行。
    # shadow_policy.py 自带幂等（同日同基金跳过）与冻结模型校验，失败不阻断主流程，
    # 但会计入降级项（V4-A：exit=0 不再等价于「证据链完整」）。
    shadow_status = {"ok": None, "reason": "skipped_non_post_slot"}
    if slot == "post":
        shadow_status = _run_shadow()
        if not shadow_status.get("ok"):
            degraded.append(f"shadow_failed:{shadow_status.get('reason')}")


    manifest_payload = {
        "slot": slot,
        # ok 语义保持不变（= 无抛异常型失败），fallback 单列：读侧若要判「数据可信」
        # 需同时看 ok 与 fallback，避免悄悄改写既有字段的含义。
        # failed/fallback 为真实代码（内存态）；落盘时由 mask_manifest_funds 统一掩码。
        "data": {"ok": not fund_failures, "failed": fund_failures,
                 "fallback": fund_fallbacks, "status_unknown": status_unknown,
                 "n_funds": len(funds)},
        "lookthrough": {"ok": lookthrough is not None, "missing": lt_missing},
        "realtime": {"ok": not (realtime_failures or realtime_degraded),
                     "failed": realtime_failures, "degraded": realtime_degraded,
                     "n_funds": len(realtime)},
        "intraday_features": {"ok": bool(feats), "n_funds": len(feats)},
        "market_context": {"ok": bool(mc_snap and mc_snap.get("ok"))},
        "report": {"ok": True, "path": f"output/report_{now:%Y%m%d}_{slot}.md"},
        "notification": push_status,
        "shadow": shadow_status,
        "degraded_reasons": degraded,
        "status": "DEGRADED" if degraded else "SUCCESS",
    }
    code = _finalize_run(now, manifest_payload, degraded, cfg["fund_pool"])

    # 当前审计指针刷新（V4.2，2026-09-18）：让 output/audit_current.json 真正「current」
    # ——旧版只有人手动跑审计才更新，实况里曾停在上一交易日。
    # 位置在**清单落盘之后**：指针描述的必须是「含本次运行在内」的证据状态，放前面会
    # 滞后一轮（post 轮的发布门禁证据就白写了）。失败不降级——指针是派生视图，其时效
    # 已由 payload 的 evidence_stale 自证；结果只进本地日志（output/logs/，gitignored），
    # 派生视图不进证据链。
    if slot == "post":
        try:
            out, snap = audit_project_mod.refresh_current()
            log(f"[aud ] 审计当前指针已刷新 → {out.name}（history: {snap.name}）")
        except Exception as e:                      # noqa: BLE001
            log(f"[warn] 审计当前指针刷新失败（不阻断本次运行，指针保持上一版）："
                f"{type(e).__name__}: {e}")
    return code


def _run_shadow() -> dict:
    """调用 shadow_policy.py 记录当日 shadow 样本（subprocess，容忍失败）。

    返回 ``{"ok": bool, "reason": str | None}``：shadow 失败不阻断主流程，
    但会作为降级项进入 run_manifest，并让本次退出码为 2。
    """
    import subprocess
    try:
        r = subprocess.run([sys.executable, str(BASE_DIR / "shadow_policy.py")],
                           capture_output=True, text=True, timeout=1200)
        out = (r.stdout or "").strip().splitlines()
        key = next((ln for ln in out if ln.startswith("== [3]")), None)
        if key:
            log(f"[shadow] {key.strip('= ')}")
        if r.returncode != 0:
            err = ((r.stderr or "").strip().splitlines() or ["<no stderr>"])[-1]
            log(f"[shadow] ⚠️ 退出码 {r.returncode}：{err}")
            return {"ok": False, "reason": f"exit_{r.returncode}"}
        return {"ok": True, "reason": None}
    except Exception as e:
        log(f"[shadow] ⚠️ 失败（不阻断主流程）：{e}")
        return {"ok": False, "reason": type(e).__name__}


def _append_state_ref_history(now, slot: str, code: str, sr: dict) -> None:
    """[stat] 行落盘 output/state_ref_history.jsonl（2026-08-27）。

    积累真实运行日的「结构态 + 当时历史条件分布」，为后续把 state_ref
    从「参考」升格为「预测输入」攒证据；失败静默不阻断主流程。
    """
    try:
        import json as _json
        from pathlib import Path as _Path
        out = _Path(__file__).resolve().parent / "output" / "state_ref_history.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": now.strftime("%Y-%m-%d %H:%M:%S"), "slot": slot, "fund": code,
               "state": sr.get("state"), "all_horizons_stable": sr.get("all_horizons_stable"),
               "horizons": sr.get("horizons")}
        with out.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
