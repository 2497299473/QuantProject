"""D-lite 研究池面板生成器（2026-09-16；预注册 forecast_lab_prereg_D_panel_20260914.md §二/§三/§五 的实现）。

回答：把评估面板从「4 只场外基金」扩到「4 基金 + 13 主题 ETF/板块代理」的研究池，
为训练侧与选择侧买统计功效（评估侧仍以生产 4 只为最终留出）。

零网络纪律（预注册 §五 之 1「此后一切读数零网络」）
- 只读 data/klines/、data/stock_klines/、data/sector_klines/ 的**已刷新缓存**；
- 本脚本不发任何 HTTP 请求。缓存刷新由 refresh_panel_cache.py 受控完成（09-16 已跑）。

口径来源（逐条可追溯，不臆猜）
- 面板成员 17 = §一 表：4 基金 + 9 闸门代理 + 3 relaxed 代理 + 1 板块指数。
- 起点 2020-04-27 = §九「本窗口实际跑 D-lite 面板（基金史自 2020-04-27）」→ 全成员统一窗。
- 特征 = §二：a158-lite 50 个纯价格结构特征（复用 features_a158lite，零新写）；
  PIT 口径 series[:i]（截至 T-1）；短窗不足一律 NaN、不缩窗。
- 取价 = §二：代理/板块取 klines 的 close（下标 2）；基金取 navs 单位净值（下标 1）。
- 标签 = §三：fwd5 绝对 = series[t+5]/series[t] − 1；rel5 相对 = 同日**在场成员**横截面超额。
  两把尺子沿用候选 A 的既有约定（forecast_lab_prereg_A_relrank_20260913.md §27-28）：
  rel5_mean = fwd5 − 同日在场 mean(fwd5)；rel5_med = fwd5 − 同日在场 median(fwd5)。
  另附 fwd1/2/3/10/20（沿用 load_samples 的既有标签集 FWD_LIST/EXTRA_FWD，供 T2 与 0910 冻结对齐）。
- 缺失掩码 = B1 协议（run_p1_ablation._mask_row）：每特征双列 (值, 掩码)，
  缺失 → 值填 0.0、掩码 1.0；本 JSONL 只存**值**，掩码由消费方按 B1 协议派生（不落盘以免翻倍）。

产物（同批产出，§五 之 2）
- forecast_outputs/panel_dlite_<date>.jsonl      面板行（canonical：每行 sort_keys）
- forecast_outputs/panel_dlite_<date>.meta.json  侧车（含 sha256、分布、指纹、缺口声明）
- forecast_outputs/kline_fingerprint_<date>_panel.json  同批 K 线指纹（含板块，§五 之 3 扩展）

用法：
  .\\.venv\\Scripts\\python.exe -X utf8 experiments\\forecast_lab\\build_panel_dlite.py
  .\\.venv\\Scripts\\python.exe -X utf8 experiments\\forecast_lab\\build_panel_dlite.py --selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / "experiments" / "forecast_lab"))

from features_a158lite import compute_features, feature_keys      # noqa: E402
from kline_fingerprint import _json_series, _nav_series, build_fingerprint  # noqa: E402

OUT_DIR = BASE / "forecast_outputs"
DATA = BASE / "data"

# ---- §一 面板成员（写死；改这里等于改面板口径，须同步预注册文与报告） ----
FUNDS = ("002112", "002207", "022853", "025687")
GATE_PROXIES = ("512480", "512880", "159915", "512660", "510880",
                "512800", "160225", "512010", "501030")
RELAXED_PROXIES = ("159611", "515220", "159825")
SECTOR_CODES = ("BK0457",)

PANEL_START = "2020-04-27"        # §九：D-lite 起点（全成员统一窗）
HORIZON = 5                       # §三：fwd5 主端点
EXTRA_FWDS = (1, 2, 3, 5, 10, 20)  # 沿用 load_samples 既有标签集
A158_KEYS = feature_keys()


def _load_series() -> dict[str, dict]:
    """读 17 个成员的已缓存序列 → {code: {kind, dates, vals, path, last_date}}。零网络。"""
    out: dict[str, dict] = {}
    for code in FUNDS:
        p = DATA / "klines" / f"{code}.json"
        s = _nav_series(p)
        if not s:
            raise RuntimeError(f"基金缓存不可用：{p}")
        out[code] = {"kind": "fund", "path": p, "series": s}
    for code in GATE_PROXIES + RELAXED_PROXIES:
        p = DATA / "stock_klines" / f"{code}.json"
        s = _json_series(p, "klines")
        if not s:
            raise RuntimeError(f"代理缓存不可用：{p}")
        out[code] = {"kind": "relaxed_proxy" if code in RELAXED_PROXIES else "gate_proxy",
                     "path": p, "series": s}
    for code in SECTOR_CODES:
        p = DATA / "sector_klines" / f"{code}.json"
        s = _json_series(p, "klines")
        if not s:
            raise RuntimeError(f"板块缓存不可用：{p}")
        out[code] = {"kind": "sector", "path": p, "series": s}
    return out


def _build_rows() -> tuple[list[dict], dict]:
    """PIT 生成面板行；随后按日补 rel5 横截面。返回 (rows, stats)。"""
    members = _load_series()
    rows: list[dict] = []
    stats: dict = {"members": {}, "dropped_short_history": 0, "dropped_no_t1": 0,
                   "dropped_incomplete_label": 0}
    for code, m in members.items():
        series = m["series"]
        idx_of = {str(d): i for i, (d, _v) in enumerate(series)}
        kept = 0
        for i, (d, v) in enumerate(series):
            d = str(d)
            if d < PANEL_START:
                continue
            if i < 1:                      # 无 T-1 → 特征全 NaN，不入面板
                stats["dropped_no_t1"] += 1
                continue
            if i + HORIZON >= len(series):  # §三：末日回退 5 个交易日，标签完整才入面板
                stats["dropped_incomplete_label"] += 1
                continue
            hist = [float(x) for _dd, x in series[:i]]
            if len(hist) < 61:             # 最长窗 60 需 61 点，否则全 NaN
                stats["dropped_short_history"] += 1
                continue
            feats = compute_features(hist)
            v = float(v)
            if v <= 0:
                continue
            row = {"member": code, "kind": m["kind"], "date": d, "close": v,
                   "fwd5": float(series[i + HORIZON][1]) / v - 1.0}
            for fwd in EXTRA_FWDS:
                if fwd != HORIZON and i + fwd < len(series):
                    sv = float(series[i + fwd][1])
                    if sv > 0:
                        row[f"fwd{fwd}"] = sv / v - 1.0
            row.update(feats)
            rows.append(row)
            kept += 1
        stats["members"][code] = {"kind": m["kind"], "bars": len(series),
                                 "first": str(series[0][0]), "last": str(series[-1][0]),
                                 "panel_rows": kept, "indexed_dates": len(idx_of)}
    _add_rel5(rows)
    return rows, stats


def _add_rel5(rows: list[dict]) -> None:
    """同日在场成员的横截面超额（两把尺子：均值 / 中位）。就地补列。"""
    by_date: dict[str, list[dict]] = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r)
    for _d, group in by_date.items():
        vals = [r["fwd5"] for r in group]
        mu = sum(vals) / len(vals)
        med = statistics.median(vals)
        for r in group:
            r["rel5_mean"] = r["fwd5"] - mu
            r["rel5_med"] = r["fwd5"] - med
            r["n_present"] = len(vals)


def _canonical_jsonl(rows: list[dict]) -> str:
    """canonical 序列化：行内 sort_keys，行序 (date, member) 稳定 → sha256 可复现。"""
    ordered = sorted(rows, key=lambda r: (r["date"], r["member"]))
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in ordered)


def _t2_vs_frozen(rows: list[dict]) -> dict:
    """T2 单位对齐：面板基金行 vs 0910 冻结，比对身份与**实际共有的**列。"""
    frozen_p = OUT_DIR / "samples_frozen_20260910.jsonl"
    if not frozen_p.exists():
        return {"status": "SKIP", "reason": f"缺 {frozen_p.name}"}
    frozen = {}
    for ln in frozen_p.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            s = json.loads(ln)
            frozen[(s["fund"], s["date"])] = s
    mine = {(r["member"], r["date"]): r for r in rows if r["kind"] == "fund"}
    shared_ids = sorted(set(mine) & set(frozen))
    fd = sorted(f for f in EXTRA_FWDS if any(f"fwd{f}" in s for s in frozen.values()))
    mismatches, maxdiff, n_cmp = [], 0.0, 0
    for k in shared_ids:
        a, b = mine[k], frozen[k]
        for col in [f"fwd{f}" for f in fd]:
            if col not in a or col not in b:
                continue
            n_cmp += 1
            diff = abs(float(a[col]) - float(b[col]))
            maxdiff = max(maxdiff, diff)
            if diff > 1e-9:
                mismatches.append({"member": k[0], "date": k[1], "col": col,
                                   "panel": a[col], "frozen0910": b[col], "absdiff": diff})
    seven = ["est_chg", "est_sign", "composite", "score",
             "breadth", "concentration", "covered_pct"]
    return {"status": "PASS" if not mismatches else "FAIL",
            "identity_shared": len(shared_ids),
            "identity_panel_only": len(set(mine) - set(frozen)),
            "identity_frozen_only": len(set(frozen) - set(mine)),
            "compared_cells": n_cmp, "max_abs_diff": maxdiff,
            "n_mismatch": len(mismatches), "mismatch_sample": mismatches[:5],
            "shared_columns_compared": [f"fwd{f}" for f in fd],
            "missing_shared_columns": seven,
            "missing_reason": ("面板特征空间为 §二 的 a158-50，7 个生产特征来自 lookthrough "
                              "重算（需 228 只股票 K 线 + 基金持仓的网络重取），不在 §五 的"
                              "「17 序列」刷新预算内，故本批不含；T2 的这 7 列待补。")}


def _selftest() -> int:
    ok = fail = 0

    def check(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  [FAIL] {name}")

    # 1) 成员数 = 17，且与 §一 表逐项一致
    allc = list(FUNDS) + list(GATE_PROXIES) + list(RELAXED_PROXIES) + list(SECTOR_CODES)
    check("成员 17 个", len(allc) == 17)
    check("成员无重复", len(set(allc)) == 17)
    check("分层计数 4/9/3/1", (len(FUNDS), len(GATE_PROXIES),
                            len(RELAXED_PROXIES), len(SECTOR_CODES)) == (4, 9, 3, 1))

    # 2) rel5 口径：同日横截面超额，均值/中位两把尺子
    rows = [{"member": "a", "kind": "fund", "date": "2020-04-27", "fwd5": 0.10},
            {"member": "b", "kind": "gate_proxy", "date": "2020-04-27", "fwd5": 0.00},
            {"member": "c", "kind": "gate_proxy", "date": "2020-04-27", "fwd5": -0.10}]
    _add_rel5(rows)
    check("rel5_mean 和为 0", abs(sum(r["rel5_mean"] for r in rows)) < 1e-12)
    check("rel5_med 和为 0", abs(sum(r["rel5_med"] for r in rows)) < 1e-12)
    check("rel5_mean 正确", abs(rows[0]["rel5_mean"] - 0.10) < 1e-12)
    check("rel5_med 正确", abs(rows[0]["rel5_med"] - 0.10) < 1e-12)
    check("n_present = 3", all(r["n_present"] == 3 for r in rows))
    check("居中成员 rel5 = 0", abs(rows[1]["rel5_mean"]) < 1e-12)

    # 3) canonical 序列化确定性（T1 前提）
    r2 = [dict(r) for r in rows]
    check("canonical 两次一致", _canonical_jsonl(rows) == _canonical_jsonl(r2))
    rev = list(reversed(rows))
    check("行序无关（按 date,member 排序）", _canonical_jsonl(rows) == _canonical_jsonl(rev))

    # 4) 零网络：装 socket 守卫实际跑一遍装载路径，任何连接尝试即失败
    #    （不用「扫源码找关键词」——那会把本行的关键词字面量自己判成违规）
    import socket as _socket

    class _NoNet(_socket.socket):
        def __init__(self, *a, **k):
            raise AssertionError("面板生成本路径不得发起网络连接")

    _orig = _socket.socket
    _socket.socket = _NoNet
    try:
        loaded = _load_series()
    except Exception as e:                       # noqa: BLE001
        loaded = None
        check(f"零网络装载（异常 {type(e).__name__}: {e}）", False)
    finally:
        _socket.socket = _orig
    if loaded is not None:
        check("零网络装载 17 成员", len(loaded) == 17)

    # 5) 掩码协议可派生（B1）：缺失 → 值 0 / 掩码 1
    def mask_row(vals):
        out = []
        for v in vals:
            good = v is not None and isinstance(v, (int, float)) and math.isfinite(float(v))
            out += [float(v) if good else 0.0, 0.0 if good else 1.0]
        return out
    mr = mask_row([1.0, float("nan"), None])
    check("B1 掩码：值 0 掩码 1", mr == [1.0, 0.0, 0.0, 1.0, 0.0, 1.0])

    print(f"[build_panel_dlite SELFTEST] {ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--date", default=None, help="覆盖 date tag（默认今日）")
    ap.add_argument("--no-fingerprint", action="store_true", help="跳过同批指纹")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()

    started = datetime.now()
    tag = args.date or started.strftime("%Y%m%d")
    print(f"# D-lite 面板生成 · {tag}（零网络：只读已刷新缓存）")

    rows, stats = _build_rows()
    body = _canonical_jsonl(rows)
    sha1 = hashlib.sha256(body.encode("utf-8")).hexdigest()

    # T1 可复现：同批输入两次生成，sha256 必须逐字节一致
    body2 = _canonical_jsonl(_build_rows()[0])
    sha2 = hashlib.sha256(body2.encode("utf-8")).hexdigest()
    t1_ok = (sha1 == sha2) and (body == body2)

    OUT_DIR.mkdir(exist_ok=True)
    out_jsonl = OUT_DIR / f"panel_dlite_{tag}.jsonl"
    out_jsonl.write_text(body, encoding="utf-8")

    t2 = _t2_vs_frozen(rows)

    kfp_info = None
    if not args.no_fingerprint:
        kfp = build_fingerprint(include_sector=True)     # §五 之 3：面板 + 板块同批
        kfp_p = OUT_DIR / f"kline_fingerprint_{tag}_panel.json"
        kfp_p.write_text(json.dumps(kfp, ensure_ascii=False, indent=1, sort_keys=True),
                         encoding="utf-8")
        kfp_info = {"file": kfp_p.name, "aggregate_sha256": kfp["aggregate_sha256"],
                    "n_stock": kfp["n_stock"], "n_fund": kfp["n_fund"],
                    "n_sector": kfp.get("n_sector"), "schema_version": kfp.get("schema_version")}

    by_kind: dict[str, int] = {}
    by_member: dict[str, int] = {}
    for r in rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        by_member[r["member"]] = by_member.get(r["member"], 0) + 1
    dates = sorted({r["date"] for r in rows})
    widths = {}
    if dates:
        for r in rows:
            widths[r["date"]] = widths.get(r["date"], 0) + 1
        wv = sorted(widths.values())
        width_stats = {"min": wv[0], "median": wv[len(wv) // 2], "max": wv[-1]}
    else:
        width_stats = {}

    meta = {
        "kind": "forecast_lab_panel_dlite", "schema_version": "1",
        "producer": "experiments/forecast_lab/build_panel_dlite.py",
        "created_at": started.isoformat(timespec="seconds"),
        "prereg": "output/forecast_lab_prereg_D_panel_20260914.md",
        "scope": "D-lite", "panel_start": PANEL_START, "zero_network": True,
        "n_rows": len(rows), "n_features": len(A158_KEYS), "n_members": len(stats["members"]),
        "rows_by_kind": dict(sorted(by_kind.items())),
        "rows_by_member": dict(sorted(by_member.items())),
        "date_min": dates[0] if dates else None, "date_max": dates[-1] if dates else None,
        "n_dates": len(dates), "day_width": width_stats,
        "labels_required": [f"fwd{HORIZON}", "rel5_mean", "rel5_med"],
        "labels_optional": [f"fwd{f}" for f in EXTRA_FWDS if f != HORIZON],
        "mask_protocol": "B1（值/掩码双列，缺失 → 值 0 / 掩码 1）；本 JSONL 只存值",
        "sha256_jsonl": sha1,
        "t1_reproducible": t1_ok, "t1_sha256_second": sha2,
        "t2_vs_frozen_0910": t2,
        "kline_fingerprint": kfp_info,
        "build_stats": stats,
    }
    out_meta = OUT_DIR / f"panel_dlite_{tag}.meta.json"
    out_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n== 结果 ==")
    print(f"  面板行数   : {len(rows)}   （跨 {len(dates)} 个交易日）")
    print(f"  日期区间   : {meta['date_min']} ~ {meta['date_max']}")
    print(f"  分层分布   : {meta['rows_by_kind']}")
    print(f"  每日在场宽度: {width_stats}")
    print(f"  特征数     : {len(A158_KEYS)}（a158-lite）")
    print(f"  T1 可复现  : {'PASS' if t1_ok else 'FAIL'}   sha256 {sha1[:16]}…")
    print(f"  T2 对齐    : {t2.get('status')}   共有身份 {t2.get('identity_shared')} 行、"
          f"比对 {t2.get('compared_cells')} 格、max|Δ|={t2.get('max_abs_diff')}")
    if t2.get("status") == "FAIL":
        print(f"    ⚠️ 不一致样本: {t2.get('mismatch_sample')}")
    if kfp_info:
        print(f"  同批指纹   : {kfp_info['aggregate_sha256'][:16]}… "
              f"(stock={kfp_info['n_stock']} fund={kfp_info['n_fund']} sector={kfp_info['n_sector']})"
              f" -> {kfp_info['file']}")
    print(f"  JSONL      : {out_jsonl}")
    print(f"  meta       : {out_meta}")
    print(f"  用时       : {round((datetime.now() - started).total_seconds(), 1)}s")
    return 0 if t1_ok else 1


if __name__ == "__main__":
    sys.exit(main())
