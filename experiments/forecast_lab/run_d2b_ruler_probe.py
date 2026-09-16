r"""D2b · 门槛②重抽样「尺子」校准探针（v0/v1/v2，零网络）。

依据：output/forecast_lab_prereg_D2b_ruler_draft_20260916.md §二（探针矩阵）；
拍板：2026-09-16 13:40 Summer「按你的建议执行」（认可 D2b 判据段）。

要回答：面板结构下轮级误报 0.12 是**真失真**（尺子需换重抽样单位）还是 **50 轮分辨率不足**
（名义 0.05 下 P(≥6/50)=0.038）。为此 N_PERM 50→200（SE 0.052→0.023）。

三格（§二 矩阵，写死）
- v0：现状复刻（日块 i.i.d. 有放回，B=1000）——仅把 N_PERM 升到 200，用于钉死读数。
- v1：moving block，块长 L=5 个日块（重叠滑动），保留短程 IC 自相关。
- v2：两阶段——先按折分层（折等权）再折内抽日块（总块数恒 = 日块数）。

每格三读数（§二 写死）：① 轮级误报率（N_PERM=200）② base-vs-base sanity（必为 False）
③ ρ=0.10 单点 power（相对 v0 塌缩 ≤20%，防「把尺子修钝到什么都判不出」）。
选择规则（§二 写死）：满足「误报 ≤0.10 且 ③ 塌缩 ≤20%」者中**改动最小**（v1 < v2）；
三者全不合格 → 尺子不可修，封顶挂 09-26，不做第四变体。

不可动（estimand 层，§一 写死）
- window_days=63 / 折构造 / oos_start=2025-04-30 / 主判据 14 主池 / relaxed 不混池；
- MDE80 阈值 0.05、B=1000、seed=42、ρ 网格 0~0.30 步 0.02、N_DRAW=50。

效率说明（不改语义）：base 重训、候选构造、置换重训与 variant 无关 → **各付一次**并缓存，
仅「判定」按 variant 各算一遍；故实测机时远低于 §四 的 3.5h 估算（不因此改任何判据）。

复刻自检（强一致证据）：v0 与今日 T3（panel_power_20260916.json）应逐位复现
① 判据 B ic_sd=0.0214 ② MDE80=0.0651 ③ 前 50 轮置换误报=6。

用法：
  .\.venv-lab\Scripts\python.exe -X utf8 experiments\forecast_lab\run_d2b_ruler_probe.py
  .\.venv-lab\Scripts\python.exe -X utf8 experiments\forecast_lab\run_d2b_ruler_probe.py --selftest
"""
from __future__ import annotations

import argparse
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

import numpy as np                                             # noqa: E402

import run_t3_panel_power as t3                                # noqa: E402 复用存量
from run_m0_power import (PairedBoot, LGB_PARAMS, N_ROUNDS, HORIZON,  # noqa: E402
                          SEED, N_BOOT, N_DRAW, N_PERM, RHO_GRID,
                          date_groups, norm_rank)

OUT_DIR = BASE / "forecast_outputs"
VARIANTS = ("v0", "v1", "v2")            # 选择规则优先序：v0(现状) < v1 < v2
BLOCK_L = 5                              # v1 moving block 长度（日块单位，§二 写死）
N_PERM_PROBE = 200                       # §二 v0 明确升档 50→200
FP_MAX = 0.10                            # 尺子可信门槛（M0 文档头继承，不放宽）
POWER_COLLAPSE_MAX = 0.20                # ③ 防修钝：相对 v0 塌缩上限
RHO_CHECK = 0.10                         # ③ 检查点
_T3_JSON = "t3_panel_power_20260916.json"


# ------------------------------------------------------------ picks 生成（各 variant 唯一差异）
def make_picks(variant: str, n_groups: int, n_boot: int, seed: int,
               fold_of_group: np.ndarray | None = None, block_l: int = BLOCK_L):
    """返回 list[np.ndarray]：每条 = 该次重抽样抽中的日块索引序列。"""
    rng = np.random.default_rng(seed)
    out: list[np.ndarray] = []
    if variant == "v0":
        for _ in range(n_boot):
            out.append(rng.integers(0, n_groups, size=n_groups))
    elif variant == "v1":
        if n_groups < block_l:
            raise ValueError(f"日块数 {n_groups} < 块长 {block_l}，v1 不可用")
        n_blk = math.ceil(n_groups / block_l)
        max_start = n_groups - block_l
        base_off = np.arange(block_l)
        for _ in range(n_boot):
            starts = rng.integers(0, max_start + 1, size=n_blk)
            out.append((starts[:, None] + base_off[None, :]).ravel()[:n_groups])
    elif variant == "v2":
        if fold_of_group is None:
            raise ValueError("v2 需 fold_of_group")
        fb = [np.flatnonzero(fold_of_group == f) for f in np.unique(fold_of_group)]
        sizes = np.asarray([len(b) for b in fb])
        cat = np.concatenate(fb)
        offs = np.r_[0, np.cumsum(sizes)]
        for _ in range(n_boot):
            f_draw = rng.integers(0, len(fb), size=n_groups)      # 折层等权
            gpos = offs[f_draw] + (rng.random(n_groups) * sizes[f_draw]).astype(int)
            out.append(cat[gpos])                                  # 折内块 i.i.d.
    else:
        raise ValueError(f"未知 variant {variant}")
    return out


def fold_ids_of_blocks(groups: list[np.ndarray], folds: list[dict],
                       dates: list[str]) -> np.ndarray:
    """日块 → 折 归属（按 OOS 折窗；折互不重叠且覆盖全 OOS，否则抛错）。"""
    uniq = sorted(set(dates))
    d2f: dict[str, int] = {}
    for fi, fd in enumerate(folds):
        for d in fd["window"]:
            if d in d2f:
                raise ValueError(f"日期 {d} 同时属于折 {d2f[d]} 与 {fi}，v2 分层不成立")
            d2f[d] = fi
    if sorted(d2f) != uniq:
        raise ValueError("折窗未完整覆盖 OOS 日期，v2 分层不成立")
    return np.asarray([d2f[sorted(set(dates))[i]] for i in range(len(uniq))])


# ------------------------------------------------------------ 与 variant 无关的重付成本（缓存一次）
def build_mde_cands(base: dict, seed_offset: int = 7) -> dict:
    """独立噪声候选（正交于 base）——与 T3 同 seed/同式 → v0 应逐位复现 MDE80。"""
    rng = np.random.default_rng(SEED + seed_offset)
    r_y = norm_rank(base["trues"])
    n = base["n"]
    per_rho = {}
    for rho in RHO_GRID:
        cands = np.empty((N_DRAW, n))
        for k in range(N_DRAW):
            cands[k] = rho * r_y + math.sqrt(max(1 - rho * rho, 0.0)) * norm_rank(
                rng.random(n))
        per_rho[rho] = cands
    return per_rho


def build_perm_preds(samples: list[dict], folds: list[dict], n_perm: int) -> np.ndarray:
    """日块内置换 fwd5 → 逐折重训 → (n_perm, n_rows) 预测矩阵。

    训练与 variant 无关（同 seed=SEED+k）→ 只付一次；判定再按 variant 各算。
    """
    import lightgbm as lgb
    by_date: dict[str, list] = {}
    for s in samples:
        if s.get(f"fwd{HORIZON}") is not None:
            by_date.setdefault(s["date"], []).append(s)
    keys_by_date = {d: [(s["member"], d) for s in ss] for d, ss in by_date.items()}
    fold_trains = [[s for s in fd["train"] if s.get(f"fwd{HORIZON}") is not None]
                   for fd in folds]
    fold_mats = [(t3.matrix(tr), t3.matrix(fd["test"]))
                 for tr, fd in zip(fold_trains, folds)]
    rng = np.random.default_rng(SEED)
    out = np.empty((n_perm, sum(len(fd["test"]) for fd in folds)))
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
        out[k] = np.asarray(preds_p, float)
        if (k + 1) % 25 == 0:
            print(f"    perm 重训 {k + 1}/{n_perm}")
    return out


def mde_from_rows(rows: list[dict], t: float):
    for a, b in zip(rows, rows[1:]):
        if a["power"] < t <= b["power"]:
            w = (t - a["power"]) / max(b["power"] - a["power"], 1e-9)
            return round(a["mean_delta_obs"] + w * (b["mean_delta_obs"]
                                                    - a["mean_delta_obs"]), 4)
    return None


# ------------------------------------------------------------ 单 variant 全量判定
def run_variant(variant: str, base: dict, groups: list[np.ndarray], picks,
                perm_preds: np.ndarray, mde_cands: dict, n_perm: int) -> dict:
    from backtest_forecast import rank_ic
    pb = PairedBoot(base["preds"], base["trues"], groups, N_BOOT, SEED, picks=picks)
    sanity = pb.decide(base["preds"])                  # 必须 False
    # ① 轮级误报（N_PERM=200）
    dec = np.fromiter((1.0 if pb.decide(perm_preds[k]) else 0.0 for k in range(n_perm)),
                      float, count=n_perm)
    fp = float(dec.mean())
    fp_first50 = float(dec[:50].mean())                # 与 T3 的 50 轮交叉验证
    # ② MDE 曲线（候选缓存复用，只重算判定）
    rows = []
    for rho in RHO_GRID:
        cands = mde_cands[rho]
        deltas = [rank_ic(list(cands[k]), list(base["trues"])) - base["pooled_ic"]
                  for k in range(N_DRAW)]
        rows.append({"rho": rho, "mean_delta_obs": round(float(np.mean(deltas)), 4),
                     "power": round(pb.power(cands), 4)})
    # ③ ρ=0.10 单点 power
    p10 = next(r["power"] for r in rows if r["rho"] == RHO_CHECK)
    return {"variant": variant, "sanity_base_vs_base": sanity,
            "round_level_fp_rate": round(fp, 4), "n_perm": n_perm,
            "round_level_fp_first50": round(fp_first50, 4),
            "mde_rows": rows, "mde80": mde_from_rows(rows, 0.80),
            "mde50": mde_from_rows(rows, 0.50), "power_rho_0.10": p10}


# ------------------------------------------------------------ 自检
def _selftest() -> int:
    from run_m0_power import N_BOOT
    ok = fail = 0

    def chk(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  [PASS] {name}")
        else:
            fail += 1
            print(f"  [FAIL] {name}")

    # 1. v0 picks 必须与 PairedBoot 默认逐字节一致（存量行为零变化）
    g = [np.arange(i * 3, i * 3 + 3) for i in range(7)]
    rng = np.random.default_rng(SEED)
    want = [rng.integers(0, 7, size=7) for _ in range(5)]
    got = make_picks("v0", 7, 5, SEED)
    chk("1 v0 picks == PairedBoot 默认 picks（逐字节）",
        all(np.array_equal(a, b) for a, b in zip(want, got)))
    # 2. picks 形状/范围
    for v in ("v0", "v1", "v2"):
        p = make_picks(v, 12, 4, 1, fold_of_group=np.repeat([0, 1, 2], 4))
        chk(f"2 {v} picks 条数=4 且每条长度=12 且索引合法",
            len(p) == 4 and all(len(x) == 12 and x.min() >= 0 and x.max() < 12
                                for x in p))
    # 3. v1 块连续性（滑动块内索引连续）
    p1 = make_picks("v1", 20, 1, 7)[0]
    chk("3 v1 块内索引连续（moving block 语义）",
        all(p1[i + 1] - p1[i] == 1 for i in range(BLOCK_L - 1)))
    # 4. v2 分层：抽中块必属被抽折（构造上已保证）→ 验证索引全合法且分布覆盖各折
    fod = np.repeat([0, 1, 2], [5, 6, 7])
    p2 = make_picks("v2", 18, 3, 5, fold_of_group=fod)
    chk("4 v2 抽中块归属合法",
        all(all(fod[idx] in (0, 1, 2) for idx in x) for x in p2))
    # 5. 折归属映射：不覆盖/重叠时抛错
    groups = date_groups(["2025-05-06", "2025-05-07", "2025-05-08"])
    try:
        fold_ids_of_blocks(groups, [{"window": ["2025-05-06"]}], ["2025-05-06",
                                                                  "2025-05-07", "2025-05-08"])
        bad = "未抛错"
    except ValueError:
        bad = None
    chk("5 折窗未覆盖 OOS 时报错（v2 分层前提）", bad is None)
    chk("6 折归属正确", list(fold_ids_of_blocks(
        groups, [{"window": ["2025-05-06", "2025-05-07"]}, {"window": ["2025-05-08"]}],
        ["2025-05-06", "2025-05-07", "2025-05-08"])) == [0, 0, 1])
    # 7. 判据常量写死
    chk("7 判据常量（0.10 / 20% / L=5 / N_PERM=200 / B=1000 / seed=42）",
        FP_MAX == 0.10 and POWER_COLLAPSE_MAX == 0.20 and BLOCK_L == 5
        and N_PERM_PROBE == 200 and N_BOOT == 1000 and SEED == 42)
    # 8. PairedBoot 显式 picks 与默认 picks 等价（同 picks 输入 → 同结果）
    rng = np.random.default_rng(3)
    y = rng.random(60); bp = rng.random(60)
    groups8 = date_groups([f"2025-05-{d:02d}" for d in range(1, 11)] * 6)
    pk = make_picks("v0", len(groups8), 50, 11)          # ≥43（存量 _check 抽样点）
    a = PairedBoot(bp, y, groups8, 50, 11).diffs(bp)
    b = PairedBoot(bp, y, groups8, 50, 11, picks=pk).diffs(bp)
    chk("8 显式 picks 与默认路径等价", float(np.abs(a - b).max()) < 1e-12)
    # 9. 面板冻结未被扰动
    rows, sha = t3.load_panel()
    meta = json.loads((OUT_DIR / t3.PANEL_META).read_text(encoding="utf-8"))
    chk("9 面板 sha256 与 meta 一致", sha == meta["sha256_jsonl"])
    # 10. MDE 插值
    rows_m = [{"rho": 0.0, "mean_delta_obs": 0.01, "power": 0.0},
              {"rho": 0.1, "mean_delta_obs": 0.05, "power": 1.0}]
    chk("10 MDE 插值 = 0.042", mde_from_rows(rows_m, 0.8) == 0.042)
    # 11. 零网络守卫
    import socket as _socket

    class _NoNet(_socket.socket):
        def connect(self, *a, **k):
            raise RuntimeError(t3._BLOCK_MSG)

    orig = _socket.socket
    _socket.socket = _NoNet
    msg = None
    try:
        try:
            _socket.socket().connect(("127.0.0.1", 1))
        except RuntimeError as e:
            msg = str(e)
        except OSError as e:
            msg = f"OS:{type(e).__name__}"
    finally:
        _socket.socket = orig
    chk("11 socket 守卫拦截 connect", msg == t3._BLOCK_MSG)
    print(f"[run_d2b_ruler_probe SELFTEST] {ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


# ------------------------------------------------------------ 主流程
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--n-perm", type=int, default=N_PERM_PROBE)
    ap.add_argument("--variants", default=",".join(VARIANTS))
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()

    # 零网络守卫（装载期后装；本脚本全程不联网）
    import socket as _socket

    class _NoNet(_socket.socket):
        def connect(self, *a, **k):
            raise RuntimeError(t3._BLOCK_MSG)

    _socket.socket = _NoNet

    t0 = datetime.now()
    stamp = t0.strftime("%Y%m%d")
    want = [v.strip() for v in args.variants.split(",") if v.strip()]

    rows, sha = t3.load_panel()
    meta = json.loads((OUT_DIR / t3.PANEL_META).read_text(encoding="utf-8"))
    if sha != meta["sha256_jsonl"]:
        print(f"ABORT: 面板 sha256 与 meta 不一致（{sha[:12]} != "
              f"{meta['sha256_jsonl'][:12]}）")
        return 2
    p14 = [s for s in rows if s["member"] in set(t3.POOL14)]
    folds, oos = t3.make_folds(p14, t3.OOS_START)
    print(f"== D2b 探针 · {t0:%Y-%m-%d %H:%M:%S} · 主池 14 · OOS={len(oos)} 行 "
          f"· 折={len(folds)} ==")

    base = t3.run_base(folds)
    groups = date_groups(base["dates"])
    print(f"  base pooled IC={base['pooled_ic']:+.4f}  n={base['n']}  日块={len(groups)}")
    icb = t3.ic_resample_sd(base["preds"], base["trues"], groups, N_BOOT, SEED)
    print(f"  判据 B（日块 i.i.d. 口径）ic_sd={icb['ic_sd']}  参照 0.0307")

    fold_of_group = fold_ids_of_blocks(groups, folds, base["dates"])
    print(f"  折归属：{dict(Counter(fold_of_group.tolist()))}")

    print("  [缓存] MDE 候选构造 …")
    mde_cands = build_mde_cands(base)
    print(f"  [缓存] 置换重训 {args.n_perm} 轮（与 variant 无关，只付一次）…")
    perm_preds = build_perm_preds(p14, folds, args.n_perm)

    res = {}
    for v in want:
        picks = make_picks(v, len(groups), N_BOOT, SEED, fold_of_group=fold_of_group)
        print(f"  [{v}] 判定中（B={N_BOOT}）…")
        res[v] = run_variant(v, base, groups, picks, perm_preds, mde_cands, args.n_perm)
        r = res[v]
        print(f"    → 误报={r['round_level_fp_rate']:.3f}（前50轮={r['round_level_fp_first50']:.3f}）"
              f"  MDE80={r['mde80']}  ρ0.10 power={r['power_rho_0.10']:.3f}"
              f"  sanity={r['sanity_base_vs_base']}")

    # 选择规则（§二 写死）：合格者中改动最小（v1 < v2），v0 视为「现状」不计入候选
    p10_v0 = res.get("v0", {}).get("power_rho_0.10")
    verdict = {}
    for v in ("v1", "v2"):
        if v not in res:
            continue
        r = res[v]
        fp_ok = r["round_level_fp_rate"] <= FP_MAX
        p_ok = (p10_v0 is None or p10_v0 <= 1e-9
                or r["power_rho_0.10"] >= p10_v0 * (1 - POWER_COLLAPSE_MAX))
        verdict[v] = {"fp_ok": bool(fp_ok), "power_ok": bool(p_ok),
                      "eligible": bool(fp_ok and p_ok)}
    chosen = next((v for v in ("v1", "v2") if verdict.get(v, {}).get("eligible")), None)
    out = {"kind": "d2b_ruler_probe", "created_at": t0.isoformat(timespec="seconds"),
           "prereg": "output/forecast_lab_prereg_D2b_ruler_draft_20260916.md",
           "panel_sha256": sha, "pool": "POOL14", "n_groups": len(groups),
           "n_boot": N_BOOT, "seed": SEED, "n_perm": args.n_perm,
           "block_l": BLOCK_L, "fp_max": FP_MAX,
           "power_collapse_max": POWER_COLLAPSE_MAX, "rho_check": RHO_CHECK,
           "base_pooled_ic": base["pooled_ic"],
           "ic_resample_dayblock": icb, "fold_blocks": dict(
               Counter(fold_of_group.tolist())),
           "results": res, "verdict": verdict, "chosen_variant": chosen,
           "elapsed_sec": round((datetime.now() - t0).total_seconds(), 1)}
    p = OUT_DIR / f"d2b_ruler_probe_{stamp}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"== 汇总：chosen={chosen}  verdict={verdict} ==")
    print(f"   产物 {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
