r"""T+3 RankIC 三拆（time-series / cross-sectional / pooled，2026-09-18；零网络）。

依据：output/forecast_lab_prereg_T3decomp_20260918.md（判据先写死，跑完不改）。
回答唯一问题：冻结 D-lite 面板上 LGB(a158-50+mask) 逐折 WF 的 OOS 预测，
T+3 预测力在 CS / TS / pooled 三尺度各自是否成立（T+1/T+5 仅对照列，不进判据）。

复用存量（八荣八耻④，不重写）：
- 面板装载 + sha256 校验 + 折构造 + 掩码矩阵：run_t3_panel_power（load_panel / make_folds / matrix）
- LGB 超参：run_m0_power.LGB_PARAMS / N_ROUNDS / SEED
- 秩引擎：backtest_forecast.rank_ic（scipy 精确）
- socket 守卫：run_t3_panel_power._guard_network（chk16 同款实测口径）

三拆定义（§三逐字落实）：
- pooled：全体 OOS 样本一次 rank_ic；CI = 按「日」整块有放回重抽样 B=1000。
- CS：日宽 ≥4 的日子逐日 rank_ic；主统计 = 日 IC 均值；CI = 对「日」重抽样。
- TS：成员内 n ≥60 的成员逐员 rank_ic；主统计 = 成员 IC 均值；CI = 对「成员」重抽样
  （成员是 TS 的相关单位；同日不同成员在 TS 口径下独立，不整日绑定）。

用法：
  .\.venv-lab\Scripts\python.exe -X utf8 experiments\forecast_lab\run_t3_decomp.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "experiments" / "forecast_lab"))

import numpy as np                                        # noqa: E402

import run_t3_panel_power as t3                           # noqa: E402
from run_m0_power import LGB_PARAMS, N_ROUNDS, SEED       # noqa: E402
from backtest_forecast import rank_ic                     # noqa: E402

# §二 事前锁死参数
PANEL = "panel_dlite_v2_20260917"
EXPECT_SHA = "c67dc6b97c0d2c3a3e89a66c208b6c91a84686c4c0f997ac53c582c22d0d8443"
OOS_START = t3.OOS_START           # 2025-04-30（§七 写死不动）
HORIZONS = (1, 3, 5)
MIN_CS_WIDTH = 4                   # §三：日宽 <4 的日剔除
MIN_TS_N = 60                      # §三：成员样本 <60 不进 TS
N_BOOT = 1000
TS_POS_GATE = 2 / 3                # §四 A2：正值成员占比阈值
OUT_JSON = BASE / "forecast_outputs" / "t3_decomp_20260918.json"


# ---------------------------------------------------------------- 基线（按端点参数化的 run_base）
def run_base_h(folds, H: int) -> dict:
    """逐折 expanding 训练 LGB(a158-50+mask) → 拼 pooled OOS 预测（同 t3.run_base，仅端点可变）。"""
    import lightgbm as lgb
    key = f"fwd{H}"
    preds, trues, dates, members = [], [], [], []
    for fd in folds:
        tr = [s for s in fd["train"] if s.get(key) is not None]
        te = [s for s in fd["test"] if s.get(key) is not None]
        if len(tr) < 100 or len(te) < 10:
            continue
        ytr = np.asarray([float(s[key]) for s in tr], dtype=float)   # 端点可变（勿用 t3.targets：其硬编码 fwd5）
        p = lgb.train(dict(LGB_PARAMS), lgb.Dataset(t3.matrix(tr), label=ytr),
                      num_boost_round=N_ROUNDS).predict(t3.matrix(te))
        preds.extend(list(p))
        trues.extend([float(s[key]) for s in te])
        dates.extend([s["date"] for s in te])
        members.extend([s["member"] for s in te])
    return {"preds": np.asarray(preds, float), "trues": np.asarray(trues, float),
            "dates": dates, "members": members, "n": len(preds)}


# ---------------------------------------------------------------- 三拆统计
def _boot_mean_ic(ics: np.ndarray, n_boot: int, seed: int) -> tuple[float, float]:
    """对「相关单位」（日 / 成员）整块有放回重抽样 → 均值 CI 2.5/97.5。"""
    rng = np.random.default_rng(seed)
    means = np.array([ics[rng.integers(0, len(ics), size=len(ics))].mean()
                      for _ in range(n_boot)])
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def decompose(base: dict) -> dict:
    pred, y = base["preds"], base["trues"]
    dates, members = np.asarray(base["dates"]), np.asarray(base["members"])

    # --- pooled：全体一次 + 日块重抽样 CI（现行口径的精确化）
    pooled_ic = rank_ic(list(pred), list(y))
    groups = t3.date_groups(list(dates))
    rng = np.random.default_rng(SEED)
    pooled_boots = []
    for _ in range(N_BOOT):
        take = np.concatenate([groups[c] for c in rng.integers(0, len(groups), size=len(groups))])
        pooled_boots.append(rank_ic(list(pred[take]), list(y[take])))
    pb = np.asarray(pooled_boots, float)
    pooled = {"ic": round(pooled_ic, 4), "n": base["n"], "n_days": len(groups),
              "ci95": [round(float(np.percentile(pb, 2.5)), 4),
                       round(float(np.percentile(pb, 97.5)), 4)]}

    # --- CS：日宽 ≥ MIN_CS_WIDTH 的日子逐日 rank_ic → 均值 + 日重抽样 CI
    day_ics = []
    for d in sorted(set(dates.tolist())):
        m = dates == d
        if int(m.sum()) >= MIN_CS_WIDTH:
            day_ics.append(rank_ic(list(pred[m]), list(y[m])))
    di = np.asarray(day_ics, float)
    ci = _boot_mean_ic(di, N_BOOT, SEED) if len(di) else (float("nan"),) * 2
    cs = {"n_days": int(len(di)), "mean_ic": round(float(di.mean()), 4) if len(di) else None,
          "ci95": [round(ci[0], 4), round(ci[1], 4)],
          "pos_day_frac": round(float((di > 0).mean()), 4) if len(di) else None}

    # --- TS：成员内 n ≥ MIN_TS_N 逐员 rank_ic → 均值/中位/正值占比 + 成员重抽样 CI
    mem_ics = []
    for mb in sorted(set(members.tolist())):
        m = members == mb
        if int(m.sum()) >= MIN_TS_N:
            mem_ics.append(rank_ic(list(pred[m]), list(y[m])))
    mi = np.asarray(mem_ics, float)
    ci2 = _boot_mean_ic(mi, N_BOOT, SEED) if len(mi) else (float("nan"),) * 2
    ts = {"n_members": int(len(mi)), "mean_ic": round(float(mi.mean()), 4) if len(mi) else None,
          "median_ic": round(float(np.median(mi)), 4) if len(mi) else None,
          "pos_frac": round(float((mi > 0).mean()), 4) if len(mi) else None,
          "ci95": [round(ci2[0], 4), round(ci2[1], 4)]}

    return {"pooled": pooled, "cs": cs, "ts": ts}


# ---------------------------------------------------------------- 判据（§四，仅裁决 pool14 T+3）
def verdict(t3_res: dict) -> dict:
    cs_lo = t3_res["cs"]["ci95"][0]
    a1 = cs_lo is not None and cs_lo == cs_lo and cs_lo > 0
    mean_ic, pos = t3_res["ts"]["mean_ic"], t3_res["ts"]["pos_frac"]
    a2 = mean_ic is not None and mean_ic > 0 and pos >= TS_POS_GATE
    pooled_lo = t3_res["pooled"]["ci95"][0]
    a3 = pooled_lo > 0 and not a1 and not a2
    grade = ("A1∧A2 → 升格「证据积累」，解锁 C2 生产侧三拆对比" if a1 and a2
             else ("仅 A1（CS 成立）→ 不升格，README 注脚只写横截面尺度"
                   if a1 and not a2 else
                   ("仅 A2（TS 成立）→ 不升格，README 注脚只写时序尺度"
                    if a2 and not a1 else
                    ("A3 pooled 膨胀登记：T+3 不得升格，「仅点估计」维持"
                     if a3 else "三尺度均不显著：维持「仅点估计」"))))
    return {"A1_cs_sig": a1, "A2_ts_sig": a2, "A3_pooled_inflation": a3, "conclusion": grade}


def main() -> int:
    t0 = datetime.now()
    # ---- [0] 依赖齐 → 上零网络守卫（任何 connect 即 RuntimeError）
    t3._guard_network()

    print("== [0] 面板冻结校验（sha256 vs 预注册 §二）==")
    t3.PANEL_JSONL = f"{PANEL}.jsonl"
    t3.PANEL_META = f"{PANEL}.meta.json"
    rows, sha = t3.load_panel()
    if sha != EXPECT_SHA:
        print(f"  [ABORT] sha256 漂移 {sha[:16]}… != {EXPECT_SHA[:16]}…")
        return 1
    meta = json.loads((t3.OUT_DIR / t3.PANEL_META).read_text(encoding="utf-8"))
    print(f"  {len(rows)} 行 sha 一致 · date_max={meta['date_max']} · oos_start={OOS_START}")

    pools = {"pool14_primary": [s for s in rows if s.get("kind") in t3.PRIMARY_KINDS],
             "pool17_sensitivity": rows}
    out = {"kind": "t3_rankic_decomposition",
           "prereg": "output/forecast_lab_prereg_T3decomp_20260918.md",
           "created_at": t0.strftime("%Y-%m-%dT%H:%M:%S"),
           "panel": f"{PANEL}.jsonl", "panel_sha256": sha,
           "panel_date_max": meta["date_max"],
           "oos_start": OOS_START, "window_days": t3.WINDOW_DAYS,
           "feature_space": "a158-50 + B1 mask = 100 dims",
           "min_cs_width": MIN_CS_WIDTH, "min_ts_n": MIN_TS_N,
           "n_boot": N_BOOT, "seed": SEED, "pools": {}}

    for name, pool in pools.items():
        members_n = len({s["member"] for s in pool})
        print(f"== [{name}] {members_n} 员，逐端点三拆 ==")
        folds, _ = t3.make_folds(pool, OOS_START)
        res_h = {}
        for H in HORIZONS:
            base = run_base_h(folds, H)
            r = decompose(base)
            if name == "pool14_primary" and H == 3:
                # §四 可复现：pool14 × T+3 全流程重跑一次，主统计 ≤1e-9
                b2 = decompose(run_base_h(folds, H))
                for k in ("pooled", "cs", "ts"):
                    for kk, vv in b2[k].items():
                        if isinstance(vv, (int, float)) and isinstance(r[k].get(kk), (int, float)):
                            if abs(float(vv) - float(r[k][kk])) > 1e-9:
                                print(f"  [ABORT] 可复现失败 {k}.{kk}: {vv} != {r[k][kk]}")
                                return 1
                print("  [repro] pool14 T+3 双跑一致（≤1e-9）")
            tag = " ←主判据" if (name == "pool14_primary" and H == 3) else ""
            print(f"  T+{H}: pooled={r['pooled']['ic']:+.4f} CI{r['pooled']['ci95']} "
                  f"| CS mean={r['cs']['mean_ic']} CI{r['cs']['ci95']} ({r['cs']['n_days']}日) "
                  f"| TS mean={r['ts']['mean_ic']} pos={r['ts']['pos_frac']} "
                  f"CI{r['ts']['ci95']} ({r['ts']['n_members']}员){tag}")
            res_h[f"T+{H}"] = r
        out["pools"][name] = res_h

    out["verdict_pool14_T3"] = verdict(out["pools"]["pool14_primary"]["T+3"])
    print(f"\n判定：{out['verdict_pool14_T3']['conclusion']}")
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[ok] → {OUT_JSON}  耗时 {round((datetime.now() - t0).total_seconds())}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
