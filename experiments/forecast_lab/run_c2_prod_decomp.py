r"""C2 生产轨 RankIC 三拆（pooled / CS / TS，2026-09-18；零网络）。

依据：output/forecast_lab_prereg_C2prod_decomp_20260918.md（判据先写死，跑完不改）。
回答唯一问题：生产口径 WF（HGB → p_up vs fwd5）的 T+5 显著性由哪个尺度撑起。
T+1/T+3 仅对照列，不进判据。

复用存量（八荣八耻④，不重写、不改口径）：
- 样本装载：forecast_outputs/samples_frozen_20260910.jsonl（load_samples 的冻结件，零网络）
- 冻结切分 / WF 折 / 逐折模型 / 特征：backtest_forecast.split_date_oos、
  backtest_walk_forward.build_wf_folds / _fit_one_horizon、backtest_forecast.build_xy
- 秩引擎：backtest_forecast.rank_ic；日块工具：run_m0_power.date_groups
- socket 守卫：run_t3_panel_power._guard_network（与 C1 同款）

三拆定义（预注册 §三，与 C1 逐字同）：
- pooled：全体 OOS 一次 rank_ic；CI = 按「日」整块有放回重抽样 B=1000。
- CS：日宽 ≥4 的日逐日 rank_ic；主统计 = 日 IC 均值；CI = 对「日」重抽样。
- TS：成员内 n ≥60 逐员 rank_ic；主统计 = 成员 IC 均值；CI = 对「成员」重抽样。
敏感性（§五，只报数不裁决）：CS 用日宽 ≥3；TS 不剔 n<60（全 4 成员）。

用法：
  .\.venv-lab\Scripts\python.exe -X utf8 experiments\forecast_lab\run_c2_prod_decomp.py
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "experiments" / "forecast_lab"))

import numpy as np                                        # noqa: E402

from backtest_forecast import rank_ic, split_date_oos, build_xy   # noqa: E402
import backtest_walk_forward as wfw                       # noqa: E402
import run_t3_panel_power as t3                           # noqa: E402
from run_m0_power import date_groups, SEED                # noqa: E402

# ---- 预注册 §二 锁死参数
SAMPLES_JSONL = BASE / "forecast_outputs" / "samples_frozen_20260910.jsonl"
EXPECT_SHA = "0d59f663a5deaf936157b1dfa54045d8b3f5e5c77465caac987f68868be87d34"
HORIZONS = (1, 3, 5)
MIN_CS_WIDTH = 4
MIN_TS_N = 60
N_BOOT = 1000
TS_POS_GATE = 2 / 3
WINDOW_DAYS = 63
OUT_JSON = BASE / "forecast_outputs" / "c2_prod_decomp_20260918.json"


def wf_oos_predictions(folds, h: int, flat_margin: float) -> dict | None:
    """逐折调生产原函数 _fit_one_horizon + build_xy → 拼 pooled OOS 预测。

    与 backtest_walk_forward.main 的 [2]/[3] 段同口径：模型 = HGB(p_up)，
    真值 = fwd{h}（build_xy 的 yret），行身份 = XYBatch.dates / .funds。
    """
    preds, trues, dates, members = [], [], [], []
    for fd in folds:
        clf = wfw._fit_one_horizon(fd["train"], h, flat_margin)
        if clf is None:
            continue
        XY = build_xy(fd["test"], h, flat_margin)
        if XY is None or len(XY.yret) < 10:
            continue
        preds.extend(clf.predict_proba(XY.X)[:, 2].tolist())
        trues.extend(XY.yret.tolist())
        dates.extend(XY.dates)
        members.extend(XY.funds)
    if len(preds) < 30:
        return None
    return {"preds": np.asarray(preds, float), "trues": np.asarray(trues, float),
            "dates": dates, "members": members, "n": len(preds)}


def _boot_mean_ic(ics: np.ndarray, n_boot: int, seed: int) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    means = np.array([ics[rng.integers(0, len(ics), size=len(ics))].mean()
                      for _ in range(n_boot)])
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def decompose(base: dict, min_cs=MIN_CS_WIDTH, min_ts=MIN_TS_N) -> dict:
    pred, y = base["preds"], base["trues"]
    dates, members = np.asarray(base["dates"]), np.asarray(base["members"])

    pooled_ic = rank_ic(list(pred), list(y))
    groups = date_groups(list(dates))
    rng = np.random.default_rng(SEED)
    boots = [rank_ic(list(pred[np.concatenate(g)]), list(y[np.concatenate(g)]))
             for g in ([groups[c] for c in rng.integers(0, len(groups), size=len(groups))]
                       for _ in range(N_BOOT))]
    pb = np.asarray(boots, float)
    pooled = {"ic": round(pooled_ic, 4), "n": base["n"], "n_days": len(groups),
              "ci95": [round(float(np.percentile(pb, 2.5)), 4),
                       round(float(np.percentile(pb, 97.5)), 4)]}

    # CS 可测性守卫（2026-09-18 实跑暴露）：backtest_forecast.rank_ic 对 n<10 直接
    # return 0.0（小样本保护）。生产轨 fund_pool 仅 4 只 ⇒ 日宽恒 <10 ⇒ 逐日 IC 全为
    # 假零。此时 CS **不可测**，必须显式标记，不得以 0.0 冒充「无信号」。
    widths = [int((dates == d).sum()) for d in sorted(set(dates.tolist()))]
    cs_computable = bool([w for w in widths if w >= min_cs]) and min_cs >= 10
    day_ics = [rank_ic(list(pred[dates == d]), list(y[dates == d]))
               for d in sorted(set(dates.tolist()))
               if int((dates == d).sum()) >= min_cs] if cs_computable else []
    di = np.asarray(day_ics, float)
    ci = _boot_mean_ic(di, N_BOOT, SEED) if len(di) else (float("nan"),) * 2
    cs = {"min_width": min_cs, "computable": cs_computable,
          "max_day_width": int(max(widths)) if widths else 0,
          "n_days": int(len(di)),
          "mean_ic": round(float(di.mean()), 4) if len(di) else None,
          "ci95": [round(ci[0], 4), round(ci[1], 4)],
          "pos_day_frac": round(float((di > 0).mean()), 4) if len(di) else None}

    mem_ics = [rank_ic(list(pred[members == mb]), list(y[members == mb]))
               for mb in sorted(set(members.tolist()))
               if int((members == mb).sum()) >= min_ts]
    mi = np.asarray(mem_ics, float)
    ci2 = _boot_mean_ic(mi, N_BOOT, SEED) if len(mi) else (float("nan"),) * 2
    ts = {"min_n": min_ts, "n_members": int(len(mi)),
          "mean_ic": round(float(mi.mean()), 4) if len(mi) else None,
          "median_ic": round(float(np.median(mi)), 4) if len(mi) else None,
          "pos_frac": round(float((mi > 0).mean()), 4) if len(mi) else None,
          "ci95": [round(ci2[0], 4), round(ci2[1], 4)]}

    return {"pooled": pooled, "cs": cs, "ts": ts}


def verdict(t5: dict) -> dict:
    cs_ok = t5["cs"].get("computable", True)
    # CS 不可测时 B1 记 None（不得记 False——「测不了」与「不显著」是两件事）
    b1 = (t5["cs"]["ci95"][0] == t5["cs"]["ci95"][0] and t5["cs"]["ci95"][0] > 0) if cs_ok else None
    b2 = (t5["ts"]["mean_ic"] is not None and t5["ts"]["mean_ic"] > 0
          and t5["ts"]["pos_frac"] >= TS_POS_GATE)
    pooled_sig = t5["pooled"]["ci95"][0] > 0
    b3 = pooled_sig and not b1 and not b2
    if not cs_ok:
        return {"B1_cs_sig": None, "B2_ts_sig": b2, "B3_pooled_inflation": None,
                "pooled_sig": pooled_sig, "cs_computable": False,
                "conclusion": (
                    "CS 尺度在生产轨**结构性不可测**：pool 日宽最大 "
                    f"{t5['cs'].get('max_day_width')} < rank_ic 的 n<10 保护阈值，"
                    "逐日 IC 会全部退化为假 0。B1/B3 因此不评估。"
                    "本文在此轨只能给 TS 一维 + pooled 对照，三拆结论以 C1（D-lite 面板）为准。")}
    if b1 and b2:
        grade = "B5 双尺度皆成立：登记供 09-26 重估输入（本次不升格）"
    elif b1:
        grade = "B4 仅 CS 成立 → README 注脚只写横截面尺度"
    elif b2:
        grade = "B4 仅 TS 成立 → README 注脚只写时序尺度"
    elif b3:
        grade = ("B3 pooled 膨胀登记：T+5 显著性主要来自尺度混合，"
                 "README 表述细化为「pooled 显著，单尺度未分离出可操作信号」")
    else:
        grade = "无判据命中（pooled 亦不显著）→ 如实登记，交 09-26 重估"
    return {"B1_cs_sig": b1, "B2_ts_sig": b2, "B3_pooled_inflation": b3,
            "pooled_sig": pooled_sig, "conclusion": grade}


def main() -> int:
    t0 = datetime.now()
    t3._guard_network()                          # [0] 依赖齐 → 上零网络守卫

    raw = SAMPLES_JSONL.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    print("== [0] 冻结样本 sha256 校验 ==")
    if sha != EXPECT_SHA:
        print(f"  [ABORT] sha 漂移 {sha[:16]}… != {EXPECT_SHA[:16]}…")
        return 1
    samples = sorted((json.loads(l) for l in raw.decode("utf-8").splitlines() if l.strip()),
                     key=lambda s: (s["date"], s["fund"]))
    print(f"  {len(samples)} 行 sha 一致")

    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    fc = cfg.get("forecast", {})
    flat_margin = fc.get("prob_flat_margin", 0.003)
    max_h = max(fc.get("horizons", [1, 3, 5]))

    train_all, oos, oos_start = split_date_oos(samples, max_horizon=max_h)
    folds = wfw.build_wf_folds(samples, oos, oos_start,
                               window_days=WINDOW_DAYS, max_horizon=max_h)
    print(f"== [1] 冻结切分 oos_start={oos_start} train={len(train_all)} oos={len(oos)} "
          f"· WF {len(folds)} 折 ==")
    for i, f in enumerate(folds, 1):
        print(f"  折 {i}: {f['window'][0]} ~ {f['window'][-1]} n_train={len(f['train'])}")
    if len(folds) != 4:
        print("  [ABORT] 折数 != 4，与既有 WF 报告不同窗，先核对再跑")
        return 1

    out = {"kind": "c2_prod_rankic_decomposition",
           "prereg": "output/forecast_lab_prereg_C2prod_decomp_20260918.md",
           "created_at": t0.strftime("%Y-%m-%dT%H:%M:%S"),
           "samples_file": SAMPLES_JSONL.name, "samples_sha256": sha,
           "n_samples": len(samples), "oos_start": oos_start,
           "window_days": WINDOW_DAYS, "n_folds": len(folds),
           "flat_margin": flat_margin,
           "feature_space": "FEATURE_KEYS 7 + B1 mask = 14 dims",
           "model": "HGB max_iter=200/lr=0.08/depth=3 rs=42（生产 _fit_one_horizon 原函数）",
           "min_cs_width": MIN_CS_WIDTH, "min_ts_n": MIN_TS_N,
           "n_boot": N_BOOT, "seed": SEED,
           "horizons": {}, "sensitivity": {}, "verdict_T5": None}

    bases: dict[int, dict] = {}
    print("== [2] 逐端点三拆 ==")
    for h in HORIZONS:
        base = wf_oos_predictions(folds, h, flat_margin)
        if base is None:
            print(f"  T+{h}: 预测不足 → 跳过")
            continue
        bases[h] = base
        r = decompose(base)
        tag = " ←主判据" if h == 5 else ""
        print(f"  T+{h}: pooled={r['pooled']['ic']:+.4f} CI{r['pooled']['ci95']} "
              f"| CS mean={r['cs']['mean_ic']} CI{r['cs']['ci95']} ({r['cs']['n_days']}日) "
              f"| TS mean={r['ts']['mean_ic']} pos={r['ts']['pos_frac']} "
              f"CI{r['ts']['ci95']} ({r['ts']['n_members']}员){tag}")
        out["horizons"][f"T+{h}"] = r

    if 5 not in bases:
        print("[fail] T+5 无可用 OOS 预测")
        return 1

    # §五 可复现：T+5 全流程重跑一次，主统计 ≤1e-9
    r5 = out["horizons"]["T+5"]
    b2x = decompose(wf_oos_predictions(folds, 5, flat_margin))
    for k in ("pooled", "cs", "ts"):
        for kk, vv in b2x[k].items():
            if isinstance(vv, (int, float)) and isinstance(r5[k].get(kk), (int, float)):
                if abs(float(vv) - float(r5[k][kk])) > 1e-9:
                    print(f"  [ABORT] 可复现失败 {k}.{kk}: {vv} != {r5[k][kk]}")
                    return 1
    print("  [repro] T+5 双跑一致（≤1e-9）")

    # §五 敏感性：CS 日宽≥3；TS 全成员（不剔 n<60）——只报数不裁决
    print("== [3] 敏感性（只报数不裁决） ==")
    base5 = bases[5]
    d = decompose(base5, min_cs=3)
    out["sensitivity"]["T5_cs_width3"] = {"mean_ic": d["cs"]["mean_ic"],
                                          "ci95": d["cs"]["ci95"], "n_days": d["cs"]["n_days"]}
    d2 = decompose(base5, min_ts=1)
    out["sensitivity"]["T5_ts_all_members"] = {"mean_ic": d2["ts"]["mean_ic"],
                                               "ci95": d2["ts"]["ci95"],
                                               "pos_frac": d2["ts"]["pos_frac"],
                                               "n_members": d2["ts"]["n_members"]}
    print(f"  CS(宽≥3): mean={d['cs']['mean_ic']} CI{d['cs']['ci95']} ({d['cs']['n_days']}日)")
    print(f"  TS(全4员): mean={d2['ts']['mean_ic']} pos={d2['ts']['pos_frac']} CI{d2['ts']['ci95']}")

    out["verdict_T5"] = verdict(r5)
    print(f"\n判定：{out['verdict_T5']['conclusion']}")
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[ok] → {OUT_JSON}  耗时 {round((datetime.now() - t0).total_seconds())}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
