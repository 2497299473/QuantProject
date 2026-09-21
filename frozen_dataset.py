#!/usr/bin/env python3
"""FrozenResearchDataset（V4.3 P0-1，2026-09-20）：回测样本快照统一入口。

背景（GPT 评审 P0-1，NeoHorse 逐行核实属实）：12 个回测/评估脚本各自直调
`backtest_spread.load_samples()`，每跑一次都**活拉**当前缓存；前复权 TTL 重取
会静默追溯改写历史 close（09-18 C2 特征非不变性根因）⇒ 两次「同代码、同日」
跑出两套数字，历史报告无法对数复现。

修复：所有回测**默认强制消费冻结快照**（G-A 硬 / G-B 软，闸门口径与
freeze_verify_tool 完全一致，不另起炉灶）：

- 默认 / --snapshot PATH：消费 canonical 冻结件（forecast_outputs/
  samples_frozen_*.jsonl + meta 侧车 + kline_fingerprint_*.json 三件套）。
  - MISSING（无冻结件且未 --fresh）⇒ fail-closed，调用方 exit 4，
    **拒绝静默活拉兜底**（静默活拉正是本次要修的病灶）。
  - INVALID（G-A 失败：sha/行数/降级征兆/个股K线失败任一不符）⇒ 同上。
  - G-B DRIFTED/INCOMPLETE/UNKNOWN ⇒ 软标记：样本照用，但 report_line 必须写进报告，
    且该轮不得引用历史绝对值作门槛。（V4.3.1：INCOMPLETE＝当前缓存缺/坏码，
    聚合指纹有空洞，比对不成立；scoped 件缺侧车 fail-closed 到 UNKNOWN。）
- --fresh：显式活拉（live_loader()），report_line 标 FRESH，与冻结基线不可比。

三件套契约（与 freeze_verify_tool 同源）：① code_commit ② samples_sha256
（LF 归一，G-A 判定口径）③ K 线聚合指纹（G-B 可比性口径，V4.3 起 scope-aware）。

用法（作为库，回测脚本）：
    from frozen_dataset import resolve_samples
    samples, snap_info = resolve_samples(args.snapshot, args.fresh, BASE_DIR, load_samples)
    if snap_info["mode"] in ("MISSING", "INVALID"):
        return 4                       # fail-closed
    ...
    lines = [..., snap_info["report_line"], ...]   # 报告首行区必须记录样本快照
    # 训练落盘时透传冻结件三元组进 registry（V4.3.1 ③，见 train_forecast_model）：
    eng.save_models(snapshot_provenance=snap_info["snapshot_provenance"])

零网络：本模块只读 forecast_outputs/ 与 data/ 缓存；活拉由调用方显式 --fresh
触发（活拉本身的频控纪律由 load_samples 上游铁律 7/8 管辖，本模块不放宽）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import freeze_verify_tool as _vt  # noqa: E402  复用三件套口径（G-A/G-B 判据、侧车发现、LF 归一）

SNAPSHOT_DIRNAME = "forecast_outputs"
SNAPSHOT_PREFIX = "samples_frozen_"

# V4.3.1 ③：snapshot_provenance 的键序单一真源（空值构造与 registry 白名单
# 一致；改键名只改这一处 + model_registry.register_model 的白名单元组）
PROVENANCE_KEYS = ("snapshot_file", "samples_sha256_lf",
                   "kfp_recorded_sha256", "kfp_current_sha256",
                   "kfp_comparability")


def _empty_provenance(snapshot_file: str | None) -> dict:
    """诚实空 provenance（MISSING/INVALID/FRESH 路径）：未判定的键全 None。"""
    d = {k: None for k in PROVENANCE_KEYS}
    d["snapshot_file"] = snapshot_file
    return d


def latest_frozen_samples(base_dir: Path) -> Path | None:
    """选最新 canonical 冻结件：按文件名日期标签（YYYYMMDD），同标签按 mtime。

    只认 8 位数字标签——`freeze_failed_*` 留证目录、临时件、非规范命名一律
    不选（静默选错件比选不到更危险；选不到走 MISSING fail-closed）。
    """
    outdir = base_dir / SNAPSHOT_DIRNAME
    if not outdir.is_dir():
        return None
    cands = []
    for p in outdir.glob(f"{SNAPSHOT_PREFIX}*.jsonl"):
        tag = p.stem.replace(SNAPSHOT_PREFIX, "")
        if len(tag) == 8 and tag.isdigit():
            cands.append((tag, p.stat().st_mtime, p))
    if not cands:
        return None
    cands.sort(key=lambda t: (t[0], t[1]))
    return cands[-1][2]


def verify_internal(jsonl: Path) -> tuple[bool, str, str]:
    """G-A 内部完整性（硬闸门）：sha256（LF 归一）+ 行数 + 降级征兆 + 个股K线失败。

    口径复用 freeze_verify_tool（lf_normalized_sha256 / resolve_sidecar），
    与 `freeze_verify_tool.py verify` 的 G-A 判据逐字一致——两处判定必须同义，
    否则「verify 过但 resolve 拒」的夹缝会破坏三件套的可信度。
    V4.3.1 ③ 返回 (ok, detail, sha_lf)：sha 同时供 registry provenance 透传
    （第三元素在失败路径也是实际算出的值——指纹记录"实际是什么"）。
    """
    if not jsonl.exists():
        return False, f"样本文件不存在: {jsonl}", ""
    meta_p = _vt.resolve_sidecar(jsonl, None, "meta")
    if meta_p is None:
        return False, f"meta 侧车缺失，G-A 无法判定（fail-closed）: {jsonl.with_suffix('.meta.json')}", ""
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    sha_lf = _vt.lf_normalized_sha256(jsonl)
    rec = str(meta.get("sha256", ""))
    if rec != sha_lf:
        return False, f"样本 sha 不符（记录 {rec[:12]}… / 实际 {sha_lf[:12]}…）", sha_lf
    n_rows = sum(1 for ln in jsonl.read_text(encoding="utf-8").splitlines() if ln.strip())
    if meta.get("n_samples") is not None and int(meta["n_samples"]) != n_rows:
        return False, f"行数不符（meta={meta['n_samples']} / 实际={n_rows}）", sha_lf
    if meta.get("degraded_or_ratelimit_flags"):
        return False, f"冻结时带降级/频控征兆: {meta['degraded_or_ratelimit_flags'][:2]}", sha_lf
    sdfs = meta.get("stock_data_failures")
    if sdfs:
        # V4.3 P0-3：冻结时记录的个股K线失败 ⇒ 快照结构性无效（数据质量门禁）。
        # 冻结侧门禁保证该字段要么缺（旧件）要么为空；存在且非空 = 失败。
        return False, f"冻结时带个股K线失败（{len(sdfs)} 码，数据质量门禁）: {sdfs[:3]}", sha_lf
    return True, f"PASS（sha={sha_lf[:12]}… n={n_rows}）", sha_lf


def verify_comparability(jsonl: Path) -> tuple[str, dict, dict]:
    """G-B 跨版本可比性（软标记）：按与留档**同口径**重算当前 K 线指纹。

    返回 (state, kfp_rec, kfp_now)，state ∈ SAME / DRIFTED / INCOMPLETE / UNKNOWN。
    kfp_now 为重算结果（reject / 重算失败时为 {}）；V4.3.1 ③ 供 registry
    provenance 透传"留档 sha + 重算 sha + 状态"三元组。
    解析与判据 V4.3.1 起复用 freeze_verify_tool.load_kfp_record /
    comparability_state（单一真源，与 CLI `verify` 逐字同义）：

    - V4.3 P0-2：留档带 stock_codes_scope / cutoff 则按同口径重算；
      09-20 前锚点件（无 scope 字段）仍无参全量口径。混口径会改变
      canonical 串 ⇒ 假漂移。
    - V4.3.1 ①：meta 标了 scope（scoped 件）但侧车断链 ⇒ reject，
      fail-closed 判 UNKNOWN，不拿退化口径去猜。
    - V4.3.1 ②：重算侧 unreadable 非空 ⇒ INCOMPLETE（聚合有空洞，
      与留档不具可比性；SAME/DRIFTED 都不可声称）。
    """
    meta_p = _vt.resolve_sidecar(jsonl, None, "meta")
    meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p else {}
    kfp_rec, _kfp_p, reject = _vt.load_kfp_record(jsonl, meta, None)
    if reject is not None:
        print(f"[frozen] G-B kfp 留档 REJECT: {reject}")
        return "UNKNOWN", kfp_rec, {}
    kfp_now = _vt.current_kline_fingerprint(kfp_rec) or {}
    state, detail = _vt.comparability_state(kfp_rec, kfp_now)
    if detail:
        print(f"[frozen] G-B {state}: {detail}")
    return state, kfp_rec, kfp_now


def load_snapshot(jsonl: Path) -> list[dict]:
    """读冻结 jsonl（一行一 JSON，sort_keys —— freeze_samples 的落盘口径）。"""
    return [json.loads(ln) for ln in jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]


def resolve_samples(snapshot: str | None, fresh: bool, base_dir: Path, live_loader) -> tuple[list[dict], dict]:
    """统一样本入口（V4.3 P0-1）。返回 (samples, snap_info)。

    snap_info 键：mode（FROZEN/FRESH/MISSING/INVALID）、file、gate_internal
    （PASS/INVALID/N/A）、gate_comparability（SAME/DRIFTED/INCOMPLETE/UNKNOWN/N/A）、
    snapshot_provenance（V4.3.1 ③：冻结件三元组，供 registry 审计绑定。
    {snapshot_file, samples_sha256_lf, kfp_recorded_sha256, kfp_current_sha256,
    kfp_comparability}；FRESH/未判定路径各键诚实为 None——活拉没有留档）、
    report_line（报告首行区必须原样记录；MISSING/INVALID 时为空串，调用方
    已 fail-closed 不会走到报告）。

    - fresh=True：显式活拉 live_loader()，标 FRESH（与冻结基线不可比）。
    - 否则：消费 snapshot（显式路径）或 latest_frozen_samples()；
      MISSING / INVALID ⇒ ([], info)，调用方 exit 4 fail-closed。
    """
    if fresh:
        samples = live_loader()
        return list(samples), {
            "mode": "FRESH", "file": None,
            "gate_internal": "N/A", "gate_comparability": "N/A",
            "snapshot_provenance": _empty_provenance(None),
            "report_line": "> 样本快照: FRESH 活拉（--fresh 显式）——与冻结基线不可比，"
                           "不得引用历史绝对值作门槛",
        }

    snap = Path(snapshot) if snapshot else None
    if snap is not None and not snap.is_absolute() and not snap.exists():
        cand = base_dir / snap
        if cand.exists():
            snap = cand
    if snap is None:
        snap = latest_frozen_samples(base_dir)
    if snap is None or not snap.exists():
        print("[frozen] MISSING —— 无冻结样本（forecast_outputs/samples_frozen_*.jsonl），"
              "且未指定 --fresh。fail-closed：拒绝静默活拉兜底。")
        print("          处置：先跑 experiments/forecast_lab/freeze_samples.py 冻结，"
              "或显式 --fresh 活拉（数字与冻结基线不可比）。")
        return [], {"mode": "MISSING", "file": str(snap) if snap else None,
                    "gate_internal": "N/A", "gate_comparability": "N/A",
                    "snapshot_provenance": _empty_provenance(str(snap) if snap else None),
                    "report_line": ""}

    print(f"[frozen] 消费冻结件: {snap.name}")
    ok, detail, sha_lf = verify_internal(snap)
    print(f"[frozen] G-A 内部完整性 : {'PASS' if ok else 'INVALID'}（{detail}）")
    if not ok:
        print("[frozen] VERDICT: INVALID —— 快照完整性失败，fail-closed，拒绝继续。")
        prov = _empty_provenance(snap.name)
        prov["samples_sha256_lf"] = sha_lf or None   # 实际算出的 sha 照记（非伪造值）
        return [], {"mode": "INVALID", "file": snap.name,
                    "gate_internal": "INVALID", "gate_comparability": "N/A",
                    "snapshot_provenance": prov, "report_line": ""}

    state, _kfp_rec, kfp_now = verify_comparability(snap)
    if state in ("DRIFTED", "INCOMPLETE"):
        print(f"[frozen] G-B K线指纹    : {state}（本轮数字与锚定历史轮次**不可比**，"
              "报告必须原样记录且不得引用历史绝对值作门槛）")
    else:
        print(f"[frozen] G-B K线指纹    : {state}")

    samples = load_snapshot(snap)
    note = "" if state == "SAME" else f"；K线指纹 {state}，与历史锚点不可比"
    # V4.3.1 ③：三元组照写全，不论闸门状态（指纹记"实际消费的是什么"）。
    prov = {
        "snapshot_file": snap.name,
        "samples_sha256_lf": sha_lf,
        "kfp_recorded_sha256": str(_kfp_rec.get("aggregate_sha256") or "") or None,
        "kfp_current_sha256": str(kfp_now.get("aggregate_sha256") or "") or None,
        "kfp_comparability": state,
    }
    return samples, {
        "mode": "FROZEN", "file": snap.name,
        "gate_internal": "PASS", "gate_comparability": state,
        "snapshot_provenance": prov,
        "report_line": f"> 样本快照: FROZEN `{snap.name}` · G-A PASS · G-B {state}{note}",
    }
