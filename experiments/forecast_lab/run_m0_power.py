r"""M0 · 功效与尺子锁定（方案 0，2026-09-12 执行；Summer 09-11 深夜拍板「3条都按建议执行」）。

依据：output/forecast_lab_candidate_options_20260911.md §方案 0（预注册规格）。
回答唯一问题：**在当前冻结样本量下，门槛②（配对差值 bootstrap CI 下界 > 0）可达吗？
能可靠判出的最小 T+5 RankIC 差值 MDE 是多少？**

离线纪律（零网络，与 P1 相同）
- 样本：forecast_outputs/samples_frozen_20260910.jsonl（sha256 必与 meta 一致，否则中止）。
- K 线指纹与 0910 留档对比：**不一致不中止、如实记录**——M0 的全部计算输入是自包含的
  冻结 JSONL，不依赖当前缓存内容；指纹漂移本身正是「尺子未锁定」的实测证据。
- 净值缓存新鲜度仅记录（跨午夜必然超 TTL，零网络）。任何需重算 est_chg 的后续实验
  （候选 A 重冻结等）必须先人工刷新、重产指纹，不得静默跳过。

判据（先写死，跑完不改）
- 基线：与 P1 完全同口径——split_date_oos(ratio=0.8, max_horizon=5) +
  build_wf_folds(window_days=63) + LGB(existing 7 特征, P1 LGB_PARAMS, seed=42)。
  复现校验：pooled T+5 RankIC 必须 ≈ P1 的 −0.0148（|Δ|≤0.01），否则中止、不出 MDE。
- 门槛②复现（PairedBoot）：按日块有放回重抽 B=1000（seed=42，与生产
  cluster_bootstrap_ci 同构）；每次重抽样**内部 re-rank**（ties 平均秩，与
  scipy.spearmanr 同构，非全局秩近似；引擎自带与 scipy 的等价性断言）；
  D_b = IC_cand(b) − IC_base(b)；**判出 ⇔ D 的 2.5% 分位 > 0**。
- MDE 主判据（独立噪声候选）：cand = ρ·r(y) + √(1−ρ²)·r(ε)，ε 为每 draw 新画的独立
  均匀噪声（与 base 正交）→ 总体 IC≈ρ、样本实现带独立抽样误差，与真训候选同构。
  ρ 网格 0~0.30 步长 0.02，每 ρ 画 N_DRAW=50 个；power(ρ) = 判出频率。
  MDE(80%) = power 跨 0.8 处 meanΔIC 线性插值；MDE(50%) 同理；网格尽头 power<0.8 →
  MDE(80%) 记 None（不可达，不编造插值）。
  注：与 base 正交是**偏保守**口径——真实候选若与 base 共享排序结构，差值方差更小。
- 反例留档（混合候选，不作 MDE）：cand=(1−λ)r(base)+λr(y) 把真标签直接混入候选，
  候选误差与评估重抽样完全共享 → 判出率虚高。09-12 首版误用，改判据后仅作敏感性对照。
  sanity：base vs base 判出率必须 = 0（D 恒为 0，严格 >0 分位不成立）。
- I 类错误实测（permutation）：日块内置换 fwd5 标签 → 4 折重训（同 P1 口径）→ 对
  **原始标签**按门槛②判出；H0 下判出频率应 ≈ 0.05。逐轮判出率 > 0.975 记「轮级误报」；
  轮级误报率 > 0.10 → bootstrap 在该样本结构下失真，MDE 作废、降级「尺子不可信」。
- 尺子可信 = 基线复现通过 ∧ 轮级误报率 ≤ 0.10（指纹一致**不**作前提，单独报告）。
- 分叉阈值（预注册，0911 文档 §四）：MDE(80%) ≤ 0.06 → 门槛可达（09-14/15 跑候选 A）；
  ≥ 0.10 → 不可达（先改判据或候选 D）；之间 = 边界（提请 Summer 定）。

产物：forecast_outputs/mde_sim_<stamp>.json（stdout 由执行方 tee 到 log）

用法：
  .\.venv-lab\Scripts\python.exe -X utf8 experiments\forecast_lab\run_m0_power.py
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "experiments" / "forecast_lab"))

import numpy as np                                        # noqa: E402
import lightgbm as lgb                                    # noqa: E402

from backtest_forecast import rank_ic, split_date_oos     # noqa: E402
from backtest_walk_forward import build_wf_folds          # noqa: E402
from core import forecast_engine                          # noqa: E402
from kline_fingerprint import build_fingerprint           # noqa: E402

HORIZON = 5
SEED = 42
N_BOOT = 1000
N_PERM = 50
N_DRAW = 50
RHO_GRID = [round(float(x), 3) for x in np.arange(0.0, 0.32, 0.02)]
LAMBDA_GRID = [round(float(x), 2) for x in np.arange(0.0, 0.65, 0.05)]
BASELINE_P1 = -0.0148
LGB_PARAMS = dict(objective="regression", learning_rate=0.05, num_leaves=31,
                  min_data_in_leaf=40, feature_fraction=0.8, bagging_fraction=0.8,
                  bagging_freq=1, verbosity=-1, seed=SEED, deterministic=True,
                  num_threads=2, force_row_wise=True)
N_ROUNDS = 300
EXISTING_KEYS = list(forecast_engine.FEATURE_KEYS)
SAMPLES_SHA_EXPECT = "be8e55f02b39a25dc48f2efd67150c895ceebfebfe97c647b00610f61a05b034"


# ---------------------------------------------------------------- 与 P1 同协议
def _mask_row(values) -> list[float]:
    row = []
    for v in values:
        ok = v is not None and isinstance(v, (int, float)) and math.isfinite(float(v))
        row.append(float(v) if ok else 0.0)
        row.append(0.0 if ok else 1.0)
    return row


def matrix_existing(samples) -> np.ndarray:
    return np.array([_mask_row([s.get(k) for k in EXISTING_KEYS]) for s in samples],
                    dtype=float)


def targets(samples) -> np.ndarray:
    return np.array([float(s[f"fwd{HORIZON}"]) for s in samples], dtype=float)


def with_label(samples) -> list[dict]:
    return [s for s in samples if s.get(f"fwd{HORIZON}") is not None]


def lgb_fit_predict(Xtr, ytr, Xte, seed=SEED) -> np.ndarray:
    ds = lgb.Dataset(Xtr, label=ytr)
    params = {**LGB_PARAMS, "seed": seed, "deterministic": True}
    return np.asarray(lgb.train(params, ds,
                                num_boost_round=N_ROUNDS).predict(Xte), dtype=float)


def run_wf(folds) -> dict:
    preds, trues, dates, per_fold = [], [], [], []
    for fd in folds:
        train, test = with_label(fd["train"]), with_label(fd["test"])
        if len(train) < 100 or len(test) < 2:
            continue
        p = lgb_fit_predict(matrix_existing(train), targets(train),
                            matrix_existing(test))
        yte = targets(test)
        per_fold.append({"test_start": fd["test_start"], "n_test": len(test),
                         "rank_ic": round(rank_ic(list(p), list(yte)), 4)})
        preds.extend(list(p))
        trues.extend(list(yte))
        dates.extend([s["date"] for s in test])
    return {"n": len(preds), "per_fold": per_fold, "dates": dates,
            "preds": np.asarray(preds, dtype=float),
            "trues": np.asarray(trues, dtype=float)}


# ---------------------------------------------------------------- 秩引擎
def rank_mean_rows(M: np.ndarray) -> np.ndarray:
    """逐行 ties-平均秩（与 scipy.stats.rankdata(method='average') 行内同构）。"""
    n, m = M.shape
    order = np.argsort(M, axis=1, kind="stable")
    Ms = np.take_along_axis(M, order, axis=1)
    new = np.empty((n, m), dtype=bool)
    new[:, 0] = True
    new[:, 1:] = Ms[:, 1:] != Ms[:, :-1]
    grp = np.cumsum(new, axis=1) - 1                     # 行内组号（排序序）
    pos = np.broadcast_to(np.arange(1, m + 1, dtype=float), (n, m))   # 1-based，同 scipy
    ridx = np.arange(n)[:, None]
    sums = np.zeros((n, m))
    cnt = np.zeros((n, m))
    np.add.at(sums, (ridx, grp), pos)
    np.add.at(cnt, (ridx, grp), 1.0)
    avg_by_grp = np.zeros((n, m))
    nz = cnt > 0
    avg_by_grp[nz] = sums[nz] / cnt[nz]
    avg_sorted = np.take_along_axis(avg_by_grp, grp, axis=1)
    ranks = np.empty((n, m))
    np.put_along_axis(ranks, order, avg_sorted, axis=1)
    return ranks


class PairedBoot:
    """门槛②精确复现：共享重抽样集合；逐重抽样内部 re-rank；判出 ⇔ pct2.5(D)>0。

    日块大小不一 → 各行有效长度不等；用等宽 padding 指向哑元 +inf，秩与点积时
    以 mask 把哑元剔除（哑元排最后、中心化用真实均值 → 结果与变长精确一致）。
    """

    def __init__(self, base_pred: np.ndarray, y: np.ndarray,
                 groups: list[np.ndarray], n_boot: int, seed: int):
        rng = np.random.default_rng(seed)
        picks = [rng.integers(0, len(groups), size=len(groups)) for _ in range(n_boot)]
        mmax = max(int(sum(len(groups[c]) for c in p)) for p in picks)
        idx, mask = [], []
        for p in picks:
            take = np.concatenate([groups[c] for c in p])
            pad = mmax - len(take)
            idx.append(np.r_[take, np.full(pad, len(y))].astype(np.int64))
            mask.append(np.r_[np.ones(len(take), bool), np.zeros(pad, bool)])
        self.idx = np.vstack(idx)                        # (B, mmax)，末位=哑元
        self.mask = np.vstack(mask)                      # (B, mmax)
        self._sentinel = np.full(len(y), math.inf)
        self.zy = self._z(y)
        self.ny = np.linalg.norm(self.zy, axis=1)
        self.icb = self._corr(self._z(base_pred))
        # 与生产 rank_ic（scipy）的等价性自检：抽 3 个重抽样比对
        self._check(y, base_pred)

    def _ext(self, x: np.ndarray) -> np.ndarray:
        return np.r_[x, self._sentinel]

    def _z(self, x: np.ndarray) -> np.ndarray:
        Z = rank_mean_rows(self._ext(x)[self.idx])
        cnt = self.mask.sum(axis=1, keepdims=True)
        mean = np.where(self.mask, Z, 0.0).sum(axis=1, keepdims=True) / cnt
        return np.where(self.mask, Z - mean, 0.0)

    def _corr(self, zx: np.ndarray) -> np.ndarray:
        nx = np.linalg.norm(zx, axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            return (zx * self.zy).sum(axis=1) / np.maximum(nx * self.ny, 1e-300)

    def _check(self, y: np.ndarray, base: np.ndarray) -> None:
        """与生产 rank_ic（scipy）逐重抽样比对，不等价即中止。"""
        for b in (0, 7, 42):
            ii = self.idx[b][self.mask[b]]
            want = rank_ic(list(base[ii]), list(y[ii]))
            got = float(self.icb[b])
            assert abs(want - got) < 1e-9, (
                f"PairedBoot 与 scipy rank_ic 不等价（boot {b}: {want} vs {got}）")

    def diffs(self, x: np.ndarray) -> np.ndarray:
        return self._corr(self._z(x)) - self.icb

    def decide(self, x: np.ndarray) -> bool:
        return bool(np.percentile(self.diffs(x), 2.5) > 0)

    def power(self, X: np.ndarray) -> float:
        return float(np.mean([self.decide(X[k]) for k in range(len(X))]))


def norm_rank(x: np.ndarray) -> np.ndarray:
    """ties-平均秩归一到 [0,1]（候选构造统一标尺）。"""
    r = rank_mean_rows(x[None, :])[0]
    return r / max(float(r.max()), 1e-12)


def date_groups(dates: list[str]) -> list[np.ndarray]:
    da = np.asarray(dates)
    order = np.argsort(da, kind="stable")
    srt = da[order]
    bounds = np.flatnonzero(np.r_[True, srt[1:] != srt[:-1], True])
    return [order[bounds[i]:bounds[i + 1]] for i in range(len(bounds) - 1)]


# ---------------------------------------------------------------- 主流程
def main() -> int:
    t0 = datetime.now()
    stamp = t0.strftime("%Y%m%d")
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    ttl = float(cfg["data"]["cache_ttl_hours"])

    jsonl = BASE / "forecast_outputs" / "samples_frozen_20260910.jsonl"
    meta = json.loads((BASE / "forecast_outputs" /
                       "samples_frozen_20260910.meta.json").read_text(encoding="utf-8"))
    raw = jsonl.read_text(encoding="utf-8")
    sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    print("== [0] 冻结样本校验 ==")
    if not (sha == SAMPLES_SHA_EXPECT == meta["sha256"]):
        print(f"  [ABORT] sha256 不一致：{sha[:16]}…")
        return 1
    samples = [json.loads(l) for l in raw.splitlines() if l.strip()]
    print(f"  {len(samples)} 行，sha256 双校验通过")

    print("== [1] K 线指纹（当前缓存 vs 0910 留档）——仅记录，不作前提 ==")
    kfp_now = build_fingerprint(cfg["fund_pool"])
    kfp_0910 = json.loads((BASE / "forecast_outputs" /
                           "kline_fingerprint_20260910.json").read_text(encoding="utf-8"))
    same_fp = kfp_now.get("aggregate_sha256") == kfp_0910.get("aggregate_sha256")
    print(f"  now {str(kfp_now.get('aggregate_sha256'))[:16]}… vs "
          f"0910 {str(kfp_0910.get('aggregate_sha256'))[:16]}… 一致={same_fp}")

    nav_rep = {}
    for code in cfg["fund_pool"]:
        p = BASE / "data" / "klines" / f"{code}.json"
        age_h = (datetime.now().timestamp() - p.stat().st_mtime) / 3600.0
        nav_rep[code] = {"age_hours": round(age_h, 1), "within_ttl": age_h <= ttl}
    print(f"  净值缓存新鲜度（仅记录，零网络）：{nav_rep}")

    print("== [2] WF 折点复现（P1 同协议）==")
    train_all, oos, oos_start = split_date_oos(samples, ratio=0.8, max_horizon=HORIZON)
    folds = build_wf_folds(samples, oos, oos_start, window_days=63, max_horizon=HORIZON)
    print(f"  oos_start={oos_start}  OOS {len(oos)}  折数 {len(folds)}")

    print(f"== [3] 基线复跑：LGB existing(7)，期望 ≈ P1 {BASELINE_P1:+.4f} ==")
    base = run_wf(folds)
    base_ic = round(float(rank_ic(list(base["preds"]), list(base["trues"]))), 4)
    print(f"  pooled={base_ic:+.4f}  n={base['n']}  "
          f"逐折={[f['rank_ic'] for f in base['per_fold']]}")
    if abs(base_ic - BASELINE_P1) > 0.01:
        print("  [ABORT] 基线未复现 P1——协议漂移，先查因，不出 MDE。")
        return 1

    pb = PairedBoot(base["preds"], base["trues"],
                    date_groups(base["dates"]), N_BOOT, SEED)
    print(f"  PairedBoot 就绪：B={pb.idx.shape[0]}×{pb.idx.shape[1]}（与 scipy 等价性自检通过）")
    print(f"  [sanity] base vs base 判出 = {pb.decide(base['preds'])}（必须 False）；"
          f"base 自身 pct2.5(D) = {np.percentile(pb.diffs(base['preds']), 2.5):+.1e}")

    r_y, r_b = norm_rank(base["trues"]), norm_rank(base["preds"])

    print("== [4a] 混合候选（反例留档：真标签混入 → 判出率虚高）==")
    mix_rows = []
    for lam in LAMBDA_GRID:
        cand = (1 - lam) * r_b + lam * r_y
        d_obs = round(rank_ic(list(cand), list(base["trues"])) - base_ic, 4)
        pwr = pb.power(cand[None, :])
        mix_rows.append({"lambda": lam, "delta_obs": d_obs, "power": round(pwr, 4)})
        print(f"  λ={lam:4.2f}  Δ={d_obs:+.4f}  power={pwr:.3f}")

    print(f"== [4b] MDE 主判据：独立噪声候选（ρ 网格 × N_DRAW={N_DRAW}，正交于 base）==")
    rng = np.random.default_rng(SEED + 7)
    rows = []
    for rho in RHO_GRID:
        cands = np.empty((N_DRAW, base["n"]))
        for k in range(N_DRAW):
            cands[k] = rho * r_y + math.sqrt(max(1 - rho * rho, 0.0)) * norm_rank(
                rng.random(base["n"]))
        deltas = [rank_ic(list(cands[k]), list(base["trues"])) - base_ic
                  for k in range(N_DRAW)]
        pwr = pb.power(cands)
        rows.append({"rho": rho, "mean_delta_obs": round(float(np.mean(deltas)), 4),
                     "power": round(pwr, 4)})
        print(f"  ρ={rho:5.2f}  meanΔ={rows[-1]['mean_delta_obs']:+.4f}  power={pwr:.3f}")

    def mde_at(target: float):
        for a, b in zip(rows, rows[1:]):
            if a["power"] < target <= b["power"]:
                w = (target - a["power"]) / max(b["power"] - a["power"], 1e-9)
                return round(a["mean_delta_obs"]
                             + w * (b["mean_delta_obs"] - a["mean_delta_obs"]), 4)
        return None
    mde80, mde50 = mde_at(0.80), mde_at(0.50)

    print(f"== [5] I 类错误实测：日块内置换 fwd5 标签重训 × {N_PERM} ==")
    by_date: dict[str, list] = {}
    for s in samples:
        if s.get(f"fwd{HORIZON}") is not None:
            by_date.setdefault(s["date"], []).append(s)
    keys_by_date = {d: [(s["fund"], d) for s in ss] for d, ss in by_date.items()}
    fold_trains = [with_label(fd["train"]) for fd in folds]
    fold_tests = [with_label(fd["test"]) for fd in folds]
    fold_mats = [(matrix_existing(tr), matrix_existing(te))
                 for tr, te in zip(fold_trains, fold_tests)]
    rngp = np.random.default_rng(SEED)
    false_pos, perm_powers = 0, []
    for k in range(N_PERM):
        perm_val = {}
        for d, ss in by_date.items():
            vals = [s.get(f"fwd{HORIZON}") for s in ss]
            for key, j in zip(keys_by_date[d], rngp.permutation(len(vals))):
                perm_val[key] = vals[j]
        preds_p = []
        for (Xtr, Xte), tr in zip(fold_mats, fold_trains):
            ytr = np.array([float(perm_val[(s["fund"], s["date"])]) for s in tr])
            preds_p.extend(list(lgb_fit_predict(Xtr, ytr, Xte, seed=SEED + k)))
        x = np.asarray(preds_p, dtype=float)
        pwr_pair = pb.power(x[None, :])
        perm_powers.append(pwr_pair)
        if pwr_pair > 0.975:
            false_pos += 1
        if (k + 1) % 10 == 0:
            print(f"  perm {k + 1}/{N_PERM}  轮级误报={false_pos}  "
                  f"近期单轮判出={pwr_pair:.3f}")
    alpha_hat = false_pos / N_PERM
    med_pp = float(np.median(perm_powers))
    print(f"  单轮判出率中位数 = {med_pp:.3f}（H0 下理论 ≈0.05）；"
          f"轮级误报率 = {alpha_hat:.2f}（>0.10 → 尺子失真、MDE 作废）")

    ruler_ok = alpha_hat <= 0.10
    verdict = ("门槛可达" if mde80 is not None and mde80 <= 0.06 else
               "门槛不可达" if mde80 is None or mde80 >= 0.10 else "边界（提请 Summer）")
    print("== 结果 ==")
    print(f"  MDE(80%)={mde80}  MDE(50%)={mde50}  尺子可信={ruler_ok}  → {verdict}")

    out = {"kind": "forecast_lab_m0_power",
           "prereg": "paired pct2.5>0, per-boot rerank (scipy-equivalent), "
                     "orthogonal-noise candidates",
           "created_at": t0.isoformat(timespec="seconds"),
           "samples_sha256": sha,
           "kfp_now": kfp_now.get("aggregate_sha256"),
           "kfp_0910": kfp_0910.get("aggregate_sha256"), "kfp_same": same_fp,
           "nav_cache_freshness": nav_rep,
           "oos_start": oos_start, "n_folds": len(folds), "n_boot": N_BOOT,
           "base": {"rank_ic": base_ic, "n": base["n"], "per_fold": base["per_fold"]},
           "power_rows": rows, "mix_rows_archive": mix_rows,
           "mde80_delta_ic": mde80, "mde50_delta_ic": mde50,
           "n_perm": N_PERM, "perm_round_level_fp": alpha_hat,
           "perm_power_median": round(med_pp, 4),
           "ruler_ok": ruler_ok, "verdict": verdict,
           "elapsed_sec": round((datetime.now() - t0).total_seconds(), 1)}
    (BASE / "forecast_outputs" / f"mde_sim_{stamp}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  JSON → forecast_outputs/mde_sim_{stamp}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
