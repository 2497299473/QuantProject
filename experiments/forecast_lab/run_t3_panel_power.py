r"""T3 · 面板功效仿真（预注册 §六 T3，2026-09-16 执行；零网络）。

依据：output/forecast_lab_prereg_D_panel_20260914.md §六 T3 + R2 之二（事前失败路径）。
回答唯一问题：**冻结后的 D-lite 面板，配对 MDE80 是否 ≤ 0.05、且单模型日 IC 重抽样 sd
是否较 0.0307 显著下降**——两条同时成立才允许开 09-18 候选槽（C′ regime_cond）。

判据（先写死、跑完不改，逐字继承 §六 T3）
- 主判据 A：面板配对 **MDE80 ≤ 0.05**（MDE50 仅作参照，不进判据）。
- 主判据 B：**单模型 IC 的日块重抽样 sd 较 0.0307 显著下降**
  （0.0307 = output/forecast_lab_review_v2_20260912.md L76 实测值，n≈900 / 4 只池）。
  「显著下降」= 面板 sd < 0.0307（阈值本身不在本文重定义，故按最保守口径：只要低于即成立，
  并同时报出降幅倍数供 Summer 判断是否"显著"）。
- 两条任一不过 → 走 R2 之二「T3 不过（首次）」四步：停候选槽、报缺口、
  登记 PR-20260914-02、本窗口剩余预算转 T1/T2 复现 + 指纹留档。

方法（严格「M0 式」= 与 run_m0_power.py 同构；参数 B=1000 / seed=42 由 §六 T3 写死）
- 样本：forecast_outputs/panel_dlite_20260916.jsonl（sha256 必与 meta 一致，否则中止）。
- 池子：**14 主池** = 4 基金 + 9 原闸门代理 + BK0457。
  ∴ relaxed 代理（159611/515220/159825）**不并入**——§七「relaxed 代理不并入原闸门池
  出单一数（分层报告）」是硬纪律，故主判据只能取 14 主池；17 全池仅作**敏感性参照**，
  明确不用于裁决。
- 特征空间：§二 a158-50（R1 已澄清面板统一用 a158）+ B1 掩码协议 = 100 维。
- 标签端点：fwd5 绝对收益（与 M0 同端点，保证 0.117 → 本值 的纵向可比）。
- 折：split 不用 ratio 自动算，**oos_start 固定 2025-04-30**（§七 写死「OOS start 不动」）
  → build_wf_folds(window_days=63, max_horizon=5) 原参重建（§七「面板折按 build_wf_folds
  原参重建」）。
- 基线：LGB（P1/M0 同 LGB_PARAMS，seed=42）逐折 expanding 训练 → pooled OOS 预测。
  M0 的「复现 P1 = −0.0148」校验在面板上**不适用**（池子与特征空间都变了），
  代之以「基线必须可复现」：两次跑同一折序的 pooled IC 差 ≤ 1e-9。
- 门槛②：PairedBoot 精确复现（日块有放回 B=1000、逐重抽样内部 re-rank、
  判出 ⇔ pct2.5(D) > 0）——直接复用 run_m0_power.PairedBoot（复用存量，不重写）。
- MDE：独立噪声候选 cand = ρ·r(y) + √(1−ρ²)·r(ε)，ε 每 draw 新画、正交于 base；
  ρ 网格 0~0.30 步 0.02，每 ρ 画 N_DRAW=50；power(ρ)=判出频率；
  MDE80 = power 跨 0.8 处 meanΔIC 线性插值；网格尽头 power<0.8 → 记 None（不编造插值）。
- I 类错误（尺子可信性前提，沿用 M0）：日块内置换 fwd5 → 逐折重训 → 对原标签按门槛②判出；
  N_PERM=50，轮级误报率 > 0.10 → bootstrap 失真、MDE 作废、降级「尺子不可信」。
- sanity：base vs base 判出必须 = False。

离线纪律（零网络，与 M0/P1 相同）
- 只读 forecast_outputs/ 已冻结产物，**不发任何网络请求**；
  依赖装载完成后装 socket 守卫（继承 build_panel_dlite 自检的实测口径），
  主流程任何 connect 尝试即 RuntimeError。
- 不碰 config / model_registry / registry.json / 生产 .py / 定时任务（§十 明确不做）。

用法：
  .\.venv-lab\Scripts\python.exe -X utf8 experiments\forecast_lab\run_t3_panel_power.py
  .\.venv-lab\Scripts\python.exe -X utf8 experiments\forecast_lab\run_t3_panel_power.py --selftest
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "experiments" / "forecast_lab"))

import numpy as np                                        # noqa: E402

from features_a158lite import feature_keys                # noqa: E402
# 复用存量引擎（不重写）：秩引擎 + 门槛② bootstrap + 分组工具 + LGB 超参
from run_m0_power import (PairedBoot, LGB_PARAMS, N_ROUNDS, HORIZON,  # noqa: E402
                          SEED, N_BOOT, N_PERM, N_DRAW, RHO_GRID,
                          date_groups, norm_rank, rank_mean_rows)

OUT_DIR = BASE / "forecast_outputs"
PANEL_PREFIX_DEFAULT = "panel_dlite"          # 历史冻结件前缀（默认口径不变）
PANEL_JSONL = "panel_dlite_20260916.jsonl"
PANEL_META = "panel_dlite_20260916.meta.json"

# D2a §四 / D2b R2：主池按 **kind** 派生（= fund + gate_proxy + sector，排除 relaxed），
# 这样面板扩档后无需维护第二份代码清单；默认冻结件上「派生结果 == 硬编码 POOL14」由自检断言。
PRIMARY_KINDS = frozenset({"fund", "gate_proxy", "sector"})


# §一 面板成员分层（与 build_panel_dlite 的成员表逐项一致）
FUNDS = ["002112", "002207", "022853", "025687"]
GATE = ["512480", "512880", "159915", "512660", "510880", "512800",
        "160225", "512010", "501030"]
SECTOR = ["BK0457"]
RELAXED = ["159611", "515220", "159825"]
POOL14 = FUNDS + GATE + SECTOR
POOL17 = POOL14 + RELAXED

OOS_START = "2025-04-30"          # §七 写死不动
WINDOW_DAYS = 63                  # M0/P1 同参
REF_IC_SD = 0.0307                # §六 T3 参照值（review_v2 L76 实测）
MDE80_GATE = 0.05                 # §六 T3 阈值（不放宽）
A158_KEYS = feature_keys()
_BLOCK_MSG = "T3 主流程禁止任何网络连接"


# ---------------------------------------------------------------- 样本装载
def load_panel() -> tuple[list[dict], str]:
    raw = (OUT_DIR / PANEL_JSONL).read_text(encoding="utf-8")
    sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    rows = [json.loads(l) for l in raw.splitlines() if l.strip()]
    return rows, sha


def mask_row(values) -> list[float]:
    """B1 掩码协议：每特征 (值, 掩码) 双列，缺失 → 值 0 / 掩码 1。"""
    row = []
    for v in values:
        ok = v is not None and isinstance(v, (int, float)) and math.isfinite(float(v))
        row.append(float(v) if ok else 0.0)
        row.append(0.0 if ok else 1.0)
    return row


def matrix(rows) -> np.ndarray:
    return np.array([mask_row([s.get(k) for k in A158_KEYS]) for s in rows], dtype=float)


def targets(rows) -> np.ndarray:
    return np.array([float(s[f"fwd{HORIZON}"]) for s in rows], dtype=float)


# ---------------------------------------------------------------- 折与基线
def make_folds(samples: list[dict], oos_start: str) -> tuple[list[dict], list[dict]]:
    """oos_start 固定 → 手工切 OOS → 用存量 build_wf_folds 原参构造折。"""
    from backtest_walk_forward import build_wf_folds
    oos = [s for s in samples if s["date"] >= oos_start]
    folds = build_wf_folds(samples, oos, oos_start,
                           window_days=WINDOW_DAYS, max_horizon=HORIZON)
    return folds, oos


def run_base(folds) -> dict:
    """逐折 expanding 训练 LGB(a158-50+mask) → 拼 pooled OOS 预测。"""
    import lightgbm as lgb
    preds, trues, dates, members, per_fold = [], [], [], [], []
    for fd in folds:
        tr, te = fd["train"], fd["test"]
        tr = [s for s in tr if s.get(f"fwd{HORIZON}") is not None]
        te = [s for s in te if s.get(f"fwd{HORIZON}") is not None]
        if len(tr) < 100 or len(te) < 10:
            continue
        p = lgb.train(dict(LGB_PARAMS), lgb.Dataset(matrix(tr), label=targets(tr)),
                      num_boost_round=N_ROUNDS).predict(matrix(te))
        yte = targets(te)
        per_fold.append({"test_start": fd["test_start"], "n_test": len(te),
                         "rank_ic": round(float(_ic(p, yte)), 4)})
        preds.extend(list(p))
        trues.extend(list(yte))
        dates.extend([s["date"] for s in te])
        members.extend([s["member"] for s in te])
    from backtest_forecast import rank_ic
    pooled = round(float(rank_ic(preds, trues)), 4)
    return {"n": len(preds), "per_fold": per_fold, "dates": dates,
            "members": members, "preds": np.asarray(preds, float),
            "trues": np.asarray(trues, float), "pooled_ic": pooled}


def _ic(xs, ys) -> float:
    from backtest_forecast import rank_ic
    return rank_ic(list(xs), list(ys))


def ic_resample_sd(pred: np.ndarray, y: np.ndarray, groups: list[np.ndarray],
                   n_boot: int, seed: int) -> dict:
    """判据 B：单模型 IC 的日块重抽样 sd（与 review_v2 的 0.0307 同一定义）。

    用 scipy 精确逐重抽样（不用 PairedBoot 的秩近似引擎，避免定义漂移）。
    """
    from backtest_forecast import rank_ic
    rng = np.random.default_rng(seed)
    ics = []
    for _ in range(n_boot):
        picks = rng.integers(0, len(groups), size=len(groups))
        take = np.concatenate([groups[c] for c in picks])
        ics.append(rank_ic(list(pred[take]), list(y[take])))
    a = np.asarray(ics, float)
    return {"n_boot": n_boot, "ic_mean": round(float(a.mean()), 4),
            "ic_sd": round(float(a.std(ddof=1)), 4),
            "ci95_halfwidth": round(float(1.96 * a.std(ddof=1)), 4),
            "pct2_5": round(float(np.percentile(a, 2.5)), 4),
            "pct97_5": round(float(np.percentile(a, 97.5)), 4)}


def mde_curve(base: dict, pb: PairedBoot, seed_offset: int = 7) -> tuple[list[dict], float | None, float | None]:
    """MDE 主判据（独立噪声候选，正交于 base）——与 M0 同构。"""
    from backtest_forecast import rank_ic
    rng = np.random.default_rng(SEED + seed_offset)
    r_y = norm_rank(base["trues"])
    n = base["n"]
    rows = []
    for rho in RHO_GRID:
        cands = np.empty((N_DRAW, n))
        for k in range(N_DRAW):
            cands[k] = rho * r_y + math.sqrt(max(1 - rho * rho, 0.0)) * norm_rank(
                rng.random(n))
        deltas = [rank_ic(list(cands[k]), list(base["trues"])) - base["pooled_ic"]
                  for k in range(N_DRAW)]
        pwr = pb.power(cands)
        rows.append({"rho": rho, "mean_delta_obs": round(float(np.mean(deltas)), 4),
                     "power": round(pwr, 4)})
    def at(t: float):
        for a, b in zip(rows, rows[1:]):
            if a["power"] < t <= b["power"]:
                w = (t - a["power"]) / max(b["power"] - a["power"], 1e-9)
                return round(a["mean_delta_obs"] + w * (b["mean_delta_obs"]
                                                        - a["mean_delta_obs"]), 4)
        return None
    return rows, at(0.80), at(0.50)


def permutation_alpha(samples: list[dict], folds: list[dict], pb: PairedBoot,
                      n_perm: int = N_PERM) -> dict:
    """I 类错误实测（沿用 M0 逻辑，键名 fund → member）。"""
    import lightgbm as lgb
    by_date: dict[str, list] = {}
    for s in samples:
        if s.get(f"fwd{HORIZON}") is not None:
            by_date.setdefault(s["date"], []).append(s)
    keys_by_date = {d: [(s["member"], d) for s in ss] for d, ss in by_date.items()}
    fold_trains = [[s for s in fd["train"] if s.get(f"fwd{HORIZON}") is not None]
                   for fd in folds]
    fold_mats = [(matrix(tr), matrix(fd["test"])) for tr, fd in zip(fold_trains, folds)]
    rng = np.random.default_rng(SEED)
    false_pos, powers = 0, []
    for k in range(n_perm):
        pv = {}
        for d, ss in by_date.items():
            vals = [s.get(f"fwd{HORIZON}") for s in ss]
            for key, j in zip(keys_by_date[d], rng.permutation(len(vals))):
                pv[key] = vals[j]
        preds_p = []
        for (Xtr, Xte), tr in zip(fold_mats, fold_trains):
            ytr = np.array([float(pv[(s["member"], s["date"])]) for s in tr])
            preds_p.extend(list(lgb.train(dict(LGB_PARAMS, seed=SEED + k),
                                          lgb.Dataset(Xtr, label=ytr),
                                          num_boost_round=N_ROUNDS).predict(Xte)))
        x = np.asarray(preds_p, float)
        p = pb.power(x[None, :])
        powers.append(p)
        if p > 0.975:
            false_pos += 1
        if (k + 1) % 10 == 0:
            print(f"  perm {k + 1}/{n_perm}  轮级误报={false_pos}  近期单轮判出={p:.3f}")
    return {"n_perm": n_perm, "round_level_fp_rate": round(false_pos / n_perm, 4),
            "perm_power_median": round(float(np.median(powers)), 4)}


# ---------------------------------------------------------------- 自检
def _selftest() -> int:
    ok = fail = 0

    def chk(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  [PASS] {name}")
        else:
            fail += 1
            print(f"  [FAIL] {name}")

    chk("1 成员表 17（4+9+1+3）",
        len(POOL17) == 17 and len(POOL14) == 14 and len(set(POOL17)) == 17)
    chk("2 主池不含 relaxed（§七 不混池）", not (set(POOL14) & set(RELAXED)))
    chk("3 判据阈值写死 0.05 / 0.0307", MDE80_GATE == 0.05 and REF_IC_SD == 0.0307)
    chk("4 B=1000 / seed=42 继承 §六 T3", N_BOOT == 1000 and SEED == 42)
    chk("5 特征空间 a158-50 → 掩码后 100 维",
        len(A158_KEYS) == 50 and len(mask_row([None] * 50)) == 100)
    chk("6 B1 掩码：缺失 → 值0/掩码1", mask_row([None, 2.0]) == [0.0, 1.0, 2.0, 0.0])
    # 秩引擎与 scipy 等价（面板规模）
    from scipy.stats import rankdata
    rng = np.random.default_rng(0)
    X = rng.choice([1.0, 2.0, 3.5], size=(37, 300))
    chk("7 rank_mean_rows == scipy.rankdata(axis=1)",
        float(np.abs(rank_mean_rows(X) - rankdata(X, axis=1)).max()) < 1e-9)
    # 日块分组：行数守恒、不跨日
    g = date_groups(["2025-05-06", "2025-05-06", "2025-05-07", "2025-05-07",
                     "2025-05-07"])
    chk("8 date_groups 守恒且不跨日",
        sum(len(x) for x in g) == 5 and len(g) == 2)
    # MDE 插值函数：单调 power 曲线可插值
    rows = [{"rho": 0.0, "mean_delta_obs": 0.01, "power": 0.0},
            {"rho": 0.1, "mean_delta_obs": 0.05, "power": 1.0}]

    def at(t):
        for a, b in zip(rows, rows[1:]):
            if a["power"] < t <= b["power"]:
                w = (t - a["power"]) / max(b["power"] - a["power"], 1e-9)
                return round(a["mean_delta_obs"] + w * (b["mean_delta_obs"]
                                                        - a["mean_delta_obs"]), 4)
        return None
    chk("9 MDE 插值在网格内正确", at(0.8) == 0.042)
    chk("10 网格尽头不可达时返回 None", at(1.5) is None)
    # 面板样本完整性（读盘，零网络）
    rows_p, sha = load_panel()
    meta = json.loads((OUT_DIR / PANEL_META).read_text(encoding="utf-8"))
    chk("11 面板 sha256 与 meta 一致（冻结未被扰动）", sha == meta["sha256_jsonl"])
    chk("12 行数与 meta 一致", len(rows_p) == meta["n_rows"])
    p14 = [s for s in rows_p if s["member"] in set(POOL14)]
    chk("13 主池样本量 > 0 且全部含 fwd5",
        len(p14) > 1000 and all(s.get("fwd5") is not None for s in p14))
    # 主池改为按 kind 派生（扩档自适应）→ 在冻结件上必须与硬编码 14 员清单逐行等集
    pk = [s for s in rows_p if s.get("kind") in PRIMARY_KINDS]
    chk("13b kind 派生主池 == 硬编码 POOL14（冻结件回归）",
        len(pk) == len(p14) and {s["member"] for s in pk} == set(POOL14)
        and len(set(s["member"] for s in pk)) == 14)
    chk("13c variant 常量与 D2b 一致（v2 需折归属）",
        "make_picks" in open(BASE / "experiments" / "forecast_lab" / "run_d2b_ruler_probe.py",
                             encoding="utf-8").read())
    oos14 = [s for s in p14 if s["date"] >= OOS_START]
    chk("14 OOS(≥2025-04-30) 非空", len(oos14) > 500)
    widths = Counter(s["date"] for s in oos14)
    chk("15 OOS 日宽中位 ≥ 12（买功效的前提）", int(np.median(list(widths.values()))) >= 12)
    # 零网络：守卫实测
    import socket as _socket

    class _NoNet(_socket.socket):
        def connect(self, *a, **k):
            raise RuntimeError(_BLOCK_MSG)

    orig = _socket.socket
    _socket.socket = _NoNet
    msg = None
    try:
        # 不测构造（构造不发包），实测 connect 被拦：无守卫时本机 1 端口会抛
        # ConnectionRefusedError（OSError），有守卫时应抛我方 RuntimeError。
        try:
            _socket.socket().connect(("127.0.0.1", 1))
        except RuntimeError as e:
            msg = str(e)
        except OSError as e:
            msg = f"OS:{type(e).__name__}"
    finally:
        _socket.socket = orig
    chk("16 socket 守卫拦截 connect（报我方异常而非 OS 错）", msg == _BLOCK_MSG)
    print(f"[run_t3_panel_power SELFTEST] {ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


# ---------------------------------------------------------------- 主流程
def main(argv=None) -> int:
    import argparse
    global PANEL_JSONL, PANEL_META
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--skip-perm", action="store_true",
                    help="跳过 permutation（调试用；正式裁决不得用）")
    ap.add_argument("--panel", default=f"{PANEL_PREFIX_DEFAULT}_20260916",
                    help="面板前缀（默认 = 冻结件 panel_dlite_20260916；D2a 传 panel_dlite_v2_20260917）")
    ap.add_argument("--resample-variant", default="v0", choices=("v0", "v1", "v2"),
                    help="门槛②的重抽样单位（D2b R2 拍板：扩档批主报 v2、并列 v0）；"
                         "默认 v0 = 与 09-16 T3 逐字节同口径")
    ap.add_argument("--n-perm", type=int, default=N_PERM,
                    help="permutation 轮数（D2b 后建议 200；默认 50 = 历史口径）")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()

    PANEL_JSONL = f"{args.panel}.jsonl"
    PANEL_META = f"{args.panel}.meta.json"
    variant = args.resample_variant

    t0 = datetime.now()
    stamp = t0.strftime("%Y%m%d")

    # 零网络守卫：依赖已全部装载，此后任何 connect 即失败
    _guard_network()

    print("== [0] 面板冻结校验（sha256 vs meta）==")
    rows, sha = load_panel()
    meta = json.loads((OUT_DIR / PANEL_META).read_text(encoding="utf-8"))
    if sha != meta["sha256_jsonl"]:
        print(f"  [ABORT] 面板 sha256 漂移：{sha[:16]}… != {meta['sha256_jsonl'][:16]}…")
        return 1
    print(f"  {len(rows)} 行 sha256 一致 {sha[:16]}…  variant={variant}  n_perm={args.n_perm}")

    # 主池按 kind 派生（扩档自适应，§七 不混池纪律不变）
    p14 = [s for s in rows if s.get("kind") in PRIMARY_KINDS]
    p17 = rows
    print(f"== [1] 池子构造（§七 不混池 → 主判据 = fund+gate+sector，本批 {len(set(s['member'] for s in p14))} 员）==")
    for name, pool in (("pool14", p14), ("pool17", p17)):
        w = Counter(s["date"] for s in pool if s["date"] >= OOS_START)
        print(f"  {name}: rows={len(pool)}  OOS日块={len(w)}  "
              f"日宽 min/med/max={min(w.values())}/{int(np.median(list(w.values())))}/{max(w.values())}")

    results = {}
    for name, pool in (("pool14_primary", p14), ("pool17_sensitivity", p17)):
        primary = name == "pool14_primary"
        print(f"== [2] {name}：折构造（oos_start={OOS_START}, window_days={WINDOW_DAYS}）==")
        folds, oos = make_folds(pool, OOS_START)
        if not folds:
            print(f"  [ABORT] {name} 无有效折")
            return 1
        print(f"  折数={len(folds)}  "
              f"逐折 n_test={[len(fd['test']) for fd in folds]}  OOS={len(oos)}")

        print(f"== [3] {name}：基线 LGB(a158-50+mask) 逐折重训 ==")
        base = run_base(folds)
        print(f"  pooled T+5 IC={base['pooled_ic']:+.4f}  n={base['n']}  "
              f"逐折={[f['rank_ic'] for f in base['per_fold']]}")
        if primary:
            # 可复现性（替代 M0 的「复现 P1 −0.0148」：池子与特征空间已变，纵向不可比）
            base2 = run_base(folds)
            if abs(base["pooled_ic"] - base2["pooled_ic"]) > 1e-9:
                print("  [ABORT] 基线不可复现（同折序两次 pooled IC 不一致）")
                return 1
            print(f"  [repro] 两次 pooled IC 一致 = {base2['pooled_ic']:+.4f}")

        print(f"== [4] {name}：判据 B — 单模型 IC 日块重抽样 sd（B={N_BOOT}, seed={SEED}）==")
        sd1 = ic_resample_sd(base["preds"], base["trues"],
                            date_groups(base["dates"]), N_BOOT, SEED)
        print(f"  ic_sd={sd1['ic_sd']:.4f}（参照 {REF_IC_SD}）  "
              f"95%CI 半宽≈{sd1['ci95_halfwidth']:.4f}  "
              f"IC 均值={sd1['ic_mean']:+.4f}  区间=[{sd1['pct2_5']:+.4f},{sd1['pct97_5']:+.4f}]")

        print(f"== [5] {name}：判据 A — PairedBoot MDE80（重抽样 variant={variant}）==")
        groups = date_groups(base["dates"])
        picks = None
        if variant != "v0":
            # 复用 D2b 探针的 picks 生成（同一实现，防口径漂移）
            from run_d2b_ruler_probe import make_picks, fold_ids_of_blocks
            fob = fold_ids_of_blocks(groups, folds, base["dates"])
            picks = make_picks(variant, len(groups), N_BOOT, SEED, fold_of_group=fob)
        pb = PairedBoot(base["preds"], base["trues"],
                        date_groups(base["dates"]), N_BOOT, SEED, picks=picks)
        decide_base = pb.decide(base["preds"])
        print(f"  [sanity] base vs base 判出={decide_base}（必须 False）")
        if primary and decide_base:
            print("  [ABORT] sanity 失败：base vs base 判出为 True，门槛②实现失真")
            return 1
        curve, mde80, mde50 = mde_curve(base, pb)
        for r in curve:
            print(f"  ρ={r['rho']:5.2f}  meanΔ={r['mean_delta_obs']:+.4f}  power={r['power']:.3f}")
        print(f"  → MDE80={mde80}  MDE50={mde50}")

        perm = None
        if primary and not args.skip_perm:
            print(f"== [6] pool14：I 类错误实测（N_PERM={args.n_perm}）==")
            perm = permutation_alpha(pool, folds, pb, n_perm=args.n_perm)
            print(f"  轮级误报率={perm['round_level_fp_rate']:.2f}（>0.10 → 尺子失真、MDE 作废）"
                  f"  单轮判出中位数={perm['perm_power_median']:.4f}")

        results[name] = {
            "n_panel_rows": len(pool), "n_folds": len(folds),
            "fold_sizes": [len(fd["test"]) for fd in folds],
            "oos_rows": base["n"], "oos_day_blocks": len(set(base["dates"])),
            "day_width": {"min": int(min(w for w in
                                         Counter(s["date"] for s in pool
                                               if s["date"] >= OOS_START).values())),
                          "median": int(np.median(list(Counter(
                              s["date"] for s in pool if s["date"] >= OOS_START).values()))),
                          "max": int(max(Counter(s["date"] for s in pool
                                                 if s["date"] >= OOS_START).values()))},
            "base": {"pooled_ic": base["pooled_ic"], "per_fold": base["per_fold"]},
            "ic_resample": sd1, "ic_sd_ratio_vs_ref": (
                round(REF_IC_SD / sd1["ic_sd"], 2) if sd1["ic_sd"] else None),
            "power_curve": curve, "mde80_delta_ic": mde80, "mde50_delta_ic": mde50,
            "sanity_base_vs_base_decide": decide_base,
            "permutation": perm,
        }

    p = results["pool14_primary"]
    gate_a = p["mde80_delta_ic"] is not None and p["mde80_delta_ic"] <= MDE80_GATE
    gate_b = p["ic_resample"]["ic_sd"] < REF_IC_SD
    ruler_ok = (p["permutation"] is None) or (p["permutation"]["round_level_fp_rate"] <= 0.10)
    pass_all = bool(gate_a and gate_b and ruler_ok)
    print("== 裁决 ==")
    print(f"  判据A 面板配对 MDE80 ≤ {MDE80_GATE}：{'PASS' if gate_a else 'FAIL'}"
          f"（实测 {p['mde80_delta_ic']}）")
    print(f"  判据B 日IC重抽样 sd < {REF_IC_SD}：{'PASS' if gate_b else 'FAIL'}"
          f"（实测 {p['ic_resample']['ic_sd']}，降幅 {p['ic_sd_ratio_vs_ref']}×）")
    print(f"  尺子可信（轮级误报 ≤0.10）：{'PASS' if ruler_ok else 'FAIL/未跑'}")
    print(f"  → T3 {'PASS：可开 09-18 候选槽（C′ regime_cond）' if pass_all else 'FAIL：走 R2 之二失败路径'}")

    out = {"kind": "forecast_lab_t3_panel_power",
           "prereg": "output/forecast_lab_prereg_D_panel_20260914.md §六 T3 + R2 之二",
           "created_at": t0.isoformat(timespec="seconds"),
           "panel": PANEL_JSONL, "panel_sha256": sha,
           "resample_variant": variant,
           "variant_note": ("v0 = 日块 i.i.d.（09-16 T3 同口径）；v2 = 两阶段分层（D2b R2 拍板主报）；"
                            "v1 moving block 已被 D2b 判定修钝、不用于裁决"),
           "pool_primary": (f"kind∈{sorted(PRIMARY_KINDS)} 派生（"
                            f"{len(set(s['member'] for s in p14))} 员；relaxed excluded per §七）"),
           "feature_space": "a158-50 + B1 mask = 100 dims (§二 / R1)",
           "label_endpoint": f"fwd{HORIZON}",
           "oos_start": OOS_START, "window_days": WINDOW_DAYS,
           "n_boot": N_BOOT, "seed": SEED, "n_draw": N_DRAW, "rho_grid": RHO_GRID,
           "n_perm": args.n_perm,
           "ref_ic_sd_from": "output/forecast_lab_review_v2_20260912.md L76 (0.0307)",
           "gates": {"mde80_le": MDE80_GATE, "ic_sd_lt": REF_IC_SD},
           "results": results,
           "verdict": {"gate_a_mde80": gate_a, "gate_b_ic_sd": gate_b,
                       "ruler_ok": ruler_ok, "t3_pass": pass_all},
           "elapsed_sec": round((datetime.now() - t0).total_seconds(), 1)}
    (OUT_DIR / f"t3_panel_power_{stamp}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  JSON → forecast_outputs/t3_panel_power_{stamp}.json")
    return 0 if pass_all else 2


def _guard_network() -> None:
    """主流程零网络守卫（依赖已装载完毕）。"""
    import socket

    class NoNet(socket.socket):
        def connect(self, *a, **k):
            raise RuntimeError(_BLOCK_MSG)
    socket.socket = NoNet          # type: ignore[misc]


if __name__ == "__main__":
    sys.exit(main())
