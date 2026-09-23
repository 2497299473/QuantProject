"""P1-① 样本级冻结（2026-09-10；Summer 09-09 11:18 拍板把 P1 由 09-11 提前至 09-10）。

用**已合入三态加固的生产路径** `backtest_spread.load_samples()` 跑一遍，逐行写
forecast_outputs/samples_frozen_YYYYMMDD.jsonl + sha256 指纹 + meta 侧车。

本脚本是纯只读包装器：不复制、不修改 load_samples 的任何口径，不改
config.json / model_ready / 生产 .py，不写 data/。

频控纪律（东财 IP 级）：
  holdings_history 无缓存（forecast_outputs/f10_raw 只是降级页留档，不是缓存），
  故本脚本必然产生命中请求：4 基金 × 7 年 = 28 次 fundf10 首发，最坏情况按生产
  既有策略每年代 2 次重试（退避 2s/5s）= 上限 84 次。该重试策略**原样沿用，不放宽、
  不加强、不自行加第三轮**。
  若捕获到 DegradedResponse / 缺年告警等降级或频控征兆 → 以退出码 1 结束并报告，
  由 Summer 决定是否补拉，禁止在本脚本外私自重试。

  原子发布（2026-09-20，P0-2）：
   三件套（jsonl / kfp / meta）一律先写 forecast_outputs/.staging/，降级/频控
   检查**通过之后**才逐个 os.replace 进 canonical 名。降级 ⇒ staging 整体
   rename 到 forecast_outputs/freeze_failed_<YYYYMMDD>_<HHMMSS>/ 留证并 exit 1，
   canonical 三件保证一个都不存在——旧实现先写 canonical 再查 flags，exit 1 时
   件已残留，no-clobber 闸门会把「降级残留」误当「当日已冻结」，挡住 09-26
   季度重估预注册的「同日重试 1 次」。降级留证目录随 forecast_outputs/
   整体不入库，不写 data/，不触碰既有 canonical 件。

V4.3 P0-2/P0-3（2026-09-20）扩展 —— KFP as-of + universe 口径 + 数据质量门禁：
   - KFP = build_fingerprint(stock_codes=<样本 universe>, cutoff=<冻结日>)：
     冻结日之后新增行不改指纹（窗口滚动假漂移消除）；cutoff 前历史 close
     追溯改写仍改指纹；scope 头使其与 09-20 前全量口径锚点不可混用。
   - 三道 SNAPSHOT_INVALID 闸门（canonical 不发布、freeze_failed_* 留证、exit 1）：
     ① 任一 stock K-line 拉取失败（数据质量门禁，不静默跳过）
     ② KFP unreadable 非空（请求码缓存缺失 / 经 cutoff 滤空）
     ③ code commit 取不到（三件套第①件断链）
   meta 增 code_commit / stock_data_failures / kline_fingerprint_scope
   （schema_version=2）；旧件（schema_version=1、无字段）verify 侧不判。

V4.3.1-⑤（2026-09-22，外部复审）：KFP fund 侧 scope ——
   build_fingerprint 另传 fund_codes=<样本基金排序去重>：基金侧只哈希样本
   实际出现的基金，无关基金缓存变化（扩池/新基金引入/净值 TTL 改写）
   不改冻结指纹。09-10 锚点的 data/klines 恰只含 4 只样本基金 ⇒
   canonical 逐字节不变，既有锚点数字不回归。

用法:
  python experiments/forecast_lab/freeze_samples.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import warnings
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]          # QuantV1 根
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "experiments" / "forecast_lab"))

from kline_fingerprint import build_fingerprint   # noqa: E402  数据层指纹（2026-09-10 拍板）
from freeze_verify_tool import git_commit as _git_commit_at_freeze  # noqa: E402  三件套①

# 降级/频控征兆关键词（来自 core/lookthrough 三态判定的告警文案与异常类名）
RATE_MARKERS = ("DegradedResponse", "SUSPECT_DEGRADED", "PARSE_MISMATCH",
                "持仓拉取失败", "年持仓拉取失败")


def _hist_mode() -> str:
    """历史特征时点口径：从生产真源 backtest_spread 常量读取（不另写一份，防漂移）。

    本脚本产出的样本均由 backtest_spread.load_samples() 生成 ⇒ 恒为 EOD_PROXY。
    取不到时返回 "UNKNOWN"（不猜、不默认成 PIT 口径）。
    """
    try:
        from backtest_spread import HISTORICAL_FEATURE_MODE as _m
        return str(_m)
    except Exception:
        return "UNKNOWN"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="冻结样本 + K 线指纹（零写入 data/，只读包装器）")
    ap.add_argument("--force", action="store_true",
                    help="允许覆写同日已有冻结件（默认拒绝；覆写须在报告里记录「已重采」）")
    args = ap.parse_args(argv)

    outdir = BASE_DIR / "forecast_outputs"
    outdir.mkdir(exist_ok=True)
    # 原子发布 staging（2026-09-20 P0-2）：三件套先落这里，降级检查过了才发布。
    staging_dir = outdir / ".staging"

    started = datetime.now()
    date_tag = started.strftime("%Y%m%d")
    out_jsonl = outdir / f"samples_frozen_{date_tag}.jsonl"
    out_meta = outdir / f"samples_frozen_{date_tag}.meta.json"
    out_kfp = outdir / f"kline_fingerprint_{date_tag}.json"

    # no-clobber（2026-09-18，C2 特征非不变性 P0）：同日已冻结过就**拒绝重采**。
    # 依据：标签 0% 漂移、特征最高 100% 漂移 ⇒ 同一日期重采一次就是**另一套数字**。
    # 若允许覆写，09-26 季度重估的「重试 1 次」会静默产出第二份样本，
    # 阶段 1（冻结）与阶段 2（基线复算）的 sha 绑定随之失效。
    # 必须在 load_samples() **之前**判定 —— 放在之后等于东财请求已发出去才说不采，
    # 既没防住重采、又白烧一次配额。
    if out_jsonl.exists() and not args.force:
        print(f"\n[ABORT] 当日冻结件已存在，拒绝重采：{out_jsonl.name}")
        print("        复用既有件（阶段 2 直接读它），或显式 --force 重采并在报告记录「已重采」。")
        print("        校验既有件：python -X utf8 freeze_verify_tool.py verify "
              f"--jsonl forecast_outputs/{out_jsonl.name}")
        return 2

    # 生产路径，原样调用（含其 print 进度输出，便于后台日志观察是否卡频控）
    from backtest_spread import load_samples

    # 降级/频控征兆捕获（2026-09-20 P0-2 补异常路径）：旧实现只查 warnings；
    # 若生产路径以异常形式抛降级征兆（类名命中 RATE_MARKERS），会 traceback 裸奔
    # 且不留留证。现：命中关键词的异常记入 flags 走同一降级分支；未命中的异常
    # （含测试哨兵 AssertionError）**原样上抛**，既有行为不变。
    warn_msgs: list[str] = []
    flags: list[str] = []

    def _capture(msg: object) -> None:
        m = str(msg)
        warn_msgs.append(m)
        if any(k in m for k in RATE_MARKERS):
            flags.append(m)

    # V4.3 防御：降级异常路径只重赋 samples，若 universe/failures 未先初始化，
    # 后续数据质量门禁读 stock_failures 会 NameError（现靠 flags 分支先 return
    # 兜着，属脆弱依赖）——显式预初始化，异常路径也保证有值可查。
    stock_universe, stock_failures = {}, []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            samples, stock_universe, stock_failures = load_samples(return_universe=True)
        except Exception as exc:          # noqa: BLE001 只拦降级征兆，其余上抛
            name = type(exc).__name__
            if any(k in name for k in RATE_MARKERS):
                _capture(f"{name}: {exc}")
                samples = []
            else:
                raise
    for x in caught:
        _capture(x.message)

    # ---- 降级/频控征兆 ⇒ 停手留证，canonical 三件一个都不写（P0-2 原子性）----
    # 判定必须先于任何 canonical 落盘：旧实现写完 jsonl 才查 flags，exit 1 时件已
    # 残留，no-clobber 把「降级残留」误当「当日已冻结」，09-26「同日重试 1 次」失效。
    if flags:
        stamp = started.strftime("%H%M%S")
        quar = outdir / f"freeze_failed_{date_tag}_{stamp}"
        if quar.exists():
            quar = outdir / f"freeze_failed_{date_tag}_{stamp}_{os.getpid()}"
        quar.mkdir(parents=True)
        (quar / "failed_flags.json").write_text(
            json.dumps({"kind": "forecast_lab_freeze_failed",
                        "created_at": started.isoformat(timespec="seconds"),
                        "degraded_or_ratelimit_flags": flags},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        (quar / "stdout_note.txt").write_text(
            "降级/频控征兆停手：本次未发布任何 canonical 冻结件。\n"
            "处置：由 Summer 决定是否补拉（禁止私自重试）；留证目录随 "
            "forecast_outputs/ 整体不入库。\n", encoding="utf-8")
        print("\n[ABORT] 捕获降级/频控征兆 —— 立即停手，勿重试（canonical 未落盘）：")
        for m in flags:
            print(f"    ! {m[:300]}")
        print(f"        留证目录 : {quar}")
        return 1

    # ---- V4.3 P0-3 数据质量门禁：任一 stock K-line 拉取失败 ⇒ SNAPSHOT_INVALID ----
    # 不完整样本不得冻结为完整（09-18 C2 根因教训：静默跳过使 covered_pct
    # 靠不完整数据算、事后无迹可查）。与降级共用留证分支：canonical 不发布、
    # 留证、exit 1，由 Summer 决定是否补拉（禁止私自重试）。
    if stock_failures:
        stamp = started.strftime("%H%M%S")
        quar = outdir / f"freeze_failed_{date_tag}_{stamp}_stockfail"
        if quar.exists():
            quar = outdir / f"freeze_failed_{date_tag}_{stamp}_stockfail_{os.getpid()}"
        quar.mkdir(parents=True)
        (quar / "failed_flags.json").write_text(
            json.dumps({"kind": "forecast_lab_freeze_failed",
                        "verdict": "SNAPSHOT_INVALID",
                        "reason": "stock_data_failures",
                        "created_at": started.isoformat(timespec="seconds"),
                        "stock_data_failures": stock_failures},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        (quar / "stdout_note.txt").write_text(
            "数据质量门禁：个股K线拉取失败，快照 INVALID，未发布任何 canonical 件。\n"
            "处置：由 Summer 决定是否补拉（禁止私自重试）；留证目录随 "
            "forecast_outputs/ 整体不入库。\n", encoding="utf-8")
        print("\n[ABORT] SNAPSHOT_INVALID —— 个股K线拉取失败（数据质量门禁）：")
        for f_ in stock_failures:
            print(f"    ! {f_['code']} ({f_['market']}): {f_['error'][:200]}")
        print(f"        留证目录: {quar}")
        return 1

    # ---- staging 落盘（与旧口径逐字节一致：write_text 同参 ⇒ sha 可复现不变）----
    staging_dir.mkdir(parents=True, exist_ok=True)
    st_jsonl = staging_dir / out_jsonl.name

    lines = [json.dumps(s, ensure_ascii=False, sort_keys=True) for s in samples]
    body = "".join(ln + "\n" for ln in lines)
    st_jsonl.write_text(body, encoding="utf-8")
    file_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()

    # ---- 分布与区间 ----
    dist: dict[str, int] = {}
    for s in samples:
        dist[s["fund"]] = dist.get(s["fund"], 0) + 1
    dates = sorted(s["date"] for s in samples) if samples else []

    # ---- 数据层指纹（前复权漂移防护，2026-09-10 Summer 拍板）----
    # 样本行 sha256 只锁行内容；缓存 TTL 到期重取会追溯改写历史 close，
    # 样本没变、语义变了。冻结时必须连 (date, close) 序列指纹一起落盘，
    # 否则两次「同 sha256 冻结」并不等价（P1 附带发现，报告 §六）。
    # V4.3 P0-2（2026-09-20）：as-of + universe 口径 —— stock_codes = 本次
    # 样本 universe，cutoff = 冻结日。冻结日之后新增行不改指纹（窗口滚动
    # 假漂移消除）；cutoff 前历史 close 追溯改写仍改指纹（对真问题的敏感度
    # 不变）。scope 头使其与 09-20 前全量口径锚点不可混用（见
    # kline_fingerprint 模块 docstring）。
    cutoff = started.strftime("%Y-%m-%d")
    # V4.3.1-⑤：fund 侧同样 scope 到样本基金（见模块 docstring）——无关基金
    # 缓存变化不改冻结指纹；09-10 锚点 data/klines 恰为 4 只样本基金，口径逐字节不变。
    kfp = build_fingerprint(stock_codes=sorted(stock_universe), cutoff=cutoff,
                            fund_codes=sorted({s["fund"] for s in samples}))
    st_kfp = staging_dir / out_kfp.name
    st_kfp.write_text(json.dumps(kfp, ensure_ascii=False, indent=1, sort_keys=True),
                      encoding="utf-8")

    # ---- V4.3 P0-2：KFP 不完整（请求码缺失/滤空）⇒ SNAPSHOT_INVALID ----
    # 不完整的数据指纹不得绑定冻结件——否则 G-B 比对有空洞（缺失码的漂移
    # 不可见）。与降级共用留证分支：canonical 不发布、留证、exit 1。
    if kfp["unreadable"]:
        stamp = started.strftime("%H%M%S")
        quar = outdir / f"freeze_failed_{date_tag}_{stamp}_kfp"
        if quar.exists():
            quar = outdir / f"freeze_failed_{date_tag}_{stamp}_kfp_{os.getpid()}"
        quar.mkdir(parents=True)
        (quar / "failed_flags.json").write_text(
            json.dumps({"kind": "forecast_lab_freeze_failed",
                        "verdict": "SNAPSHOT_INVALID",
                        "reason": "kfp_unreadable",
                        "created_at": started.isoformat(timespec="seconds"),
                        "kfp_unreadable": kfp["unreadable"]},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        for st in (st_jsonl, st_kfp):
            if st.exists():
                os.replace(st, quar / st.name)
        (quar / "stdout_note.txt").write_text(
            "数据质量门禁：K线指纹缺失请求码（缓存缺失或经 cutoff 滤空），"
            "快照 INVALID，未发布任何 canonical 件。\n"
            "处置：核对上方缺失码缓存，按铁律 7 四闸门补拉后重新冻结。\n",
            encoding="utf-8")
        print(f"\n[ABORT] SNAPSHOT_INVALID —— KFP unreadable 非空: {kfp['unreadable']}")
        print(f"        留证目录: {quar}")
        return 1

    # ---- V4.3：code_commit（三件套①）fail-closed ----
    # 冻结件必须能回答「样本基于哪个代码 commit 构建」；取不到（git 缺失/
    # 非仓库）⇒ 三件套断链 ⇒ INVALID 不发布（P1：研究冻结缺 commit 必须
    # fail-closed，不留无主件）。
    code_commit = _git_commit_at_freeze()
    if not code_commit:
        stamp = started.strftime("%H%M%S")
        quar = outdir / f"freeze_failed_{date_tag}_{stamp}_commit"
        if quar.exists():
            quar = outdir / f"freeze_failed_{date_tag}_{stamp}_commit_{os.getpid()}"
        quar.mkdir(parents=True)
        (quar / "failed_flags.json").write_text(
            json.dumps({"kind": "forecast_lab_freeze_failed",
                        "verdict": "SNAPSHOT_INVALID",
                        "reason": "code_commit_missing",
                        "created_at": started.isoformat(timespec="seconds")},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        for st in (st_jsonl, st_kfp):
            if st.exists():
                os.replace(st, quar / st.name)
        (quar / "stdout_note.txt").write_text(
            "数据质量门禁：code commit 取不到，三件套断链，快照 INVALID，"
            "未发布任何 canonical 件。\n", encoding="utf-8")
        print("\n[ABORT] SNAPSHOT_INVALID —— code commit 取不到（三件套①缺失）")
        print(f"        留证目录: {quar}")
        return 1

    meta = {
        "kind": "forecast_lab_samples_freeze",
        "schema_version": "2",   # V4.3：+code_commit / stock_data_failures / kfp scope
        "producer": "experiments/forecast_lab/freeze_samples.py",
        "code_path": "backtest_spread.load_samples()  # 生产路径，三态加固已合入(0d3cc69)",
        "created_at": started.isoformat(timespec="seconds"),
        "elapsed_sec": round((datetime.now() - started).total_seconds(), 1),
        "n_samples": len(samples),
        "n_fields": len(samples[0]) if samples else 0,
        "fund_distribution": dict(sorted(dist.items())),
        "date_min": dates[0] if dates else None,
        "date_max": dates[-1] if dates else None,
        "jsonl": out_jsonl.name,
        "sha256": file_sha,
        "n_warnings": len(warn_msgs),
        "degraded_or_ratelimit_flags": flags,
        "kline_fingerprint_sha256": kfp["aggregate_sha256"],
        "kline_fingerprint_file": out_kfp.name,
        "kline_fingerprint_counts": {"stock": kfp["n_stock"], "fund": kfp["n_fund"],
                                     "unreadable": kfp["unreadable"]},
        "kline_fingerprint_scope": {"stock_n": len(stock_universe), "cutoff": cutoff},
        "stock_data_failures": [],   # V4.3 P0-3：非空会已 INVALID，故此处恒为空
        "code_commit": code_commit,  # V4.3：三件套①，冻结时落盘（verify 侧重解析对照）
        # 历史特征时点口径（2026-09-23 诚实化）：本件由 backtest_spread.load_samples()
        # 生产，个股估涨用 d 日 EOD close 近似 14:55 价 ⇒ 历史件恒为 EOD_PROXY。
        # 字段值取自生产真源常量，不在此处另写一份（防两处漂移）。
        "historical_feature_mode": _hist_mode(),
        "publish": {"mode": "atomic_staging", "staging_dir": ".staging"},
    }
    st_meta = staging_dir / out_meta.name
    st_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- 原子发布：staging -> canonical，逐个 os.replace（P0-2）----
    # 中途崩溃最坏留下「部分三件套」：此时 jsonl 已存在，no-clobber 按
    # 「当日已冻结」拒绝静默重采（fail-closed，不冒充完整件），处置走 --force。
    try:
        for st, final in ((st_jsonl, out_jsonl), (st_kfp, out_kfp), (st_meta, out_meta)):
            os.replace(st, final)
    except OSError as exc:
        quar = outdir / f"freeze_failed_{date_tag}_{started.strftime('%H%M%S')}_publish"
        try:
            if not quar.exists():
                os.replace(staging_dir, quar)
        except OSError:
            pass
        print(f"\n[ABORT] 发布阶段落盘失败（{exc}）；staging 已转留证：{quar}")
        return 1
    try:
        staging_dir.rmdir()
    except OSError:
        print(f"  note: staging 目录残留（非空），可手动清理：{staging_dir}")

    print("\n== P1-① 样本冻结结果 ==")
    print(f"  样本数     : {len(samples)}  （预期 ~3343）")
    print(f"  每行字段数 : {meta['n_fields']}")
    print(f"  日期区间   : {meta['date_min']} ~ {meta['date_max']}")
    print(f"  四基金分布 : {meta['fund_distribution']}")
    print(f"  sha256     : {file_sha}")
    print(f"  K线指纹    : {kfp['aggregate_sha256']}（stock={kfp['n_stock']} "
          f"fund={kfp['n_fund']} unreadable={len(kfp['unreadable'])}）-> {out_kfp.name}")
    print(f"  KFP 口径   : universe={len(stock_universe)} 码, cutoff={cutoff}（as-of + universe）")
    print(f"  commit     : {code_commit}")
    print(f"  文件       : {out_jsonl}")
    print(f"  meta       : {out_meta}")
    print(f"  告警数     : {len(warn_msgs)}   用时 {meta['elapsed_sec']}s")
    for m in warn_msgs:
        print(f"  note: {m[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
