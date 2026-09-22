r"""候选 A（rel_rank）冻结专家的**打分侧**（2026-09-13，接法 ① 影子并列）。

为什么要单独一个脚本（而不是写在 shadow_policy 里）：
  每日 shadow 由 `run.py` 以 `sys.executable`（= 生产 venv `.venv`）调用，而 A 的
  冻结产物是 LightGBM 模型，**lightgbm 只装在隔离的 `.venv-lab`**（这是项目既有的
  lab 依赖隔离约定）。故本脚本跑在 lab venv，由 shadow_policy 以子进程调用，
  **不往生产 venv 装任何包、不改任何 cron/自动化任务**。

契约（stdout 单行 JSON，供 shadow_policy 解析）：
  {"ok": true, "date": "...", "model_sha256": "...", "usage_lock": "...",
   "horizon": 5, "arms": ["a_mean","a_med"],
   "scores": {"002112": {"a_mean": 0.0123, "a_med": 0.0119}, ...}}
失败时：{"ok": false, "reason": "<机器可读原因>"}，退出码仍为 0（让调用方决定降级）。

纪律：零网络；只读 post 槽特征与冻结产物；**不含任何未来标签**；只打印不落盘。

用法：
  .\.venv-lab\Scripts\python.exe -X utf8 experiments\forecast_lab\score_expert_a.py \
      [--date YYYY-MM-DD] [--artifact data/models/expert_a_relrank_v1.pkl]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "experiments" / "forecast_lab"))

import numpy as np                                              # noqa: E402

import run_m0_power as m0                                       # noqa: E402
from core import intraday_feature_store as feature_store        # noqa: E402
from core import forecast_engine as fe                          # noqa: E402


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_post_features(date: str | None):
    """当日 post 槽特征（与 shadow_policy 同源、同口径；只取 ≥ 决策日可得信息）。

    V4.4 步 2 量纲迁移：切换日前的行 est_chg 是百分数，过唯一桥归一到
    契约 fraction；切换日后原样透传（判别真源 pit1455_contract）。
    """
    from core.pit1455_contract import est_chg_from_pct, est_chg_live_is_fraction
    records = feature_store.load_history(slot="post")
    if not records:
        return None, {}
    d = date or max(r.get("date", "") for r in records)
    out = {}
    for rec in records:
        if rec.get("date") != d:
            continue
        if rec.get("model_version") != fe.MODEL_VERSION:
            continue
        feats = rec.get("features") or {}
        row = {k: feats.get(k) for k in fe.FEATURE_KEYS}
        if not est_chg_live_is_fraction(d):
            row["est_chg"] = est_chg_from_pct(row.get("est_chg"))
        out[rec.get("fund", "")] = row
    out.pop("", None)
    return d, out


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--artifact", default=str(
        BASE / "data" / "models" / "expert_a_relrank_v1.pkl"))
    args = ap.parse_args()

    art = Path(args.artifact)
    meta_path = art.with_suffix(".meta.json")
    if not art.exists() or not meta_path.exists():
        emit({"ok": False, "reason": "artifact_missing"})
        return 0
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        emit({"ok": False, "reason": "meta_unreadable"})
        return 0
    actual = _sha256(art)
    if actual != meta.get("artifact_sha256"):
        emit({"ok": False, "reason": "artifact_sha_mismatch",
              "expected": meta.get("artifact_sha256"), "actual": actual})
        return 0

    d, post = load_post_features(args.date)
    if not d or not post:
        emit({"ok": False, "reason": "no_post_features"})
        return 0

    try:
        with open(art, "rb") as fh:
            payload = pickle.load(fh)
    except Exception:
        emit({"ok": False, "reason": "artifact_unpickle_failed"})
        return 0

    # 特征顺序与协议以**产物内记录**为准（自描述），不信任调用方的隐式假设
    keys = list(payload.get("feature_keys") or [])
    if keys != list(fe.FEATURE_KEYS):
        emit({"ok": False, "reason": "feature_keys_mismatch_with_engine",
              "artifact_keys": keys, "engine_keys": list(fe.FEATURE_KEYS)})
        return 0

    funds = sorted(post)
    X = m0.matrix_existing([post[f] for f in funds])
    scores = {f: {} for f in funds}
    for arm, spec in (payload.get("arms") or {}).items():
        booster = spec.get("booster")
        if booster is None:
            continue
        pred = np.asarray(booster.predict(X), dtype=float)
        for f, v in zip(funds, pred):
            scores[f][arm] = round(float(v), 6)

    emit({"ok": True, "date": d,
          "artifact_sha256": actual,
          "samples_sha256": payload.get("samples_sha256"),
          "trained_at": payload.get("trained_at"),
          "n_train": payload.get("n_train"),
          "horizon": payload.get("horizon"),
          "label_basis": payload.get("label_basis"),
          "usage_lock": meta.get("usage_lock",
                                 "relative_pool_only_no_abs_display"),
          "arms": sorted((payload.get("arms") or {}).keys()),
          "n_pool": len(funds),
          "scores": scores})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
