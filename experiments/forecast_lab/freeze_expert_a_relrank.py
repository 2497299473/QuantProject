r"""冻结「候选 A · rel_rank」专家产物（2026-09-13，接法 ① 影子并列）。

预注册（本文实现前落盘）：
  output/forecast_lab_prereg_A_relative_pool_20260913.md §二「冻结规格」
  —— 冻结什么、用哪些样本、哪两条臂、怎么校验，全部按该文执行，跑后不改。

定位：A 的 G2 资格是**离线回测**结论（4 折 WF）。本脚本把 A 固化为一个
**只读、可校验**的产物，供 shadow_policy 逐日做「只记录不执行」的前瞻对照。
**不进 model_registry**（A 未获生产批准，登记会污染主证据链）。

冻结口径（与预注册逐条对应）：
- 训练集 = frozen 0910 样本中 fwd5 非空的全部样本（末个标签日 ≤ 2026-08-12）；
- 决策日 ≥ 2026-09-11 > 所有标签日 → 无标签泄漏（结构保证）；
- 特征 = m0.matrix_existing（7 键 × 值/掩码 = 14 维），超参/轮数/种子全取 m0；
- 两条臂都冻结（a_mean / a_med）：G2 中两臂同过相同判据，只挑一条等于按结果挑臂。

零网络；只读 frozen 样本；除 data/models/expert_a_relrank_v1.* 外不写任何文件。

用法：
  .\.venv-lab\Scripts\python.exe -X utf8 experiments\forecast_lab\freeze_expert_a_relrank.py
"""
from __future__ import annotations

import hashlib
import json
import pickle
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "experiments" / "forecast_lab"))

import lightgbm as lgb                                       # noqa: E402
import numpy as np                                           # noqa: E402

import run_m0_power as m0                                    # noqa: E402
from run_a_relrank import label_map_rel                      # noqa: E402

H = m0.HORIZON
FROZEN = BASE / "forecast_outputs" / "samples_frozen_20260910.jsonl"
OUT_PKL = BASE / "data" / "models" / "expert_a_relrank_v1.pkl"
OUT_META = BASE / "data" / "models" / "expert_a_relrank_v1.meta.json"

# 两条臂 = 同一训练协议，唯一变量是同池中心的定义（progressive：都冻结）
ARMS = {"a_mean": "mean", "a_med": "median"}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    print("== [0] 校验冻结样本 sha256 ==")
    raw = FROZEN.read_text(encoding="utf-8")
    sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    print(f"  {sha}")
    if sha != m0.SAMPLES_SHA_EXPECT:
        print(f"  [ABORT] 与 m0.SAMPLES_SHA_EXPECT（{m0.SAMPLES_SHA_EXPECT}）不一致 → 拒绝冻结。")
        return 1

    samples = [json.loads(ln) for ln in raw.splitlines() if ln.strip()]
    train = m0.with_label(samples)          # fwd5 非空
    tag_dates = [s["date"] for s in train]
    print(f"  n_all={len(samples)}  n_train={len(train)}  "
          f"标签日 {min(tag_dates)} ~ {max(tag_dates)}")
    if max(tag_dates) >= "2026-09-11":
        print("  [ABORT] 训练标签日已触及首个决策日 → 存在泄漏风险，拒绝冻结。")
        return 1

    X = m0.matrix_existing(train)
    print(f"  特征矩阵 {X.shape}（7 键 × 值/掩码 = 14 维）")

    boosters = {}
    for arm, how in ARMS.items():
        lmap = label_map_rel(train, how)
        y = np.array([lmap[(s["fund"], s["date"])] for s in train], dtype=float)
        params = {**m0.LGB_PARAMS, "seed": m0.SEED, "deterministic": True}
        booster = lgb.train(params, lgb.Dataset(X, label=y),
                            num_boost_round=m0.N_ROUNDS)
        boosters[arm] = booster
        print(f"  [{arm}] label={how}  y_mean={y.mean():+.5f}  trained.")

    payload = {
        "kind": "expert_a_relrank",
        "schema": 1,
        "label_basis": "same_day_cross_section_excess",
        "horizon": H,
        "feature_keys": list(m0.EXISTING_KEYS),
        "masking": "value_then_mask (14 dim)",
        "lgb_params": {k: v for k, v in m0.LGB_PARAMS.items()},
        "num_boost_round": m0.N_ROUNDS,
        "seed": m0.SEED,
        "samples_sha256": sha,
        "samples_file": FROZEN.name,
        "n_train": len(train),
        "train_date_max": max(tag_dates),
        "trained_at": datetime.now().isoformat(timespec="seconds"),
        "arms": {arm: {"label": how, "booster": boosters[arm]}
                 for arm, how in ARMS.items()},
    }
    OUT_PKL.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PKL, "wb") as fh:
        pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
    pkl_sha = _sha256(OUT_PKL)

    meta = {
        "kind": "expert_a_relrank_freeze_meta",
        "artifact": OUT_PKL.name,
        "artifact_sha256": pkl_sha,
        "frozen_at": datetime.now().isoformat(timespec="seconds"),
        "prereg": "output/forecast_lab_prereg_A_relative_pool_20260913.md",
        "samples_sha256": sha,
        "n_train": len(train),
        "train_date_max": max(tag_dates),
        "horizon": H,
        "arms": sorted(ARMS),
        "usage_lock": "relative_pool_only_no_abs_display",
        "not_registered_note": "不写入 data/model_registry/registry.json（A 未获生产批准）",
    }
    OUT_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n== [1] 落盘 ==\n  {OUT_PKL}\n  sha256={pkl_sha}\n  {OUT_META}")
    print("[done] 仅落盘 frozen 专家产物；未改 config / registry / 生产 .py。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
