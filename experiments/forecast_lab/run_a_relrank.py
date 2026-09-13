r"""候选 A · rel_rank（相对收益标签专家）· 预注册执行脚本（2026-09-13）。

预注册（跑前写死，跑完不改）：output/forecast_lab_prereg_A_relrank_20260913.md
复用：experiments/forecast_lab/run_m0_power.py 的协议件（特征矩阵 / 标签 / LGB 拟合 /
      日块配对 bootstrap 引擎 PairedBoot / 折构造常量），**不重写协议**。

唯一变量：训练标签（绝对 fwd5 vs 同日横截面超额）。
诊断用相对标签（rel_mean / rel_med）对**所有臂共用同一把尺子**（含 base），
避免某个臂在自己训练用的标签上占便宜。

离线纪律：零网络；只读 frozen 0910 样本；不写任何 config / registry / 生产文件。

用法：
  .\.venv-lab\Scripts\python.exe -X utf8 experiments\forecast_lab\run_a_relrank.py
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "experiments" / "forecast_lab"))

import numpy as np                                              # noqa: E402

import run_m0_power as m0                                       # noqa: E402
from backtest_forecast import rank_ic, split_date_oos            # noqa: E402
from backtest_walk_forward import build_wf_folds                 # noqa: E402

H = m0.HORIZON
STAMP = datetime.now().strftime("%Y%m%d")
JSONL = BASE / "forecast_outputs" / "samples_frozen_20260910.jsonl"
META = BASE / "forecast_outputs" / "samples_frozen_20260910.meta.json"
OUT = BASE / "forecast_outputs" / f"cand_a_relrank_{STAMP}.json"

# D1-c（Summer 09-13 16:59 拍板）G2-⑤′ 用途锁定条款（预注册文本，不得由实现侧放宽）
G2_USAGE_LOCK = ("relative_pool_only_no_abs_display："
                 "G2 通过者仅可作 decision_engine.relative_pool（权重 0.15）的候选输入；"
                 "不得进入绝对收益展示或下单链路；接入须另行预注册。")

# v2 §三.3 实测映射表（IC 序列 ρ → MDE 档位），仅作「功效够不够」的参照
MDE_BANDS = [(1.00, 0.000), (0.988, 0.014), (0.943, 0.030), (0.834, 0.051),
             (0.664, 0.074), (0.454, 0.096), (0.242, 0.114), (0.038, 0.128)]


def mde_band(rho: float) -> float:
    """给定实测 ρ_IC，取 v2 映射表中最接近的档位（不插值、不外推）。"""
    return min(MDE_BANDS, key=lambda kv: abs(kv[0] - rho))[1]


def label_map_abs(samples) -> dict:
    return {(s["fund"], s["date"]): float(s[f"fwd{H}"])
            for s in samples if s.get(f"fwd{H}") is not None}


def label_map_rel(samples, how: str = "mean") -> dict:
    """同日横截面超额标签：fwd5 − 同日池内均值/中位数（within-date，折内完整）。"""
    by = defaultdict(list)
    for s in samples:
        if s.get(f"fwd{H}") is not None:
            by[s["date"]].append(s)
    out = {}
    for d, ss in by.items():
        vals = np.array([float(s[f"fwd{H}"]) for s in ss], dtype=float)
        centre = float(vals.mean() if how == "mean" else np.median(vals))
        for s, v in zip(ss, vals):
            out[(s["fund"], d)] = float(v) - centre
    return out


def run_arm(folds, train_lmap: dict, rel_mean: dict, rel_med: dict) -> dict:
    """逐折按 {train_lmap} 重训；pooled 行身份（date/fund）与 base 严格同序。"""
    preds, yabs, yrel, yrelm, dates, funds, per_fold = [], [], [], [], [], [], []
    for fd in folds:
        tr, te = m0.with_label(fd["train"]), m0.with_label(fd["test"])
        if len(tr) < 100 or len(te) < 2:
            continue
        ytr = np.array([train_lmap[(s["fund"], s["date"])] for s in tr], dtype=float)
        p = np.asarray(m0.lgb_fit_predict(m0.matrix_existing(tr), ytr,
                                          m0.matrix_existing(te)), dtype=float)
        ya = np.array([float(s[f"fwd{H}"]) for s in te], dtype=float)
        yr = np.array([rel_mean[(s["fund"], s["date"])] for s in te], dtype=float)
        yrm = np.array([rel_med[(s["fund"], s["date"])] for s in te], dtype=float)
        per_fold.append({"test_start": fd["test_start"], "n_test": len(te),
                         "ic_abs": round(rank_ic(list(p), list(ya)), 4),
                         "ic_rel": round(rank_ic(list(p), list(yr)), 4),
                         "ic_rel_med": round(rank_ic(list(p), list(yrm)), 4)})
        preds.extend(list(p)); yabs.extend(ya); yrel.extend(yr); yrelm.extend(yrm)
        dates.extend([s["date"] for s in te]); funds.extend([s["fund"] for s in te])
    return {"n": len(preds), "preds": np.asarray(preds, dtype=float),
            "y_abs": np.asarray(yabs, dtype=float),
            "y_rel": np.asarray(yrel, dtype=float),
            "y_rel_med": np.asarray(yrelm, dtype=float),
            "dates": dates, "funds": funds, "per_fold": per_fold}


def ic_series(pb, x: np.ndarray) -> np.ndarray:
    """候选在 pb 的同一批重抽样下的 IC 序列。"""
    return pb._corr(pb._z(np.asarray(x, dtype=float)))


def summarise(name, arm, y_key, fold_key, pb, base_ic, base_fold) -> dict:
    ic = rank_ic(list(arm["preds"]), list(arm[y_key]))
    d = pb.diffs(arm["preds"])
    rho = float(np.corrcoef(pb.icb, ic_series(pb, arm["preds"]))[0, 1])
    folds_cmp = [{"test_start": c["test_start"], "cand": c[fold_key], "base": b[fold_key],
                  "not_worse": c[fold_key] >= b[fold_key] - 1e-9}
                 for c, b in zip(arm["per_fold"], base_fold)]
    n_not_worse = sum(1 for f in folds_cmp if f["not_worse"])
    delta = float(ic - base_ic)
    row = {
        "arm": name, "endpoint": y_key, "n": arm["n"],
        "ic": round(float(ic), 4), "base_ic": round(float(base_ic), 4),
        "delta": round(delta, 4),
        "paired_mean_delta": round(float(d.mean()), 4),
        "pct2_5_D": round(float(np.percentile(d, 2.5)), 4),
        "pct10_D": round(float(np.percentile(d, 10)), 4),
        "gate2_current": bool(np.percentile(d, 2.5) > 0),
        "gate_proposed": bool(delta >= 0.05 and np.percentile(d, 10) > 0
                              and n_not_worse >= 3),
        "rho_ic_vs_base": round(rho, 4), "mde_band": mde_band(rho),
        "n_folds_not_worse": n_not_worse, "folds": folds_cmp,
    }
    print(f"  [{y_key}] {name:8s} IC={row['ic']:+.4f} (base {row['base_ic']:+.4f}, "
          f"Δ={row['delta']:+.4f})  pct2.5(D)={row['pct2_5_D']:+.4f}  "
          f"pct10(D)={row['pct10_D']:+.4f}  ρ={row['rho_ic_vs_base']:+.3f} "
          f"(MDE档≈{row['mde_band']})  逐折不劣 {n_not_worse}/4")
    return row


def main() -> int:
    t0 = datetime.now()
    print("== [0] 冻结样本校验 ==")
    raw = JSONL.read_text(encoding="utf-8")
    sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    meta = json.loads(META.read_text(encoding="utf-8"))
    if not (sha == m0.SAMPLES_SHA_EXPECT == meta["sha256"]):
        print(f"  [ABORT] sha256 不一致：{sha[:16]}…")
        return 1
    samples = [json.loads(l) for l in raw.splitlines() if l.strip()]
    print(f"  {len(samples)} 行，sha256 双校验通过（{sha[:16]}…）")

    print("== [1] 折协议（P1/M0 同构）==")
    _tr, oos, oos_start = split_date_oos(samples, ratio=0.8, max_horizon=H)
    folds = build_wf_folds(samples, oos, oos_start, window_days=63, max_horizon=H)
    print(f"  oos_start={oos_start}  OOS {len(oos)}  折数 {len(folds)}")

    abs_map = label_map_abs(samples)
    rel_mean = label_map_rel(samples, "mean")
    rel_med = label_map_rel(samples, "median")
    print(f"  标签构建：abs {len(abs_map)}  rel_mean {len(rel_mean)}  rel_med {len(rel_med)}")

    print("== [2] 自校验：base 臂（训练=绝对标签）必须逐位复现 m0.run_wf ==")
    base = run_arm(folds, abs_map, rel_mean, rel_med)
    base_ref = m0.run_wf(folds)
    same = (base["n"] == base_ref["n"]
            and np.allclose(base["preds"], base_ref["preds"])
            and np.allclose(base["y_abs"], base_ref["trues"])
            and base["dates"] == base_ref["dates"])
    print(f"  预测逐位一致 = {same}  n={base['n']}")
    if not same:
        print("  [ABORT] base 未复现 M0 协议——先查因，不出候选结论。")
        return 1
    base_ic = rank_ic(list(base["preds"]), list(base["y_abs"]))
    base_ic_rel = rank_ic(list(base["preds"]), list(base["y_rel"]))
    base_ic_relm = rank_ic(list(base["preds"]), list(base["y_rel_med"]))
    if abs(base_ic_rel - base_ic) < 1e-9:
        print("  [ABORT] 诊断标签与绝对标签同值 → 标签构建有 bug。")
        return 1
    print(f"  base IC_abs={base_ic:+.4f}（P1={m0.BASELINE_P1:+.4f}）  "
          f"IC_rel_mean={base_ic_rel:+.4f}  IC_rel_med={base_ic_relm:+.4f}")
    print(f"  逐折 IC_abs={[f['ic_abs'] for f in base['per_fold']]}")

    print("== [3] 候选臂（唯一变量 = 训练标签）==")
    arms = [("A_mean", run_arm(folds, rel_mean, rel_mean, rel_med)),
            ("A_med", run_arm(folds, rel_med, rel_mean, rel_med))]

    groups = m0.date_groups(base["dates"])
    pb_abs = m0.PairedBoot(base["preds"], base["y_abs"], groups, m0.N_BOOT, m0.SEED)
    pb_rel = m0.PairedBoot(base["preds"], base["y_rel"], groups, m0.N_BOOT, m0.SEED)
    print(f"  PairedBoot 就绪：B={pb_abs.idx.shape[0]}（与 scipy 等价性自检通过）")

    print("-- primary 端点：对 fwd5（绝对）的 pooled RankIC（与准入门同一把尺子）--")
    rows_abs = [summarise(n, a, "y_abs", "ic_abs", pb_abs, base_ic, base["per_fold"])
                for n, a in arms]
    print("-- secondary 端点：对相对标签（all arms 共用 rel_mean 尺子，诊断用）--")
    rows_rel = [summarise(n, a, "y_rel", "ic_rel", pb_rel, base_ic_rel, base["per_fold"])
                for n, a in arms]
    print("-- robustness / G2-④′：对 rel_med 尺子（跨尺子稳健，需配对 CI）--")
    pb_relm = m0.PairedBoot(base["preds"], base["y_rel_med"], groups,
                            m0.N_BOOT, m0.SEED)
    rel_med_rows = []
    for n, a in arms:
        ic_m = rank_ic(list(a["preds"]), list(a["y_rel_med"]))
        dm = pb_relm.diffs(a["preds"])
        rel_med_rows.append({
            "arm": n, "base_ic": round(float(base_ic_relm), 4),
            "ic": round(float(ic_m), 4),
            "delta": round(float(ic_m - base_ic_relm), 4),
            "pct2_5_D": round(float(np.percentile(dm, 2.5)), 4)})
    for r in rel_med_rows:
        print(f"  [rel_med] {r['arm']:8s} IC={r['ic']:+.4f} (base {r['base_ic']:+.4f}, "
              f"Δ={r['delta']:+.4f})  pct2.5(D)={r['pct2_5_D']:+.4f}")

    print("== [4] 结论（判据读数一律并列，不替 Summer 预选）==")

    def line(tag, rows):
        best = max(rows, key=lambda r: r["delta"])
        g2 = [r["arm"] for r in rows if r["gate2_current"]]
        gp = [r["arm"] for r in rows if r["gate_proposed"]]
        if g2:
            note = "判出（配对 CI 下界 > 0）"
        elif abs(best["delta"]) < best["mde_band"]:
            note = "未判出，且 Δ 落在该臂功效盲区内 → 本次**不充分**（非「无效果」）"
        else:
            note = "未判出，且 Δ 已超出盲区 → 可视为无效果"
        print(f"  [{tag}] 最佳臂 {best['arm']}  Δ={best['delta']:+.4f}  "
              f"（该臂 MDE 档≈{best['mde_band']}，ρ={best['rho_ic_vs_base']:+.3f}）")
        print(f"      现行门槛②（pct2.5>0）通过 = {g2 or '无'}")
        print(f"      建议替代判据通过 = {gp or '无'}")
        print(f"      判定：{note}")

    line("primary/绝对 fwd5", rows_abs)
    line("secondary/相对标签", rows_rel)

    # ---------- D1-c（Summer 09-13 16:59 拍板）：双端点双门槛正式判读 ----------
    # 预注册：output/forecast_lab_prereg_rules_20260913.md §一 R1（本文之前落盘）。
    print("== [5] D1-c 双门槛裁决（G1 绝对资格 / G2 相对资格）==")
    relm_by_arm = {r["arm"]: r for r in rel_med_rows}
    gates = []
    for r in rows_rel:
        rm = relm_by_arm[r["arm"]]
        g2 = {
            "arm": r["arm"],
            "c1_paired_ci_lower_gt0": bool(r["pct2_5_D"] > 0),
            "c2_folds_not_worse_4of4": bool(r["n_folds_not_worse"] == 4),
            "c3_cross_ruler_same_sign_and_ci_gt0": bool(
                rm["delta"] > 0 and rm["pct2_5_D"] > 0),
            "c4_usage_lock": "relative_pool_only_no_abs_display",
        }
        g2["G2_pass"] = bool(g2["c1_paired_ci_lower_gt0"]
                             and g2["c2_folds_not_worse_4of4"]
                             and g2["c3_cross_ruler_same_sign_and_ci_gt0"])
        gates.append(g2)
        print(f"  [G2/secondary] {r['arm']:8s} ②′CI>0={g2['c1_paired_ci_lower_gt0']}  "
              f"③′逐折4/4={g2['c2_folds_not_worse_4of4']}  "
              f"④′跨尺子稳健={g2['c3_cross_ruler_same_sign_and_ci_gt0']}"
              f"（rel_med Δ={rm['delta']:+.4f} CI下界={rm['pct2_5_D']:+.4f}）  "
              f"→ G2 {'✅通过' if g2['G2_pass'] else '❌未过'}")
    abs_pass = {}
    for r in rows_abs:
        g1_pass = bool(r["gate2_current"])
        abs_pass[r["arm"]] = g1_pass
        if g1_pass:
            v1 = "✅通过"
        elif abs(r["delta"]) < r["mde_band"]:
            v1 = f"❌未过（Δ={r['delta']:+.4f} 落在盲区 {r['mde_band']} 内 → 记「证据不充分」）"
        else:
            v1 = f"❌未过（Δ={r['delta']:+.4f} 已超盲区 {r['mde_band']} → 可视为无效果）"
        print(f"  [G1/primary]   {r['arm']:8s} Δ={r['delta']:+.4f}  "
              f"pct2.5(D)={r['pct2_5_D']:+.4f}  逐折不劣 {r['n_folds_not_worse']}/4"
              f"  → G1 {v1}")
    for gg in gates:
        gg["G1_pass"] = abs_pass.get(gg["arm"], False)
    print("  用途锁定（G2-⑤′）：G2 通过者**不得**进入绝对收益展示/下单链路，")
    print("      仅可作 decision_engine.relative_pool（权重 0.15）的候选输入，接入另行预注册。")

    out = {"kind": "forecast_lab_cand_a_relrank",
           "prereg": "output/forecast_lab_prereg_A_relrank_20260913.md",
           "created_at": t0.isoformat(timespec="seconds"),
           "samples_sha256": sha, "oos_start": oos_start,
           "n_folds": len(folds), "n_boot": m0.N_BOOT, "n": base["n"],
           "base": {"ic_abs": round(float(base_ic), 4),
                    "ic_rel_mean": round(float(base_ic_rel), 4),
                    "ic_rel_med": round(float(base_ic_relm), 4),
                    "per_fold": base["per_fold"], "reproduced_m0": bool(same)},
           "endpoint_abs": rows_abs, "endpoint_rel": rows_rel,
           "endpoint_rel_med_robust": rel_med_rows,
           "d1c_gates": {
               "rule_doc": "output/forecast_lab_prereg_rules_20260913.md §一 R1",
               "decided_by": "Summer 2026-09-13 16:59（D1=c 双端点双门槛）",
               "G1_primary": [{"arm": r["arm"], "pass": bool(r["gate2_current"]),
                               "delta": r["delta"], "pct2_5_D": r["pct2_5_D"],
                               "mde_band": r["mde_band"],
                               "verdict": ("判出" if r["gate2_current"] else
                                           ("证据不充分（Δ 落在盲区）"
                                            if abs(r["delta"]) < r["mde_band"]
                                            else "可视为无效果"))}
                              for r in rows_abs],
               "G2_secondary": gates,
               "usage_lock": G2_USAGE_LOCK,
           },
           "note": "首跑 secondary 端点实现有 bug（base 臂 y_rel 误用绝对标签），已修正；"
                   "primary 端点与判据不受影响。",
           "elapsed_sec": round((datetime.now() - t0).total_seconds(), 1)}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  JSON → {OUT.relative_to(BASE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
