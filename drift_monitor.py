#!/usr/bin/env python3
"""特征/标签 Drift 监控（2026-09-01，P2-④ 升格为 P1）。

动机（2026-09-01 实证）：Path-WF 逐折 hit mdd10 从 0% 摆到 17.8%、T+5 WF
最近两折 RankIC 转负而 pooled 仍显著——两者都指向 regime 变化。本脚本把
「regime 变了没」从推断变成逐折可量化指标：

- 特征漂移：每个 WF fold，fold-train（预期分布）vs fold-test（实际分布）
  逐特征 PSI（Population Stability Index，10 分位桶）+ KS 统计量；
- 标签漂移：fwd1/fwd5 的 train_k vs test_k 均值差与波动比（σ_test/σ_train）；
- 预注册阈值（PSI）：<0.10 稳定 / 0.10~0.25 中度 / >0.25 显著；
  KS 仅作参考列（无分布假设，对桶切法不敏感）。

纪律：纯观察层——不绑定 registry/promotion，不改 model_ready，不改 verdict。
与 WF/calib 报告同冻结切分（split_date_oos + build_wf_folds），同窗口可比。

用法：python3 drift_monitor.py [--window-days 63]
输出：output/drift_monitor_YYYYMMDD.md（+ stdout 摘要）。
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import numpy as np

from backtest_spread import load_samples
from frozen_dataset import resolve_samples   # V4.3 P0-1：统一冻结样本入口
from backtest_forecast import split_date_oos
from backtest_walk_forward import build_wf_folds
from core import no_net_guard                # D5-F2：默认路径零网络守卫（铁律 1/7/8）
from core.forecast_engine import FEATURE_KEYS

PSI_STABLE = 0.10      # 预注册：<0.10 特征分布稳定
PSI_MODERATE = 0.25    # 预注册：0.10~0.25 中度漂移；>0.25 显著漂移

FRESH_CONFIRM_FLAG = "--i-know-this-hits-network"
"""D5-F2（2026-09-30 面 5 审计）：--fresh 活拉的双旗标人工确认。

--fresh 经 resolve_samples→load_samples→data_loader/stock_data 全链活拉，是
离线工具名下的真实触网入口（铁律 8 「load_samples 系」名单外等价物，
09-18 同型事故在案）。无确认旗标 ⇒ 拒绝启动（exit 3）；有旗标 ⇒ 醒目警示
后放行（铁律 7 四闸门仍需人工确认，代码不假装判断）。"""


def psi_level(v: float | None) -> str:
    """PSI → 预注册等级标签。None（退化/样本不足）= n/a。"""
    if v is None:
        return "n/a"
    if v > PSI_MODERATE:
        return "significant"
    if v > PSI_STABLE:
        return "moderate"
    return "stable"


def psi_vs_ref(ref, cur, n_bins: int = 10) -> float | None:
    """PSI(ref→cur)：ref 切 10 分位桶，Σ (a-e)·ln(a/e)（含平滑）。

    ref = 预期分布（fold-train），cur = 实际分布（fold-test）。
    离散特征（ref 唯一值 ≤5，如 est_sign/composite）改用类别 PSI——
    分位桶对离散值会产生重复边界伪影，高估漂移。
    返回 None = 样本不足 / 常数特征无法分桶（诚实跳过，不误报 0）。
    """
    ref = np.asarray([x for x in ref if x is not None], dtype=float)
    cur = np.asarray([x for x in cur if x is not None], dtype=float)
    if len(ref) < 50 or len(cur) < 20:
        return None
    if float(np.ptp(ref)) == 0.0:
        return None                      # 常数特征：预期分布无信息
    uniq = np.unique(ref)
    if len(uniq) <= 5:                   # 离散 → 类别 PSI（按唯一值）
        cnt_ref = {v: float(np.sum(ref == v)) / len(ref) for v in uniq}
        cnt_cur = {v: float(np.sum(cur == v)) / len(cur) for v in uniq}
        eps = 1e-4
        return float(sum((cnt_cur.get(v, 0.0) - p) * math.log(max(cnt_cur.get(v, 0.0), eps) / max(p, eps))
                         for v, p in cnt_ref.items()))
    edges = np.unique(np.quantile(ref, np.linspace(0, 1, n_bins + 1)[1:-1]))
    if len(edges) == 0:
        return None

    def _props(x: np.ndarray) -> np.ndarray:
        idx = np.searchsorted(edges, x, side="right")
        return np.bincount(idx, minlength=len(edges) + 1) / len(x)

    e, a = _props(ref), _props(cur)
    eps = 1e-4                           # 防 ln(0)：空桶占比截断
    e, a = np.clip(e, eps, None), np.clip(a, eps, None)
    return float(np.sum((a - e) * np.log(a / e)))


def ks_statistic(ref, cur) -> float | None:
    """双样本 KS 统计量 max|CDF_ref - CDF_cur| ∈ [0,1]。None = 样本不足。"""
    ref = np.asarray([x for x in ref if x is not None], dtype=float)
    cur = np.asarray([x for x in cur if x is not None], dtype=float)
    if len(ref) < 50 or len(cur) < 20:
        return None
    ref_s, cur_s = np.sort(ref), np.sort(cur)
    combined = np.sort(np.concatenate([ref, cur]))
    cdf_ref = np.searchsorted(ref_s, combined, side="right") / len(ref)
    cdf_cur = np.searchsorted(cur_s, combined, side="right") / len(cur)
    return float(np.max(np.abs(cdf_ref - cdf_cur)))


def label_drift(train: list[dict], test: list[dict], horizon: int) -> dict | None:
    """标签漂移：fwd{h} 均值差（test − train）与波动比 σ_test/σ_train。"""
    tr = [float(s[f"fwd{horizon}"]) for s in train if s.get(f"fwd{horizon}") is not None]
    te = [float(s[f"fwd{horizon}"]) for s in test if s.get(f"fwd{horizon}") is not None]
    if len(tr) < 50 or len(te) < 20:
        return None
    mu_tr, mu_te = float(np.mean(tr)), float(np.mean(te))
    sd_tr = float(np.std(tr, ddof=1))
    sd_te = float(np.std(te, ddof=1))
    return {"h": horizon, "n_train": len(tr), "n_test": len(te),
            "mu_train": mu_tr, "mu_test": mu_te,
            "mu_diff": mu_te - mu_tr,
            "sigma_ratio": (sd_te / sd_tr) if sd_tr > 1e-12 else None}


def fold_drift(f: dict) -> dict:
    """单 fold 全量漂移：7 特征 PSI/KS + fwd1/fwd5 标签漂移。"""
    feats = {}
    for k in FEATURE_KEYS:
        ref = [s.get(k) for s in f["train"]]
        cur = [s.get(k) for s in f["test"]]
        feats[k] = {"psi": psi_vs_ref(ref, cur), "ks": ks_statistic(ref, cur)}
    return {"window": f"{f['window'][0]} ~ {f['window'][-1]}",
            "n_train": len(f["train"]), "n_test": len(f["test"]),
            "features": feats,
            "labels": {h: label_drift(f["train"], f["test"], h)
                       for h in (1, 5)}}


def drift_summary(folds_drift: list[dict]) -> dict:
    """跨折汇总：每特征 max PSI / 显著折数 / 标签漂移极值。"""
    per_feat = {}
    for k in FEATURE_KEYS:
        vals = [(fd["fold"], fd["features"][k]["psi"]) for fd in folds_drift
                if fd["features"][k]["psi"] is not None]
        per_feat[k] = {
            "max_psi": max((v for _, v in vals), default=None),
            "argmax_fold": (max(vals, key=lambda t: t[1])[0] if vals else None),
            "n_significant": sum(1 for _, v in vals if v > PSI_MODERATE),
            "n_moderate": sum(1 for _, v in vals if PSI_STABLE < v <= PSI_MODERATE),
        }
    return {"per_feat": per_feat}


def _install_default_guard():
    """D5-F2：默认（冻结件）路径守卫——零网络作业，socket 封锁（铁律 1/7/8）。

    与 main 的装载点同一函数（接线可测）；长驻进程纪律下不 restore。
    守卫是频控纪律墙，不是 PIT 防穿越机制（见 core.no_net_guard docstring）。
    """
    return no_net_guard.install(
        f"[drift_monitor] 默认路径为零网络作业（只读冻结件）；活拉需 "
        f"--fresh {FRESH_CONFIRM_FLAG} 双旗标（铁律 7/8）",
        block_construction=False)


def guard_selftest() -> tuple[bool, str]:
    """D5-F2 自检：守卫装载后 connect/sendto 均被 NoNetViolation 拦截（非 OS 错）。

    复用 core.no_net_guard.selftest 共享实现（ponytail：不另写拦截探针）；
    「main 在 resolve_samples 之前装载」的接线时序由测试源码序断言钉住。
    """
    return no_net_guard.selftest("[drift_monitor] 默认路径零网络守卫")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-days", type=int, default=63)
    ap.add_argument("--snapshot", default=None,
                    help="冻结样本 jsonl（默认自动选最新 forecast_outputs/samples_frozen_*.jsonl）")
    ap.add_argument("--fresh", action="store_true",
                    help="显式活拉样本（数字与冻结基线不可比；报告标 FRESH）——"
                         f"需同时给 {FRESH_CONFIRM_FLAG} 双旗标确认（铁律 8）")
    ap.add_argument(FRESH_CONFIRM_FLAG, dest="fresh_confirm", action="store_true",
                    help="D5-F2 双旗标：确知本次会发真实网络请求（东财/腾讯全链），"
                         "须已过铁律 7 四闸门并获人工授权（铁律 8）")
    ap.add_argument("--selftest", action="store_true",
                    help="零网络自检：守卫拦截行为（connect/sendto → NoNetViolation）")
    args = ap.parse_args(argv)

    if args.selftest:
        ok, detail = guard_selftest()
        print(f"[selftest] 零网络守卫：{'OK' if ok else 'FAIL'} {detail}")
        return 0 if ok else 1

    # D5-F2（2026-09-30 面 5 审计）：触网入口双旗标 + 默认路径守卫常开。
    if args.fresh:
        if not args.fresh_confirm:
            print(f"[fail] --fresh 是活拉路径（load_samples 全链拉东财/腾讯）——铁律 8："
                  f"冒烟＝真跑网络路径，必须先获人工授权。"
                  f"获批后加双旗标：--fresh {FRESH_CONFIRM_FLAG}")
            return 3
        print("[warn] ⚠️ --fresh 双旗标已确认：本次将发真实网络请求。"
              "请确认已过铁律 7 四闸门（上游已跑完/无频控征兆/无并行拉取/11:20 时间盒内）。")
    else:
        _install_default_guard()   # 默认（冻结件）路径：零网络守卫，无开关可关

    t0 = time.time()

    print("== [0] 加载样本 ==")
    samples, snap_info = resolve_samples(args.snapshot, args.fresh, BASE_DIR, load_samples)
    if snap_info["mode"] in ("MISSING", "INVALID"):
        return 4
    samples_sorted = sorted(samples, key=lambda s: (s["date"], s["fund"]))
    train_all, oos, oos_start = split_date_oos(samples_sorted)
    print(f"== [1] 冻结切分：train < {oos_start}（{len(train_all)}），"
          f"OOS ≥ {oos_start}（{len(oos)}）==")

    folds = build_wf_folds(samples_sorted, oos, oos_start,
                           window_days=args.window_days, max_horizon=5)
    if not folds:
        print("[fail] 无可用 WF 折")
        return 1
    print(f"== [2] Drift {len(folds)} 折（{args.window_days} 交易日/折，与 WF 同窗）==")

    folds_drift = []
    for fi, f in enumerate(folds, 1):
        d = fold_drift(f)
        d["fold"] = fi
        folds_drift.append(d)
        worst = max((v["psi"] for v in d["features"].values()
                     if v["psi"] is not None), default=None)
        n_sig = sum(1 for v in d["features"].values() if psi_level(v["psi"]) == "significant")
        l5 = d["labels"].get(5)
        l5_str = (f"fwd5 μ差={l5['mu_diff']:+.5f} σ比={l5['sigma_ratio']:.2f}"
                  if l5 and l5["sigma_ratio"] else "fwd5 n/a")
        print(f"  折 {fi}（{d['window']}）：最差特征 PSI={worst:.3f} "
              f"显著漂移 {n_sig}/{len(FEATURE_KEYS)} · {l5_str}")

    summary = drift_summary(folds_drift)

    # ---- 报告 ----
    lines = [
        "# 特征/标签 Drift 监控（2026-09-01，观察层）", "",
        snap_info["report_line"],
        f"> 生成：{time.strftime('%Y-%m-%d %H:%M')} · {len(folds)} 折"
        f"（{args.window_days} 交易日/折，与 WF 同窗）· PSI 10 分位桶 · "
        f"预注册阈值：<0.10 稳定 / 0.10~0.25 中度 / >0.25 显著", "",
        "**回答的问题**：WF 最近折衰减（T+5 最近两折 IC 转负）与 Path-WF 折间"
        "大幅波动，有多少能用特征/标签分布漂移解释？", "",
    ]
    for d in folds_drift:
        lines += [f"## 折 {d['fold']}：{d['window']}", "",
                  f"n_train={d['n_train']} n_test={d['n_test']}", "",
                  "| 特征 | PSI | 等级 | KS |",
                  "|---|---:|---|---:|"]
        for k in FEATURE_KEYS:
            v = d["features"][k]
            psi_s = f"{v['psi']:.3f}" if v["psi"] is not None else "—"
            ks_s = f"{v['ks']:.3f}" if v["ks"] is not None else "—"
            lines.append(f"| {k} | {psi_s} | {psi_level(v['psi'])} | {ks_s} |")
        lines.append("")
        for h in (1, 5):
            l = d["labels"].get(h)
            if l is None:
                continue
            sr = (f"{l['sigma_ratio']:.2f}" if l["sigma_ratio"] is not None else "—")
            lines.append(f"- fwd{h} 标签漂移：μ {l['mu_train']:+.5f} → {l['mu_test']:+.5f}"
                         f"（差 {l['mu_diff']:+.5f}）· σ比 {sr}")
        lines.append("")

    lines += ["## 跨折汇总", "",
              "| 特征 | max PSI | 最差折 | 显著折数 | 中度折数 |",
              "|---|---:|---:|---:|---:|"]
    for k in FEATURE_KEYS:
        s = summary["per_feat"][k]
        mp = f"{s['max_psi']:.3f}" if s["max_psi"] is not None else "—"
        am = str(s["argmax_fold"]) if s["argmax_fold"] is not None else "—"
        lines.append(f"| {k} | {mp} | {am} | {s['n_significant']} | {s['n_moderate']} |")
    lines += ["",
              "<sub>口径：与 backtest_walk_forward 同冻结切分与折构造"
              "（split_date_oos + build_wf_folds，label-end purge）。PSI 桶边界取"
              "fold-train 10 分位；离散特征（唯一值 ≤5，est_sign/composite）用类别 PSI；"
              "常数特征诚实跳过 n/a；KS 为参考列。"
              "观察层证据：不绑定 registry/promotion，不改 model_ready。</sub>", ""]
    report = "\n".join(lines) + "\n"
    stamp = time.strftime("%Y%m%d")
    out_md = BASE_DIR / "output" / f"drift_monitor_{stamp}.md"
    out_md.write_text(report, encoding="utf-8")
    (BASE_DIR / "output" / f"drift_monitor_{stamp}.json").write_text(
        json.dumps({"folds": [{k: v for k, v in d.items()} for d in folds_drift],
                    "summary": summary}, ensure_ascii=False, indent=1,
                   default=lambda o: None if o is None else (float(o) if isinstance(o, (np.floating,)) else str(o))),
        encoding="utf-8")
    print(f"\n[ok] 报告 → {out_md}")
    print(f"[done] 耗时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
