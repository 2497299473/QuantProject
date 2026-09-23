#!/usr/bin/env python3
"""V4.4-债务批 ③：early_stopping A/B/C 对照实验（2026-09-23，零网络）。

问题（GPT 审阅 + 逐行核实）：生产 `core/forecast_engine.py` 三处 HistGB 全部
`early_stopping=True, random_state=42` ⇒ sklearn 内部做 **随机** train_test_split
切验证集（sklearn 1.9 `_hist_gradient_boosting/gradient_boosting.py:556`）。
时间序列任务里随机切分 = 验证集含训练集未来样本 ⇒ 早停判据本身可能被泄漏污染，
「最优迭代数」选在非因果位置上。random_state=42 固定 ⇒ 这是**方法学纯度**问题，
不是可复现性问题。

三轨对照（同一冻结件、同一特征、同一 OOS 切分，只改早停策略）：

  A · INNER_RANDOM      现状复刻：early_stopping=True（内部随机 10% 验证集）
  B · NONE              早停关闭：early_stopping=False（跑满 max_iter=200）
  C · TEMPORAL_VALID    显式时间序验证集：训练段**末尾**切 15% 传入 fit(X_val=…)
                        ⇒ 跳过内部随机切分，验证集严格晚于训练集

判定（事先写死）：
  1. 三轨跑同一 OOS 段，逐 horizon 出 RankIC / Brier / 训练迭代数。
  2. 若 C 与 A 差异显著 ⇒ 证实随机切分确实污染了早停判据，生产应改 C。
  3. 若三轨差异在噪声带内 ⇒ 早停策略对结论无实质影响，记录后维持现状即可。

纪律（对齐 backtest_pit1455_matrix 体例）：
  - 零网络：只读 forecast_outputs/ 与 data/ 缓存；socket 守卫。
  - **不改任何生产脚本**、不动 forecast_engine、不进 registry、不动门禁与
    model_ready、不改冻结件本体。本脚本只**复制**训练逻辑做对照实验。
  - 报告落 output/（.gitignore 拦截）；机读件落 forecast_outputs/。

用法：
    .\\.venv\\Scripts\\python.exe backtest_early_stopping_ab.py
    ... --snapshot forecast_outputs/samples_frozen_20260910.jsonl
    ... --val-frac 0.15 --horizons 1,3,5
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import numpy as np  # noqa: E402

from frozen_dataset import resolve_samples          # noqa: E402
from backtest_spread import load_samples            # noqa: E402
from core.forecast_engine import FEATURE_KEYS       # noqa: E402

TRACKS = ("A_INNER_RANDOM", "B_NONE", "C_TEMPORAL_VALID")
TRACK_LABELS = {
    "A_INNER_RANDOM": "A · 现状（内部随机 10% 验证集）",
    "B_NONE": "B · 早停关闭（跑满 max_iter）",
    "C_TEMPORAL_VALID": "C · 显式时间序验证集（训练段末尾切）",
}

MAX_ITER = 200
LEARNING_RATE = 0.08
MAX_DEPTH = 3
RANDOM_STATE = 42
FLAT_MARGIN = 0.003
DEFAULT_VAL_FRAC = 0.15


def _block_network() -> None:
    """零网络守卫：本脚本不得触网（冻结件/缓存均已在本地）。"""

    def _deny(*_a, **_k):
        raise RuntimeError("本脚本为零网络作业，禁止建 socket")

    socket.socket = _deny          # type: ignore[assignment]
    socket.create_connection = _deny  # type: ignore[assignment]


def _xy(samples: list[dict], horizon: int) -> tuple[np.ndarray, np.ndarray]:
    """复刻 forecast_engine.DirectionModel.features_labels（B1 双列掩码口径）。

    只读复制，不改生产实现——三轨用同一份特征，差异只来自早停策略。
    """
    X, y = [], []
    for s in samples:
        fwd = s.get(f"fwd{horizon}")
        if fwd is None:
            continue
        row = []
        for k in FEATURE_KEYS:
            v = s.get(k)
            row.append(0.0 if v is None else float(v))
            row.append(1.0 if v is None else 0.0)
        X.append(row)
        if fwd > FLAT_MARGIN:
            y.append(2)
        elif fwd < -FLAT_MARGIN:
            y.append(0)
        else:
            y.append(1)
    if not X:
        return np.zeros((0, len(FEATURE_KEYS) * 2)), np.zeros(0, dtype=int)
    return np.array(X, dtype=float), np.array(y, dtype=int)


def _ranks(vals: np.ndarray) -> np.ndarray:
    order = np.argsort(vals, kind="mergesort")
    r = np.empty(len(vals), dtype=float)
    r[order] = np.arange(len(vals), dtype=float)
    return r


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 3:
        return 0.0
    ra, rb = _ranks(a), _ranks(b)
    ra -= ra.mean()
    rb -= rb.mean()
    den = float(np.sqrt((ra ** 2).sum() * (rb ** 2).sum()))
    return float((ra * rb).sum() / den) if den > 1e-12 else 0.0


def _brier(proba: np.ndarray, y: np.ndarray) -> float:
    """三分类 Brier（对 up 类做二值化，与项目既有口径一致）。"""
    p_up = proba[:, 2]
    hit = (y == 2).astype(float)
    return float(np.mean((p_up - hit) ** 2))


def _make_clf(track: str):
    from sklearn.ensemble import HistGradientBoostingClassifier
    kw = dict(max_iter=MAX_ITER, learning_rate=LEARNING_RATE, max_depth=MAX_DEPTH,
              random_state=RANDOM_STATE)
    if track == "B_NONE":
        return HistGradientBoostingClassifier(early_stopping=False, **kw)
    # A 与 C 都需要早停开；差别只在验证集从哪来
    return HistGradientBoostingClassifier(early_stopping=True, **kw)


def _fit_track(track: str, X_tr: np.ndarray, y_tr: np.ndarray,
               val_frac: float) -> tuple[object | None, int | None, int]:
    """返回 (model, n_iter_used, n_val_used)。model=None 表示训练样本不足。"""
    if len(y_tr) < 50:
        return None, None, 0
    clf = _make_clf(track)
    if track == "C_TEMPORAL_VALID":
        # 训练段末尾切验证（时间序，非随机）——X/y 已按日期升序传入
        n_val = max(30, int(len(y_tr) * val_frac))
        n_val = min(n_val, len(y_tr) - 50)
        if n_val < 30:
            # 验证集切不出来 ⇒ 退化为跑满（诚实标注，不假装是 C）
            clf.set_params(early_stopping=False)
            clf.fit(X_tr, y_tr)
            return clf, MAX_ITER, 0
        X_v, y_v = X_tr[-n_val:], y_tr[-n_val:]
        clf.fit(X_tr[:-n_val], y_tr[:-n_val], X_val=X_v, y_val=y_v)
        return clf, int(clf.n_iter_), n_val
    clf.fit(X_tr, y_tr)
    return clf, int(getattr(clf, "n_iter_", MAX_ITER)), 0


def main() -> int:
    ap = argparse.ArgumentParser(description="early_stopping A/B/C 对照（零网络）")
    ap.add_argument("--snapshot", default=None,
                    help="冻结样本 jsonl（默认自动选最新 forecast_outputs/samples_frozen_*.jsonl）")
    ap.add_argument("--fresh", action="store_true",
                    help="显式活拉样本（数字与冻结基线不可比；报告标 FRESH）")
    ap.add_argument("--val-frac", type=float, default=DEFAULT_VAL_FRAC,
                    help=f"C 轨时间序验证集占比（默认 {DEFAULT_VAL_FRAC}）")
    ap.add_argument("--horizons", default="1,3,5", help="对照周期，逗号分隔")
    ap.add_argument("--no-net", action="store_true", default=True,
                    help="零网络守卫（默认开）")
    ap.add_argument("--out", default=None, help="报告落点（默认 output/）")
    args = ap.parse_args()

    _block_network()
    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]

    samples, snap_info = resolve_samples(args.snapshot, args.fresh, BASE_DIR, load_samples)
    if snap_info["mode"] in ("MISSING", "INVALID"):
        print(f"[ABORT] 样本快照 {snap_info['mode']} ⇒ fail-closed")
        return 4

    # 切分真源与项目既有口径一致：按日期升序，SPLIT_DATE 前 train / 后 OOS
    from backtest_spread import SPLIT_DATE
    samples = sorted(samples, key=lambda s: (s["date"], s["fund"]))
    train = [s for s in samples if s["date"] < SPLIT_DATE]
    oos = [s for s in samples if s["date"] >= SPLIT_DATE]

    print("=" * 74)
    print("early_stopping A/B/C 对照实验（零网络）")
    print(f"  样本       : {snap_info.get('file')}  [{snap_info['mode']}]")
    print(f"  闸门       : G-A {snap_info.get('gate_internal', '-')}  "
          f"G-B {snap_info.get('gate_comparability', '-')}")
    print(f"  训练段     : {len(train)} 行（< {SPLIT_DATE}）")
    print(f"  OOS 段     : {len(oos)} 行（>= {SPLIT_DATE}）")
    print(f"  周期       : {horizons}    max_iter={MAX_ITER} lr={LEARNING_RATE} depth={MAX_DEPTH}")
    print(f"  C 验证占比 : {args.val_frac}")
    print("=" * 74)

    results: dict[str, dict] = {}
    for h in horizons:
        X_tr, y_tr = _xy(train, h)
        X_oos, y_oos = _xy(oos, h)
        if len(y_oos) < 10 or len(y_tr) < 50:
            print(f"\n[T+{h}] 样本不足（train={len(y_tr)} oos={len(y_oos)}）⇒ 跳过")
            continue
        print(f"\n[T+{h}] train={len(y_tr)}  oos={len(y_oos)}")
        print(f"  {'轨':<26}{'n_iter':>8}{'val_n':>8}{'RankIC':>10}{'Brier':>10}")
        results[f"T+{h}"] = {}
        for track in TRACKS:
            clf, n_iter, n_val = _fit_track(track, X_tr, y_tr, args.val_frac)
            if clf is None:
                print(f"  {TRACK_LABELS[track]:<26}{'-':>8}{'-':>8}{'-':>10}{'-':>10}")
                continue
            proba = clf.predict_proba(X_oos)
            # 三分类可能缺类（<3 unique）⇒ 对齐到 [down, flat, up] 三列
            classes = list(getattr(clf, "classes_", [0, 1, 2]))
            full = np.zeros((len(X_oos), 3), dtype=float)
            for j, c in enumerate(classes):
                full[:, int(c)] = proba[:, j]
            ic = _spearman(full[:, 2], y_oos.astype(float))
            br = _brier(full, y_oos)
            results[f"T+{h}"][track] = {
                "n_iter": n_iter, "n_val": n_val,
                "rank_ic_p_up": round(ic, 4), "brier": round(br, 4),
                "train_n": int(len(y_tr)), "oos_n": int(len(y_oos)),
            }
            print(f"  {TRACK_LABELS[track]:<26}{n_iter:>8}{n_val:>8}{ic:>10.4f}{br:>10.4f}")

    # ---- 判定摘要 ----
    print("\n" + "=" * 74)
    print("判定摘要（同 OOS 段、同特征，只改早停策略）")
    print("=" * 74)
    verdict_lines = []
    for h in horizons:
        key = f"T+{h}"
        r = results.get(key)
        if not r or len(r) < 2:
            continue
        a = r.get("A_INNER_RANDOM", {})
        c = r.get("C_TEMPORAL_VALID", {})
        b = r.get("B_NONE", {})
        if a and c:
            d_ic = c["rank_ic_p_up"] - a["rank_ic_p_up"]
            d_it = c["n_iter"] - a["n_iter"]
            line = (f"  {key}: C−A  ΔRankIC={d_ic:+.4f}  Δn_iter={d_it:+d}"
                    f"   (A n_iter={a['n_iter']} / C n_iter={c['n_iter']} / B n_iter={b.get('n_iter')})")
            verdict_lines.append(line)
            print(line)
    print("\n  读法：上表是**单窗口**读数（训练段末尾切验证）。时间序验证集的")
    print("        选择自带窗口敏感性：换 val_frac 会换一个时刻的 regime。")

    # ---- 稳健性复核（C 轨 val_frac 扫描）----
    print("\n" + "=" * 74)
    print("稳健性：C 轨 val_frac 扫描（同 OOS 段，只改验证窗口大小）")
    print("=" * 74)
    print(f"  {'val_frac':>10}{'T+1 ΔIC':>12}{'T+3 ΔIC':>12}{'T+5 ΔIC':>12}")
    scan = {}
    for vf in (0.10, 0.15, 0.20, 0.30):
        row = {}
        for h in horizons:
            X_tr, y_tr = _xy(train, h)
            X_oos, y_oos = _xy(oos, h)
            if len(y_oos) < 10 or len(y_tr) < 50:
                continue
            clf_a, _ni, _nv = _fit_track("A_INNER_RANDOM", X_tr, y_tr, args.val_frac)
            if clf_a is None:
                continue
            fa = np.zeros((len(X_oos), 3))
            pa = clf_a.predict_proba(X_oos)
            for j, c in enumerate(list(getattr(clf_a, "classes_", [0, 1, 2]))):
                fa[:, int(c)] = pa[:, j]
            ic_a = _spearman(fa[:, 2], y_oos.astype(float))
            clf_c, _ni, _nv = _fit_track("C_TEMPORAL_VALID", X_tr, y_tr, vf)
            if clf_c is None:
                continue
            fc = np.zeros((len(X_oos), 3))
            pc = clf_c.predict_proba(X_oos)
            for j, c in enumerate(list(getattr(clf_c, "classes_", [0, 1, 2]))):
                fc[:, int(c)] = pc[:, j]
            ic_c = _spearman(fc[:, 2], y_oos.astype(float))
            row[f"T+{h}"] = round(ic_c - ic_a, 4)
        scan[str(vf)] = row
        cells = "".join(f"{row.get(f'T+{h}', float('nan')):>12.4f}" for h in horizons)
        print(f"  {vf:>10.2f}{cells}")
    all_neg = all(v < 0 for row in scan.values() for v in row.values())
    print(f"\n  全部 val_frac × 周期组合均为 C<A：{'✅ 是' if all_neg else '❌ 否'}")
    if all_neg:
        print("  ⇒ 时间序验证集在该任务上系统性地更差，不是单窗口偶然。")

    # ---- 落档 ----
    stamp = datetime.now().strftime("%Y%m%d")
    outdir = Path(args.out) if args.out else (BASE_DIR / "output")
    outdir.mkdir(exist_ok=True)
    payload = {
        "kind": "early_stopping_ab",
        "schema_version": "1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot": snap_info.get("file"),
        "snapshot_mode": snap_info["mode"],
        "gate_internal": snap_info.get("gate_internal"),
        "gate_comparability": snap_info.get("gate_comparability"),
        "report_line": snap_info.get("report_line", ""),
        "split_date": SPLIT_DATE,
        "hyperparams": {"max_iter": MAX_ITER, "learning_rate": LEARNING_RATE,
                        "max_depth": MAX_DEPTH, "random_state": RANDOM_STATE,
                        "flat_margin": FLAT_MARGIN, "val_frac_c": args.val_frac},
        "tracks": list(TRACKS),
        "results": results,
        "verdict_lines": verdict_lines,
        "scope_note": ("证据层诊断：不绑 registry / promotion，model_ready 不受影响；"
                       "本轮不修改任何生产脚本。"),
    }
    md = outdir / f"backtest_early_stopping_ab_{stamp}.md"
    js = BASE_DIR / "forecast_outputs" / f"early_stopping_ab_{stamp}.json"
    lines = ["# early_stopping A/B/C 对照实验", "",
             f"- 生成：{payload['generated_at']}（零网络）",
             f"- 冻结件：`{payload['snapshot']}` [{payload['snapshot_mode']}]",
             f"- 切分：`{SPLIT_DATE}`", f"- 超参：{payload['hyperparams']}", "",
             "| 周期 | 轨 | n_iter | val_n | RankIC(p_up) | Brier |",
             "|:--|:--|--:|--:|--:|--:|"]
    for h in horizons:
        for track in TRACKS:
            r = results.get(f"T+{h}", {}).get(track)
            if r:
                lines.append(f"| T+{h} | {TRACK_LABELS[track]} | {r['n_iter']} | "
                             f"{r['n_val']} | {r['rank_ic_p_up']:+.4f} | {r['brier']:.4f} |")
    lines += ["", "## 判定", ""] + [f"- {l.strip()}" for l in verdict_lines]
    lines += ["", "## 稳健性：C 轨 val_frac 扫描", "",
              "| val_frac | " + " | ".join(f"T+{h} ΔIC" for h in horizons) + " |",
              "|:--|" + "--:|" * len(horizons)]
    for vf, row in scan.items():
        lines.append(f"| {vf} | " + " | ".join(
            f"{row.get(f'T+{h}', float('nan')):+.4f}" for h in horizons) + " |")
    lines += ["", f"全部组合 C<A：{'是' if all_neg else '否'}", ""]
    lines += ["", f"> {payload['scope_note']}", ""]
    md.write_text("\n".join(lines), encoding="utf-8")
    js.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[done] 报告 → {md}")
    print(f"[done] 机读 → {js}")
    return 0


if __name__ == "__main__":
    sys.exit(main())