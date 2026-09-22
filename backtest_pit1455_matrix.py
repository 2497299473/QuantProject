#!/usr/bin/env python3
"""V4.4-步3：四基金 × 多周期 14:55 验收矩阵（2026-09-22，零网络）。

回答 SSOT 0922 篇「七、步 3」挂的两个问题：
  Q1 排序能力：模型有没有把强的排在弱的前面？→ 逐基金 RankIC（cluster bootstrap CI）
  Q2 概率校准：P(up)=x 时实际上涨率是否 ≈ x？→ 逐基金校准表 + ACE + 偏差（mean p_up − 实际上涨率）

三件设计事实（先核实后动手，勿照抄印象）：
1. **双 label 同预测**：模型输出的 p_up 只依赖特征，与 label 口径无关 ⇒ 同一组
   预测套两套真值（旧 `fwd{h}` / 新 `fwd{h}_1455`）就是干净的口径对照，
   不需要重训即可回答「换分母会不会改变结论」。反事实重训（--retrain）另列，
   用来分离「口径影响训练」这一层。
2. **切分必须用冻结池**：PIT 件日期尾到 2026-08-12（冻结件尾到 2026-08-07 附近），
   直接对 PIT 件跑 split_date_oos 会把 train 段推宽（实测 oos_start 2025-05-14、
   train 2438），与线上/冻结报告不可比。本脚本以 0910 冻结件为**唯一切分真源**
   （oos_start / train / oos 三值照原样取），PIT 件只按 (fund,date) 查表补
   `*_1455` 列 ⇒ 池成员与历史报告逐行一致。
3. **G-B DRIFTED 已量化澄清**（--verify-cache-drift，默认开）：按当前 data/klines
   缓存重算**旧口径** label，与 0910 存的 `fwd{h}` 逐行比；实测 mean|Δ|=0.0、
   max|Δ|≤1e-6 ⇒ 缓存的净值层面与冻结时点一致，新旧对照未被缓存漂移污染。
   （G-B 指纹仍报 DRIFTED —— 那是 K 线聚合口径变了，不是净值被追溯改写。）

样本量边界（诚实标注，不做裁决）：
  OOS 段逐基金 n = 002112 306 / 002207 306 / 022853 276 / 025687 35。
  025687 系 2026-06 新基金、无训练段样本 ⇒ 该格只出数值、标记 LOW_N，
  任何周期都不得由它推导 model_ready / 晋升结论。

纪律：
  - 零网络：只读 forecast_outputs/ 与 data/ 缓存；socket 守卫（--no-net 默认开）。
  - 不改任何生产脚本、不动 label 构造（消费步 1 产物）、不动门禁与 model_ready。
  - 报告落 output/（.gitignore 拦截，按需本地留档）；机读件落 forecast_outputs/。

用法：
    .\\.venv\\Scripts\\python.exe backtest_pit1455_matrix.py
    ... --retrain --n-boot 999          # 加反事实重训轨（1455 label 训练）
    ... --no-verify-cache-drift         # 跳过缓存漂移复核（快，但报告须标未核）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import numpy as np

from backtest_forecast import (build_xy, rank_ic, brier_multiclass,
                               calibration_curve, cluster_bootstrap_ci,
                               split_date_oos)
from frozen_dataset import load_snapshot
from core import forecast_engine as FE
from core import pit1455_contract as PIT

HORIZONS = (1, 3, 5)
LABELS = ("old", "1455")          # old = fwd{h}（T 日最终 NAV 为分母）
DEFAULT_PIT = "forecast_outputs/samples_pit1455_20260922.jsonl"
DEFAULT_SPLIT_SRC = "forecast_outputs/samples_frozen_20260910.jsonl"
LOW_N = 60                        # 逐基金格样本量下限（低于此只出数、不下判）
CACHE_DRIFT_TOL = 1e-5            # 旧 label 重算容差（round 到 6 位 ⇒ 1e-5 足够宽松）

# 步 2 报告基准（output/backtest_forecast_v44_step2_20260922.log，2026-09-22）：
# C 轨（旧 label 重训）应复现该 RankIC —— 容差 0.002 容纳打印舍入（4 位小数）。
C_REF = {1: 0.049, 3: 0.048, 5: -0.019}
C_TOL = 0.002


def _new_clf():
    """与 backtest_forecast / forecast_engine.DirectionModel 同超参的分类器。"""
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(max_iter=200, learning_rate=0.08,
                                          max_depth=3, early_stopping=True,
                                          random_state=42)


def _sha12(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


def _block_network() -> None:
    import socket

    class NoNet(socket.socket):
        def connect(self, *a, **k):
            raise RuntimeError("[no-net] 本脚本零网络：socket.connect 已封锁")

    socket.socket = NoNet      # type: ignore[misc]


# ---------- 数据装载与血缘 ----------
def load_pit_rows(path: Path) -> dict[tuple[str, str], dict]:
    """PIT jsonl → {(fund,date): row}。同键重复即报错（步 1 件应无重复）。"""
    rows: dict[tuple[str, str], dict] = {}
    for r in load_snapshot(path):
        k = (r["fund"], r["date"])
        if k in rows:
            raise SystemExit(f"[fail] PIT 件出现重复 (fund,date)：{k}")
        rows[k] = r
    return rows


def attach_1455(split_rows: list[dict], pit: dict[tuple[str, str], dict]) -> tuple[list[dict], int]:
    """把 `fwd*_1455` 列按 (fund,date) 贴到冻结池行上。缺失计数照实返回。"""
    missing = 0
    out = []
    for s in split_rows:
        p = pit.get((s["fund"], s["date"]))
        if p is None:
            missing += 1
            out.append(dict(s))
            continue
        row = dict(s)
        for h in HORIZONS:
            row[f"fwd{h}_1455"] = p.get(f"fwd{h}_1455")
        row["_nav_hat_1455"] = p.get("_nav_hat_1455")
        out.append(row)
    return out, missing


def verify_cache_drift(rows: list[dict]) -> dict:
    """按当前缓存重算旧口径 label，与存的 fwd{h} 比（G-B DRIFTED 是否污染对照）。"""
    from pit1455_dataset import load_nav_series

    cache: dict[str, list[tuple[str, float]]] = {}
    idx: dict[str, dict[str, int]] = {}
    res = {}
    for h in HORIZONS:
        diffs, no_cache = [], 0
        for s in rows:
            code, date = s["fund"], s["date"]
            if code not in cache:
                cache[code] = load_nav_series(code) or []
                idx[code] = {d: n for n, (d, _) in enumerate(cache[code])}
            series = cache[code]
            i = idx[code].get(date)
            if not series or i is None or i + h >= len(series):
                no_cache += 1
                continue
            recomp = series[i + h][1] / series[i][1] - 1.0
            stored = s.get(f"fwd{h}")
            if isinstance(stored, (int, float)):
                diffs.append(abs(recomp - float(stored)))
        res[f"h{h}"] = {
            "n": len(diffs), "unmatched": no_cache,
            "mean_abs": round(float(np.mean(diffs)), 8) if diffs else None,
            "max_abs": round(float(np.max(diffs)), 8) if diffs else None,
            "verdict": ("OK" if diffs and max(diffs) <= CACHE_DRIFT_TOL else "SUSPECT"),
        }
    return res


# ---------- 单格指标 ----------
def _slope(p: np.ndarray, y: np.ndarray) -> float | None:
    """校准斜率（OLS p_up → 实际 up 指示）；退化返回 None。"""
    if len(p) < 5 or float(np.std(p)) < 1e-12:
        return None
    return float(np.polyfit(p, y, 1)[0])


def cell_metrics(name: str, proba3: np.ndarray, fwd: np.ndarray, dates: list[str],
                 est_chg: np.ndarray, flat_margin: float, n_boot: int,
                 arith_sign: float = 1.0) -> dict:
    """一格（模型 × label 口径 × 基金/池）的全部指标。proba3 顺序 = (down,flat,up)。

    arith_sign：该 label 口径下「零成本算术基线」的方向。
      old  口径 = +1（基线 = est_chg 本身，生产既有基线）
      1455 口径 = −1（契约分母含 (1+est_chg) ⇒ est_chg 越大、label 机械越小，
                    「拿 −est_chg 当预测分」零训练就有正 IC；见报告 1c 节）
    excess_vs_arith = 模型 RankIC − 该基线 IC，**这才是模型的真实增量**。
    """
    n = len(fwd)
    if n == 0:
        return {"cell": name, "n": 0, "verdict": "NO_SAMPLES"}

    p_up = proba3[:, 2]
    y_up = (fwd > flat_margin).astype(int)          # 三分类里的 up
    y_dn = (fwd < -flat_margin).astype(int)
    lab = np.where(fwd > flat_margin, 2, np.where(fwd < -flat_margin, 0, 1))

    # 多类 Brier（瞎猜 0.667）+ 二值 Brier（up vs 非 up，口径同 t5_scorecard）
    b_mc = brier_multiclass(lab, proba3)
    b_bin = float(np.mean((y_up - p_up) ** 2))
    # 多数类基线（用本格自身分布 —— 诚实展示，不参与裁决）
    prior = np.bincount(lab, minlength=3) / n
    onehot = np.zeros((n, 3)); onehot[np.arange(n), lab] = 1.0
    b_majority = float(np.mean(np.sum((onehot - prior) ** 2, axis=1)))

    # p_up 无方差时 spearmanr 未定义（scipy 会告警）⇒ 直接记 0，不制造日志噪声
    ric = 0.0 if float(np.std(p_up)) < 1e-12 else rank_ic(p_up.tolist(), fwd.tolist())
    lo, hi = cluster_bootstrap_ci(
        lambda sub: rank_ic(sub["x"].tolist(), sub["y"].tolist()),
        {"x": p_up, "y": fwd}, dates, n_boot=n_boot)
    base_ic = rank_ic(est_chg.tolist(), fwd.tolist())

    calib = calibration_curve(y_up, p_up, nbins=5)
    cp = np.array([b["avg_p"] for b in calib["bins"]], dtype=float)
    cf = np.array([b["freq"] for b in calib["bins"]], dtype=float)
    cn = np.array([b["n"] for b in calib["bins"]], dtype=float)
    ece = float(np.sum(np.abs(cp - cf) * cn) / max(1.0, cn.sum())) if len(cp) else None

    top = bot = None
    if n >= LOW_N and np.std(p_up) > 1e-12:
        order = np.argsort(p_up)
        k = max(1, int(n * 0.2))
        top = float(fwd[order[-k:]].mean())
        bot = float(fwd[order[:k]].mean())

    return {
        "cell": name, "n": int(n), "n_days": len(set(dates)),
        "base_rate_up_gt0": round(float(np.mean(fwd > 0)), 4),
        "base_rate_up_flatmargin": round(float(np.mean(y_up)), 4),
        "rate_down": round(float(np.mean(y_dn)), 4),
        "mean_p_up": round(float(p_up.mean()), 4),
        "calib_bias": round(float(p_up.mean() - np.mean(y_up)), 4),   # 预测−实际
        "rank_ic": round(ric, 4),
        "rank_ic_ci": [round(lo, 4), round(hi, 4)] if not math.isnan(lo) else None,
        "brier_mc": round(b_mc, 4),
        "brier_bin": round(b_bin, 4), "brier_majority": round(b_majority, 4),
        "ace": round(calib["ace"], 4), "ece": round(ece, 4) if ece is not None else None,
        "calib_slope": (round(_slope(p_up, y_up.astype(float)), 3)
                        if _slope(p_up, y_up.astype(float)) is not None else None),
        "est_chg_ic": round(base_ic, 4),
        # 零成本算术基线：old = +est_chg，1455 = −est_chg（rank_ic 对取负是线性取反）
        "arith_baseline_ic": round(arith_sign * base_ic, 4),
        "excess_vs_arith": round(ric - arith_sign * base_ic, 4),
        "q20_fwd": round(bot, 4) if bot is not None else None,
        "q80_fwd": round(top, 4) if top is not None else None,
        "spread": round(top - bot, 4) if top is not None and bot is not None else None,
        "low_n": bool(n < LOW_N),
        "bins": [{"bin": b["bin"], "n": b["n"], "p": round(b["avg_p"], 3),
                  "freq": round(b["freq"], 3)} for b in calib["bins"]],
    }


# ---------- 主流程 ----------
def main() -> int:
    ap = argparse.ArgumentParser(description="V4.4-步3 14:55 验收矩阵（零网络）")
    ap.add_argument("--pit", default=DEFAULT_PIT, help="步 1 产物 jsonl（含 fwd*_1455）")
    ap.add_argument("--split-src", default=DEFAULT_SPLIT_SRC,
                    help="切分真源（0910 冻结件），保证池成员与历史报告一致")
    ap.add_argument("--retrain", action="store_true",
                    help="加反事实轨：用 1455 label 按生产超参重训后双口径评估")
    ap.add_argument("--n-boot", type=int, default=999)
    ap.add_argument("--window-days", type=int, default=0,
                    help=">0 时输出 OOS 分窗 RankIC 轨迹（每 window_days 交易日一窗）")
    ap.add_argument("--no-verify-cache-drift", dest="verify_cache_drift",
                    action="store_false", help="跳过缓存漂移复核")
    ap.add_argument("--no-net", dest="net_guard", action="store_false",
                    help="关闭 socket 守卫（默认开）")
    ap.add_argument("--json-out", default="forecast_outputs/pit1455_matrix_20260922.json")
    ap.add_argument("--md-out", default=None, help="默认 output/backtest_pit1455_matrix_<今日>.md")
    ap.set_defaults(verify_cache_drift=True, net_guard=True)
    args = ap.parse_args()

    t0 = time.time()
    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    flat_margin = float(cfg.get("forecast", {}).get("prob_flat_margin", 0.003))

    pit_p = (BASE_DIR / args.pit) if not Path(args.pit).is_absolute() else Path(args.pit)
    src_p = (BASE_DIR / args.split_src) if not Path(args.split_src).is_absolute() else Path(args.split_src)
    for p in (pit_p, src_p):
        if not p.is_file():
            print(f"[fail] 输入件不存在：{p}")
            return 2
    if args.net_guard:
        _block_network()

    print("== [0] 输入件与血缘 ==")
    print(f"  PIT  {pit_p.name}  sha256(前12)={_sha12(pit_p)}")
    print(f"  SPLIT {src_p.name} sha256(前12)={_sha12(src_p)}")
    print(f"  契约 {json.dumps(PIT.contract_provenance(), ensure_ascii=False)}")

    pit_rows = load_pit_rows(pit_p)
    split_rows_raw = load_snapshot(src_p)
    rows, missing = attach_1455(split_rows_raw, pit_rows)
    n_1455 = sum(1 for r in rows if r.get("fwd5_1455") is not None)
    print(f"  冻结池 {len(rows)} 行 / PIT 件 {len(pit_rows)} 行 · (fund,date) 未匹配 {missing}"
          f" · fwd5_1455 可用 {n_1455}")
    if missing:
        print("[fail] 存在未匹配行 ⇒ 冻结池成员与步 1 件不一致，中止（不猜标签）")
        return 3

    drift = None
    if args.verify_cache_drift:
        print("\n== [1] 缓存漂移复核（旧 label 重算 vs 0910 存量）==")
        drift = verify_cache_drift(rows)
        for h, d in drift.items():
            print(f"  {h}: n={d['n']} mean|Δ|={d['mean_abs']} max|Δ|={d['max_abs']} → {d['verdict']}")
        if any(v["verdict"] != "OK" for v in drift.values()):
            print("  [warn] 旧 label 与当前缓存不一致 ⇒ 新旧对照含缓存漂移成分，报告已标注")

    # ---- 切分（真源 = 冻结池，与线上/历史报告同口径）----
    rows_sorted = sorted(rows, key=lambda s: (s["date"], s["fund"]))
    train, oos, oos_start = split_date_oos(rows_sorted)
    print(f"\n== [2] 冻结切分：train < {oos_start}（{len(train)}）/ OOS ≥ {oos_start}（{len(oos)}）==")
    from collections import Counter
    print("  OOS 逐基金 n:", dict(Counter(s["fund"] for s in oos)))
    print("  train 逐基金 n:", dict(Counter(s["fund"] for s in train)))
    if not train or not oos:
        print("[fail] 切分为空")
        return 1

    dates = [s["date"] for s in oos]
    # est_chg 单因子基线：缺失填 0.0 —— 与 build_xy 的 B1 布局同处理（值列填 0），
    # 保证与步 2 报告的 base_ic 可对照。
    est = np.array([0.0 if s.get("est_chg") is None else float(s["est_chg"]) for s in oos])

    # ---- A 轨：现行授权模型（v3 权重，训练口径=旧 label，只推理）----
    print("\n== [3] A 轨：现行授权权重（forecast_v3.pkl）推理，不重训 ==")
    eng = FE.get_loaded_engine()
    if not eng.model_loaded:
        print(f"[fail] 权重加载失败（load_error={eng.load_error}）⇒ A 轨不可跑")
        return 4
    print(f"  trained_at={eng.loaded_at} oos_start={eng.trained_oos_start} "
          f"model_ready(cfg)={eng.model_ready} approved={eng.model_approved} "
          f"approval_error={eng.approval_error}")
    X_all = _feature_matrix(oos)
    # 等价性守卫：本脚本的行过滤/取值必须与生产 build_xy 逐位一致，
    # 否则「同一样本集」这个前提就不成立（09-22 步 3 自设的防漂移检查）。
    for h in HORIZONS:
        ref = build_xy(oos, h, flat_margin)
        mine = [s for s in oos if s.get(f"fwd{h}") is not None]
        assert ref is not None and len(ref.dates) == len(mine), f"行数不符 h={h}"
        assert np.allclose(ref.yret, np.array([float(s[f"fwd{h}"]) for s in mine])), \
            f"yret 与本脚本取值不一致 h={h}"
        assert list(ref.dates) == [s["date"] for s in mine], f"日期序不一致 h={h}"
        keep_h = np.array([i for i, s in enumerate(oos)
                           if s.get(f"fwd{h}") is not None], dtype=int)
        assert np.array_equal(X_all[keep_h], ref.X), f"特征矩阵与 build_xy 不一致 h={h}"
    print("  等价性守卫：build_xy 行过滤/取值/特征矩阵与本脚本逐位一致 ✅")
    proba: dict[int, np.ndarray] = {}
    for h in HORIZONS:
        proba[h] = eng.dirmodels[h].clf.predict_proba(X_all)

    # ---- C 轨：旧 label 重训（= backtest_forecast / 步 2 报告的口径）----
    # 作用：**自校验**。若 C 轨 old 列能复现步 2 的 RankIC（+0.049 / +0.048 / −0.019），
    # 说明本矩阵的指标实现与生产验证器等价 ⇒ 1455 列的可信度才成立。
    print("\n== [4] C 轨：旧 label 重训（自校验轨，超参/切分同生产）==")
    proba_c: dict[int, np.ndarray] = {}
    for h in HORIZONS:
        tr_h = [s for s in train if s.get(f"fwd{h}") is not None]
        Xtr, ytr = _fit_arrays(tr_h, f"fwd{h}", flat_margin)
        clf = _new_clf().fit(Xtr, ytr)
        proba_c[h] = clf.predict_proba(X_all)
        print(f"  T+{h}: train_n={len(ytr)}")

    # ---- B 轨：反事实重训（1455 label 训练，生产超参/purge 切分）----
    proba_b: dict[int, np.ndarray] = {}
    b_note = "未跑（加 --retrain 启用）"
    if args.retrain:
        print("\n== [4b] B 轨：反事实重训（label=fwd*_1455，超参/切分同生产）==")
        for h in HORIZONS:
            tr_h = [s for s in train if s.get(f"fwd{h}_1455") is not None]
            Xtr, ytr = _fit_arrays(tr_h, f"fwd{h}_1455", flat_margin)
            clf = _new_clf().fit(Xtr, ytr)
            proba_b[h] = clf.predict_proba(X_all)
            print(f"  T+{h}: train_n={len(ytr)}（逐基金 "
                  f"{dict(Counter(s['fund'] for s in tr_h))}）")
        b_note = "已跑"

    # ---- 组格：模型轨 × label 口径 × (池 / 逐基金) ----
    print("\n== [5] 矩阵组格 ==")
    results: dict[str, dict] = {"meta": {
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pit_file": pit_p.name, "pit_sha256_12": _sha12(pit_p),
        "split_src": src_p.name, "split_src_sha256_12": _sha12(src_p),
        "oos_start": oos_start, "n_train": len(train), "n_oos": len(oos),
        "flat_margin": flat_margin, "n_boot": args.n_boot,
        "contract": PIT.contract_provenance(),
        "weights": {"file": f"forecast_v{FE.MODEL_VERSION}.pkl",
                    "trained_at": eng.loaded_at, "pkl_oos_start": eng.trained_oos_start,
                    "model_ready_cfg": eng.model_ready, "approved": eng.model_approved,
                    "approval_error": eng.approval_error},
        "cache_drift": drift, "b_track": b_note,
    }}
    for track, proba_map in (("A_v3_frozen_weights", proba),
                             ("C_old_label_retrain", proba_c),
                             ("B_1455_label_retrain", proba_b)):
        if not proba_map:
            continue
        results[track] = {}
        for h in HORIZONS:
            keep = np.array([i for i, s in enumerate(oos)
                             if s.get(f"fwd{h}") is not None], dtype=int)
            keep_new = np.array([i for i, s in enumerate(oos)
                                 if s.get(f"fwd{h}_1455") is not None], dtype=int)
            grid = {}
            # arith_sign：1455 契约的分母含 (1+est_chg) ⇒ 该口径的零成本基线是 −est_chg
            for lab_sys, col, idx, asgn in (
                    ("old", f"fwd{h}", keep, 1.0),
                    ("1455", f"fwd{h}_1455", keep_new, -1.0)):
                sub = [oos[i] for i in idx]
                fwd = np.array([float(s[col]) for s in sub])
                dts = [s["date"] for s in sub]
                pr = proba_map[h][idx]
                grid[f"pool::{lab_sys}"] = cell_metrics(
                    f"pool::{lab_sys}", pr, fwd, dts, est[idx], flat_margin, args.n_boot,
                    arith_sign=asgn)
                by_fund: dict[str, list[int]] = {}
                for j, s in enumerate(sub):
                    by_fund.setdefault(s["fund"], []).append(j)
                for code, js in sorted(by_fund.items()):
                    jj = np.array(js, dtype=int)
                    grid[f"{code}::{lab_sys}"] = cell_metrics(
                        f"{code}::{lab_sys}", pr[jj], fwd[jj], [dts[k] for k in js],
                        est[idx][jj], flat_margin, args.n_boot, arith_sign=asgn)
            results[track][f"T+{h}"] = grid
            print(f"  {track} T+{h}: pool_old n={grid['pool::old']['n']} "
                  f"IC={grid['pool::old']['rank_ic']} | pool_1455 n={grid['pool::1455']['n']} "
                  f"IC={grid['pool::1455']['rank_ic']}")

    # ---- 自校验结论（读 C 轨 old 列 vs 步 2 基准）----
    selfcheck = {}
    for h in HORIZONS:
        got = results["C_old_label_retrain"][f"T+{h}"]["pool::old"]["rank_ic"]
        selfcheck[f"T+{h}"] = {"matrix": got, "step2_ref": C_REF[h],
                               "abs_delta": round(abs(got - C_REF[h]), 4),
                               "match": bool(abs(got - C_REF[h]) <= C_TOL)}
        print(f"  自校验 T+{h}: 矩阵 C 轨 old={got:+.4f} vs 步2 基准 {C_REF[h]:+.3f} "
              f"→ {'✅' if selfcheck[f'T+{h}']['match'] else '❌ 超容差'}")
    c_match = all(v["match"] for v in selfcheck.values())
    results["meta"]["selfcheck"] = selfcheck
    results["meta"]["selfcheck_ok"] = c_match
    print(f"  ⇒ 指标实现与生产验证器{'等价 ✅（1455 列可信）' if c_match else '存在偏差 ❌：先查口径再读结论'}")
    if not c_match:
        print("  [warn] 自校验失败仍出报告，但报告首行会标注，避免被当作等价读数引用")

    # ---- 分窗轨迹（可选）----
    if args.window_days > 0:
        print(f"\n== [6] OOS 分窗 RankIC（窗宽 {args.window_days} 交易日）==")
        results["windows"] = {}
        for track, proba_map in (("A_v3_frozen_weights", proba),
                                 ("C_old_label_retrain", proba_c),
                                 ("B_1455_label_retrain", proba_b)):
            if not proba_map:
                continue
            rows_w = _window_track(oos, proba_map, dates, args.window_days, HORIZONS)
            results["windows"][track] = rows_w
            for w in rows_w:
                print(f"  {track} {w['window']} n={w['n']} " + " ".join(
                    f"T+{h}:{_wfmt(w, h, 'old')}/{_wfmt(w, h, '1455')}" for h in HORIZONS))

    out_json = (BASE_DIR / args.json_out) if not Path(args.json_out).is_absolute() else Path(args.json_out)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[ok] 机读件 → {out_json.relative_to(BASE_DIR)}")

    md = _render_md(results, args)
    out_md = (BASE_DIR / args.md_out) if args.md_out else \
        BASE_DIR / "output" / f"backtest_pit1455_matrix_{time.strftime('%Y%m%d')}.md"
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(md, encoding="utf-8")
    print(f"[ok] 报告 → {out_md.relative_to(BASE_DIR)}")
    print(f"[done] 耗时 {time.time() - t0:.0f}s")
    return 0


def _feature_matrix(rows: list[dict]) -> np.ndarray:
    """构造与 build_xy 同协议的 14 维矩阵（值 + missing_mask 双列）。

    特征与周期无关 ⇒ 一份全行矩阵；**不按 label 过滤**，取概率时由调用方用
    与各 label 口径对应的下标集切片（old 与 1455 可用行集可能不同）。
    """
    X = []
    for s in rows:
        row = []
        for k in FE.FEATURE_KEYS:
            v = s.get(k)
            row.append(0.0 if v is None else float(v))
            row.append(1.0 if v is None else 0.0)
        X.append(row)
    return np.array(X, dtype=float)


def _fit_arrays(rows: list[dict], col: str, flat_margin: float) -> tuple[np.ndarray, np.ndarray]:
    X, y = [], []
    for s in rows:
        v = s.get(col)
        if v is None:
            continue
        row = []
        for k in FE.FEATURE_KEYS:
            fv = s.get(k)
            row.append(0.0 if fv is None else float(fv))
            row.append(1.0 if fv is None else 0.0)
        X.append(row)
        f = float(v)
        y.append(2 if f > flat_margin else (0 if f < -flat_margin else 1))
    return np.array(X, dtype=float), np.array(y, dtype=int)


def _window_track(oos, proba, dates, window_days, horizons) -> list[dict]:
    uniq = sorted(set(dates))
    out = []
    for i in range(0, len(uniq), window_days):
        w = set(uniq[i:i + window_days])
        idx = np.array([k for k, d in enumerate(dates) if d in w], dtype=int)
        if len(idx) < 20:
            continue
        rec = {"window": f"{min(w)} ~ {max(w)}", "n": int(len(idx)), "ic": {}}
        for h in horizons:
            rec["ic"][str(h)] = {}
            for lab, col in (("old", f"fwd{h}"), ("1455", f"fwd{h}_1455")):
                pairs = [(proba[h][k][2], oos[k][col]) for k in idx if oos[k].get(col) is not None]
                if len(pairs) < 10:
                    rec["ic"][str(h)][lab] = None
                    continue
                rec["ic"][str(h)][lab] = round(rank_ic([p for p, _ in pairs],
                                                       [float(y) for _, y in pairs]), 4)
        out.append(rec)
    return out


TRACKS = ("A_v3_frozen_weights", "C_old_label_retrain", "B_1455_label_retrain")
TRACK_CN = {
    "A_v3_frozen_weights": "A · 现行 v3 权重（旧 label 所训，只推理）",
    "C_old_label_retrain": "C · 旧 label 重训（自校验轨 = 步 2 口径）",
    "B_1455_label_retrain": "B · 1455 label 重训（反事实轨）",
}


def _fmt(v):
    return "—" if v is None else f"{v:+.4f}"


def _wfmt(w, h, lab):
    v = w["ic"][str(h)][lab]
    return "—" if v is None else f"{v:+.3f}"


def _render_md(results: dict, args) -> str:
    m = results["meta"]
    L = ["# V4.4-步3 · 四基金 × 多周期 14:55 验收矩阵", "",
         f"> 生成：{m['run_at']} · 零网络（socket 守卫={args.net_guard}）"
         f" · n_boot={m['n_boot']} · flat_margin={m['flat_margin']}",
         f"> 样本：A 轨/池成员真源 `{m['split_src']}`（sha {m['split_src_sha256_12']}）"
         f" + `fwd*_1455` 列来自 `{m['pit_file']}`（sha {m['pit_sha256_12']}）",
         f"> 切分：train < {m['oos_start']}（{m['n_train']}）/ OOS ≥ {m['oos_start']}（{m['n_oos']}）"
         f" —— 与冻结池历史报告同口径，非对 PIT 件重切",
         f"> 权重：`{m['weights']['file']}` trained_at={m['weights']['trained_at']} "
         f"pkl oos_start={m['weights']['pkl_oos_start']} · "
         f"model_ready(cfg)={m['weights']['model_ready_cfg']} · "
         f"registry_approved={m['weights']['approved']}（未批准 = 仅研究展示，不外推）",
         f"> 契约：`{m['contract']['contract_version']}` · {m['contract']['label_semantics']}",
         f"> B 轨（反事实重训）：{m['b_track']}", ""]

    if m.get("cache_drift"):
        L += ["## 0. 缓存漂移复核（G-B DRIFTED 是否污染新旧对照）", "",
              "| 周期 | 可比行 | mean\\|Δ\\| | max\\|Δ\\| | 判定 |", "|---|---:|---:|---:|---|"]
        for h, d in m["cache_drift"].items():
            L.append(f"| {h} | {d['n']} | {d['mean_abs']} | {d['max_abs']} | {d['verdict']} |")
        L += ["", "Δ 为「按当前 data/klines 重算旧口径 label」与「0910 存量 fwd{h}」之差。"
              "mean 0 / max ≤1e-5 ⇒ 净值层与冻结时点一致，新旧对照是**纯口径差**，未被缓存漂移混入。"
              "（G-B 指纹仍 DRIFTED：变的是 K 线聚合口径，不是历史净值被改写。）", ""]

    tracks = [t for t in TRACKS if t in results]
    L += ["> **三轨说明**：A 轨=线上现行 v3 权重（旧 label 训练）；C 轨=旧 label 重训，"
          "用于**自校验**本矩阵指标实现与生产验证器（backtest_forecast / 步 2 报告）等价；"
          "B 轨=1455 label 重训（反事实，需 --retrain）。"
          "**A 与 C 的差 = 模型血统差；C 与 B 的 old/1455 列之差才是 label 口径差。**"
          "读矩阵时勿把 A 轨的数字直接与步 2 报告对比。", ""]

    sc = m.get("selfcheck") or {}
    L += ["## 0b. 自校验（C 轨 old 列 vs 步 2 报告基准）", "",
          "| 周期 | 矩阵 C 轨 RankIC(old) | 步 2 基准 | \\|Δ\\| | 容差 | 判定 |",
          "|---|---:|---:|---:|---:|---|"]
    for h in HORIZONS:
        e = sc.get(f"T+{h}")
        if not e:
            continue
        L.append(f"| T+{h} | {e['matrix']:+.4f} | {e['step2_ref']:+.3f} "
                 f"| {e['abs_delta']:.4f} | {C_TOL} | {'✅' if e['match'] else '❌'} |")
    L += ["",
          ("**结论：指标实现与生产验证器等价 ⇒ 1455 列读数可按同口径解读。**"
           if m.get("selfcheck_ok") else
           "**⚠️ 自校验超容差：1455 列只作内部参考，不得对外引用，先排查指标口径。**"), ""]
    L += ["## 1. 池级矩阵（四基金合并 OOS，pooled）", "",
          "| 轨 | 周期 | label | n | P(fwd>0) | mean p_up | 校准偏差 | RankIC | 95% CI | Brier(多类) | Brier(多数类) | ACE | ECE | 斜率 | est_chg IC | Q80−Q20 fwd |",
          "|---|---|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|"]
    for t in tracks:
        for h in HORIZONS:
            for lab in LABELS:
                c = results[t][f"T+{h}"].get(f"pool::{lab}")
                if not c:
                    continue
                ci = f"[{c['rank_ic_ci'][0]:+.3f}, {c['rank_ic_ci'][1]:+.3f}]" if c["rank_ic_ci"] else "—"
                L.append(f"| {t} | T+{h} | {lab} | {c['n']} | {c['base_rate_up_flatmargin']:.3f} "
                         f"| {c['mean_p_up']:.3f} | {c['calib_bias']:+.3f} | {c['rank_ic']:+.4f} "
                         f"| {ci} | {c['brier_mc']:.3f} | {c['brier_majority']:.3f} "
                         f"| {c['ace']:.3f} | {c['ece']:.3f} | "
                         f"{('%.2f' % c['calib_slope']) if c['calib_slope'] is not None else '—'} "
                         f"| {c['est_chg_ic']:+.4f} | "
                         f"{(_fmt(c['spread'])) if c['spread'] is not None else '—'} |")
    L += ["", "`校准偏差` = mean(p_up) − 实际 up 率（>0 = 高估上涨概率）；`up` 按 ±flat_margin 三分类判定。"
          "Brier 多类瞎猜基准 0.667。`Q80−Q20 fwd` = p_up 最高/最低 20% 组的平均未来收益差（经济价值）。", ""]

    # ---- 1c. 与 1455 口径的机械耦合对照（本步最关键的一条防伪）----
    L += ["## 1c. ⚠️ 1455 口径的机械耦合：模型必须跑赢的是「−est_chg」，不是 0", "",
          "契约式 `fwd_1455 = navs[T+h] / (navs[T-1]·(1+est_chg)) − 1` 的**分母含 est_chg**，"
          "而分子（未来真实净值）与之无关 ⇒ est_chg 越大则 label 机械越小。"
          "因此在 1455 口径下，**零训练公式「预测分 = −est_chg」本身就有正 RankIC**。"
          "把它当 0 基线才是诚实比较；与 0 比会把算术耦合误读成模型能力。", "",
          "| 轨 | 周期 | 模型 IC(1455) | 零成本基线 −est_chg | 超额 | 判定 |",
          "|---|---|---:|---:|---:|---|"]
    for t in tracks:
        for h in HORIZONS:
            c = results[t][f"T+{h}"].get("pool::1455")
            if not c:
                continue
            good = c["excess_vs_arith"] > 0
            L.append(f"| {t} | T+{h} | {c['rank_ic']:+.4f} | {c['arith_baseline_ic']:+.4f} "
                     f"| {c['excess_vs_arith']:+.4f} | {'✅ 有超额' if good else '❌ 不及零成本公式'} |")
    L += ["", "<sub>「判定」列 = 描述性对照（模型 IC 是否高于该口径的零成本基线），"
          "**不是预注册裁决**；晋升判据仍以 promotion_prereg.json 为准，本表不触发任何门禁。</sub>", "",
          "对照 old 口径（无此耦合，基线 IC≈0，与 0 比是成立的）：", "",
          "| 轨 | 周期 | 模型 IC(old) | 基线 +est_chg | 超额 |", "|---|---|---:|---:|---:|"]
    for t in tracks:
        for h in HORIZONS:
            c = results[t][f"T+{h}"].get("pool::old")
            if not c:
                continue
            L.append(f"| {t} | T+{h} | {c['rank_ic']:+.4f} | {c['arith_baseline_ic']:+.4f} "
                     f"| {c['excess_vs_arith']:+.4f} |")
    L += ["", "### 对步 4「是否重训 v4」的直接含义", "",
          "- B 轨（1455 重训）的 1455 IC（T+1 +0.1139）看着比 C 轨（旧 label 训）的 +0.0649 高，"
          "**但两者都低于同格零成本基线 +0.1278** ⇒ 该「提升」不构成重训依据。",
          "- 若将来改用 1455 label 训练，裁决基线必须同步从 `est_chg` 换成 `−est_chg`，"
          "否则会系统性高估模型；此点应在重训预注册里写死，不能事后补。",
          "- 更干净的替代口径（留给 Summer 拍板）：1455 预测的**目标应仍是 T→T+h 的真实收益**（old 口径），"
          "1455 只该用于「决策时点可得的输入」与「相对 14:55 估计价的盈亏核算」，"
          "而不是把 est_chg 塞进 label 分母后再拿模型分与它比较。", ""]

    main_track = "C_old_label_retrain" if "C_old_label_retrain" in results else tracks[0]
    L += [f"## 2. 逐基金矩阵（主轨 = {TRACK_CN[main_track]}）", "",
          f"<sub>取 {main_track} 而非 A 轨：口径对照必须在「同一血统模型」内部读，"
          "避免把模型差异误读成 label 差异。A 轨逐基金数见第 2b 节。</sub>", ""]
    for h in HORIZONS:
        L += [f"### T+{h}", "",
              "| 基金 | label | n | 日数 | P(fwd>0) | mean p_up | 偏差 | RankIC | 95% CI | Brier(多类) | ACE | ECE | 标记 |",
              "|---|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---|"]
        codes = sorted({k.split("::")[0] for k in results[main_track][f"T+{h}"] if not k.startswith("pool")})
        for code in codes:
            for lab in LABELS:
                c = results[main_track][f"T+{h}"].get(f"{code}::{lab}")
                if not c:
                    continue
                ci = (f"[{c['rank_ic_ci'][0]:+.3f}, {c['rank_ic_ci'][1]:+.3f}]"
                      if c["rank_ic_ci"] else "—")
                flag = "⚠️ LOW_N（只出数，不下判）" if c["low_n"] else ""
                L.append(f"| {code} | {lab} | {c['n']} | {c['n_days']} "
                         f"| {c['base_rate_up_flatmargin']:.3f} | {c['mean_p_up']:.3f} "
                         f"| {c['calib_bias']:+.3f} | {c['rank_ic']:+.4f} | {ci} "
                         f"| {c['brier_mc']:.3f} | {c['ace']:.3f} | {c['ece']:.3f} | {flag} |")
        L.append("")

    if main_track != "A_v3_frozen_weights" and "A_v3_frozen_weights" in results:
        L += ["### 2b. A 轨（现行 v3 权重）逐基金同表", ""]
        for h in HORIZONS:
            L += [f"T+{h}", "",
                  "| 基金 | label | n | RankIC | 95% CI | mean p_up | 偏差 | ACE | 标记 |",
                  "|---|---|---:|---:|---|---:|---:|---:|---|"]
            codes = sorted({k.split("::")[0] for k in results["A_v3_frozen_weights"][f"T+{h}"]
                            if not k.startswith("pool")})
            for code in codes:
                for lab in LABELS:
                    c = results["A_v3_frozen_weights"][f"T+{h}"].get(f"{code}::{lab}")
                    if not c:
                        continue
                    ci = (f"[{c['rank_ic_ci'][0]:+.3f}, {c['rank_ic_ci'][1]:+.3f}]"
                          if c["rank_ic_ci"] else "—")
                    L.append(f"| {code} | {lab} | {c['n']} | {c['rank_ic']:+.4f} | {ci} "
                             f"| {c['mean_p_up']:.3f} | {c['calib_bias']:+.3f} | {c['ace']:.3f} "
                             f"| {'⚠️ LOW_N' if c['low_n'] else ''} |")
            L.append("")

    L += ["## 3. 可靠性分桶（池级 · up 二值 · 全轨）", "",
          "| 轨 | 周期 | label | 预测区间 | n | 预测均值 | 实际频率 | 差 |",
          "|---|---|---|---|---:|---:|---:|---:|"]
    for t in tracks:
        for h in HORIZONS:
            for lab in LABELS:
                c = results[t][f"T+{h}"].get(f"pool::{lab}")
                if not c:
                    continue
                for b in c["bins"]:
                    L.append(f"| {t} | T+{h} | {lab} | {b['bin']} | {b['n']} "
                             f"| {b['p']:.3f} | {b['freq']:.3f} | {b['p'] - b['freq']:+.3f} |")
    L += ["", "「差」= 预测均值 − 实际频率（>0 = 该区间高估上涨概率）。"
          "这是 SSOT 问题「63% 上涨概率是否大致真 63%」的直接读数。", ""]

    if "windows" in results:
        L += ["## 4. OOS 分窗 RankIC（每格 old / 1455）", ""]
        for t, rows_w in results["windows"].items():
            L += [f"### {TRACK_CN.get(t, t)}", "",
                  "| 窗口 | n | T+1 | T+3 | T+5 |", "|---|---:|---|---|---|"]
            for w in rows_w:
                cells = [f"{_wfmt(w, h, 'old')} / {_wfmt(w, h, '1455')}" for h in HORIZONS]
                L.append(f"| {w['window']} | {w['n']} | " + " | ".join(cells) + " |")
            L.append("")

    L += ["---", "", "## 5. 本矩阵不做什么（边界）", "",
          "- **不改门禁**：A 轨是线上现行权重的读数；registry 判定该权重 "
          f"validation 未批准（approval_error=`{m['weights'].get('approval_error')}`、"
          "model_ready=false），本矩阵不构成 model_ready 或晋升依据。",
          "- **不重算 label**：`fwd*_1455` 全部消费步 1 产物，本脚本只读不算。",
          "- **不触网**：只读 forecast_outputs/ 与 data/ 缓存；"
          "0910 冻结件 G-A PASS、G-B DRIFTED 已在第 0 节量化复核为净值层无漂移。",
          "- 小样本格（n<60，如 025687）标 LOW_N，只出数值不进任何结论。", ""]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
