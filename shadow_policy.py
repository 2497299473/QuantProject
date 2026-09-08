#!/usr/bin/env python3
"""Shadow Policy 记录器（2026-09-01，P1-⑦）：真实世界纸面交易样本积累。

问题：回测终究是历史。Policy（加仓/减仓/不动）要证明「在真实世界里成立」，
需要完全没有回测选择偏差的未来样本——每天用冻结模型算预测与政策动作，
**不执行、只记录**。积累 30~60 个交易日后，可回看样本与后续真实净值对照，
得到第一份「无回测选择偏差」的 Policy 证据。

纪律（与项目铁律对齐）：
- **不接下单、不动 config.decision 门禁**：只读 + jsonl 落盘，status 恒为 "shadow"；
- **只加载冻结已验证模型**（data/models/forecast_v3.pkl，经 registry sha256
  + 特征协议三重校验，load_models 内置）——不重训。Shadow 必须与「当时部署的
  权重」完全一致，否则样本污染；
- **PIT**：决策日 D 的路径 σ 窗口只用 date < D 的样本（D 日 fwd1 在 D+1 才
  实现，不得进入窗口）；
- **政策动作口径 = T+1**（与 backtest_forecast_policy 唯一现有政策证据同口径，
  θ=0.6 固定不后验调参）；T+5 概率/分位/路径并行记录作参照；
- **Evidence Contract**（GPT 十五：Evidence DAG 入口）：每条记录内嵌模型
  sha256 / trained_at / oos_start / feature protocol 版本 / 数据 manifest
  sha256 / RECENT_WINDOW / θ——一眼可查「这条 shadow 由哪个模型、哪份数据算出」。

用法：python3 shadow_policy.py [--date YYYY-MM-DD] [--out shadow_actions.jsonl]
  --date 缺省 = 数据中最新样本日（当前可决策的最近一天）。
  幂等：(date, fund) 已记录的不重复写，同日重跑安全。
输出：output/shadow_actions.jsonl（每行 = 一基金一决策日）+ stdout 摘要。
"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from backtest_spread import load_samples          # noqa: E402
from core import forecast_engine as fe            # noqa: E402
from core import path_forecast as pf              # noqa: E402
from core import model_registry as mr             # noqa: E402

POLICY_THR = 0.6          # 与 backtest_forecast_policy 同值（固定，不后验调参）


def policy_action(p_up: float | None, p_down: float | None,
                  theta: float = POLICY_THR) -> str:
    """政策动作（阈值固定）：ADD / HOLD / REDUCE / NA。

    边界归 ADD（p_up ≥ θ，与 backtest_forecast_policy.policy_actions 一致）；
    双超阈值时 ADD 优先（同该脚本向量化实现的赋值顺序）。
    """
    if p_up is None or p_down is None:
        return "NA"
    if p_up >= theta:
        return "ADD"
    if p_down >= theta:
        return "REDUCE"
    return "HOLD"


def load_existing_keys(path: Path) -> set:
    """已记录的 (date, fund) 键集合 → 幂等去重。坏行跳过不抛错。"""
    if not path.exists():
        return set()
    keys = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            keys.add((rec["date"], rec["fund"]))
        except (json.JSONDecodeError, KeyError):
            continue
    return keys


def append_records(path: Path, records: list[dict]) -> int:
    """把新记录追加进 jsonl（读旧 → 拼新 → 原子重写）。返回新增条数。"""
    existing = ([ln for ln in path.read_text(encoding="utf-8").splitlines()
                 if ln.strip()] if path.exists() else [])
    existing += [json.dumps(r, ensure_ascii=False) for r in records]
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(existing) + "\n", encoding="utf-8")
    tmp.replace(path)
    return len(records)


def model_contract() -> dict:
    """Evidence Contract：模型指纹 + 特征协议 + 数据 manifest，内嵌每条记录。"""
    entry = mr.load_registry().get("models", {}).get(
        f"forecast_v{fe.MODEL_VERSION}.pkl", {})
    fp = entry.get("feature_protocol") or {}
    manifest_path = BASE_DIR / "data" / "manifest.json"
    manifest_sha = (hashlib.sha256(manifest_path.read_bytes()).hexdigest()[:16]
                    if manifest_path.exists() else None)
    return {
        "model_version": fe.MODEL_VERSION,
        "model_sha256": entry.get("sha256"),
        "trained_at": (entry.get("meta") or {}).get("trained_at"),
        "oos_start": (entry.get("meta") or {}).get("oos_start"),
        "feature_protocol_version": fp.get("protocol_version"),
        "feature_dim": fp.get("feature_dim"),
        "data_manifest_sha256": manifest_sha,
        "recent_window": pf.RECENT_WINDOW,      # v1.3 起冻结
        "policy_theta": POLICY_THR,
        "policy_basis": "T+1",                  # 动作口径（T+5 仅参照）
    }


def _fmt(v) -> str:
    return f"{v:+.4f}" if isinstance(v, (int, float)) else "  —  "


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None,
                    help="决策日期（缺省 = 数据中最新样本日）")
    ap.add_argument("--out", default=None,
                    help="输出文件名（默认 output/shadow_actions.jsonl）")
    args = ap.parse_args()

    t0 = time.time()
    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    funds = cfg["fund_pool"]

    # 冻结模型（registry sha256 + 特征协议校验；失败拒绝启动）
    engine = fe.ForecastEngine(cfg)
    if not engine.load_models():
        print(f"[fail] 冻结模型加载失败（load_error={engine.load_error}）→ 拒绝启动。"
              "Shadow 必须运行在已验证冻结权重上；如需换模型，先走 "
              "train_forecast_model.py 重训 + 重新登记验证。")
        return 1
    contract = model_contract()
    print(f"== [0] 冻结模型 v{fe.MODEL_VERSION} sha256={str(contract['model_sha256'])[:12]}… "
          f"trained={contract['trained_at']} oos_start={contract['oos_start']} "
          f"（load_models 三重校验通过）==")

    print("== [1] 加载样本（PIT 口径）==")
    samples = load_samples()
    if not samples:
        print("[fail] 无样本")
        return 1
    dates = sorted({s["date"] for s in samples})
    d = args.date or dates[-1]
    if d not in dates:
        print(f"[fail] {d} 不在样本日期内（最新 {dates[-1]}）")
        return 1

    out_path = BASE_DIR / "output" / (args.out or "shadow_actions.jsonl")
    known = load_existing_keys(out_path)

    # PIT：路径 σ 窗口只用 date < d 的样本（d 日 fwd1 在 d+1 才实现）
    path_samples = [s for s in samples if s["date"] < d]

    # 持仓快照（as_of_date 可能滞后，如实标注）
    try:
        hol = json.loads((BASE_DIR / "holdings.json").read_text(encoding="utf-8"))
        holdings_meta = hol.get("as_of_date")
        holdings_by_fund = hol.get("funds", {})
    except Exception:
        holdings_meta, holdings_by_fund = None, {}

    records = []
    print(f"== [2] shadow 记录（决策日 {d} · 动作口径 T+1 · θ={POLICY_THR} · 只记录不执行）==")
    for code in funds:
        rows = [s for s in samples if s["fund"] == code and s["date"] == d]
        if not rows:
            print(f"  {code}: 当日无特征行（持仓快照/净值缺失）→ 跳过")
            continue
        s = rows[0]
        feat = {k: s.get(k) for k in fe.FEATURE_KEYS}
        fc = engine.predict(feat, fund_code=code)
        t1, t5 = fc.t1, fc.t5
        act = policy_action(t1.p_up, t1.p_down)
        # 路径（基金级 v1.3，σ 窗口 = 该基金最近 20 个交易日，全部 < d）
        pfo = pf.forecast_path(path_samples, 5, fund_code=code)
        path_block = {"model_ready": pfo.model_ready}
        if pfo.model_ready:
            params = pf.fit_path_params(path_samples, 5, fund_code=code)
            path_block = {
                "model_ready": True,
                "mu": round(params[0], 6), "sigma": round(params[1], 6),
                "sigma_src": pfo.meta.get("sigma_src"),
                "sigma_scope": pfo.meta.get("sigma_scope"),
                "q10": round(pfo.q10, 6), "q90": round(pfo.q90, 6),
                "mdd_q10": round(pfo.mdd_q10, 6),
                "mdd_q50": round(pfo.mdd_q50, 6),
                "mfe_q50": round(pfo.mfe_q50, 6),
            }
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "date": d,
            "fund": code,
            "contract": contract,
            "features": {k: (round(v, 6) if isinstance(v, (int, float)) else v)
                         for k, v in feat.items()},
            "t1": {"p_up": round(t1.p_up, 6), "p_flat": round(t1.p_flat, 6),
                   "p_down": round(t1.p_down, 6), "e_return": round(t1.e_return, 6),
                   "q10": round(t1.q10, 6), "q50": round(t1.q50, 6),
                   "q90": round(t1.q90, 6)},
            "t5": {"p_up": round(t5.p_up, 6), "p_flat": round(t5.p_flat, 6),
                   "p_down": round(t5.p_down, 6), "e_return": round(t5.e_return, 6),
                   "q10": round(t5.q10, 6), "q50": round(t5.q50, 6),
                   "q90": round(t5.q90, 6)},
            "path": path_block,
            "policy": {"theta": POLICY_THR, "basis": "T+1", "action": act},
            "holdings": ({"as_of_date": holdings_meta,
                          **holdings_by_fund.get(code, {})}
                         if holdings_by_fund else None),
            "status": "shadow",            # 固定标记：不执行
        }
        records.append(rec)
        print(f"  {code}: T+1 p_up={t1.p_up:.2f} p_down={t1.p_down:.2f} "
              f"→ {act} ｜ T+5 q10={t5.q10:+.4f} q50={t5.q50:+.4f} q90={t5.q90:+.4f} ｜ "
              f"path σ={_fmt(path_block.get('sigma'))} src={path_block.get('sigma_src', '—')}")

    new_recs = [r for r in records if (r["date"], r["fund"]) not in known]
    skipped = len(records) - len(new_recs)
    if new_recs:
        n = append_records(out_path, new_recs)
        print(f"\n== [3] 落盘 → {out_path}（新增 {n} 条，同日已记录跳过 {skipped} 条）==")
    else:
        print(f"\n== [3] {skipped} 条当日全部已记录 → 幂等跳过（可放心重跑）==")
    print("[done] shadow 只记录、不执行、不触碰生产门禁 · 不构成投资建议")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
