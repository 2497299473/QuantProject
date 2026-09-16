#!/usr/bin/env python3
"""Shadow Policy 记录器（2026-09-01，P1-⑦）：真实世界纸面交易样本积累。

问题：回测终究是历史。Policy（加仓/减仓/不动）要证明「在真实世界里成立」，
需要完全没有回测选择偏差的未来样本——每天用冻结模型算预测与政策动作，
**不执行、只记录**。积累 30~60 个交易日后，可回看样本与后续真实净值对照，
得到第一份「无回测选择偏差」的 Policy 证据。

纪律（与项目铁律对齐）：
- **不接下单、不动 config.decision 门禁**：只读 + jsonl 落盘，status 恒为 "shadow"；
- **模型闸门 = 双通道**（2026-09-13 D2 拍板）：① 完整批准（模型 hash + 特征协议
  + validation 报告 hash + promotion=approved + config.model_ready=true）→
  promotion_mode=approved_full；② 预注册降级授权（data/promotion_prereg.json，
  判据 prereg_shadow_v1，有 expiry）→ promotion_mode=prereg_degraded，
  **仅纸面记录**，对外展示门（verify_approval）不受影响，两类证据永不混池
  （逐条打标可筛）。两通道都不过 → 拒记并非零退出；
- **不重训**；**零网络硬纪律**：当前特征读 post 槽，路径 σ 只读冻结样本 JSONL
  （缺失即失败，不回退 load_samples——避免 TTL 到期触发前复权 K 线重取漂移）；
- **当前特征来自真实 post 累积存储**，生成时不含任何 fwd/mdd/mfe 未来标签；
- **PIT**：决策日 D 的路径 σ 窗口只用 date < D 的历史样本（D 日未来标签不得进入）；
- **政策动作口径 = T+1**（与 backtest_forecast_policy 唯一现有政策证据同口径，
  θ=0.6 固定不后验调参）；T+5 概率/分位/路径并行记录作参照；
- **Evidence Contract**（GPT 十五：Evidence DAG 入口）：每条记录内嵌模型
  sha256 / trained_at / oos_start / feature protocol 版本 / 数据 manifest
  sha256 / RECENT_WINDOW / θ——一眼可查「这条 shadow 由哪个模型、哪份数据算出」。

用法：python3 shadow_policy.py [--date YYYY-MM-DD] [--out shadow_actions.jsonl]
  --date 缺省 = intraday_features.jsonl 中最新 post 特征日。
  幂等：(date, fund) 已记录的不重复写，同日重跑安全。
输出：output/shadow_actions.jsonl（每行 = 一基金一决策日）+ stdout 摘要。
"""
import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))


def load_frozen_samples() -> tuple[list[dict] | None, str]:
    """零网络样本源：只读最新冻结样本 JSONL（path σ 参数用，不回退）。

    降级授权打开后 shadow 每日必跑，不能让路径块触发 K 线/净值重取
    （既耗东财频控预算，又会让前复权序列追溯改写→尺子漂移，09-10 已实证）。
    冻结样本里的 fwd/mdd/mfe 列只喂 date < D 的历史 σ 拟合，不进当前特征；
    当前特征一律来自 post 槽（contains_future_labels=False 不变）。
    不联网、不回退：冻结缺失时返回 (None, reason)，调用方必须显式处理，
    不得静默降级到可能联网的 load_samples。σ 鲜度 gap 写进记录的
    source.samples_source，下次重冻结（候选 D）自然消除。
    """
    import json as _json
    cands = sorted((BASE_DIR / "forecast_outputs").glob("samples_frozen_*.jsonl"))
    if not cands:
        return None, "no_frozen_samples"
    src = cands[-1]
    rows = []
    for ln in src.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            rows.append(_json.loads(ln))
    if not rows:
        return None, "frozen_samples_empty"
    return rows, f"forecast_outputs/{src.name}"


from core import forecast_engine as fe            # noqa: E402
from core import intraday_feature_store as feature_store  # noqa: E402
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


# ---- 证据通道隔离（V4-A，2026-09-16·P0-1 尾巴清扫）----
# 三条通道允许并存于同一 jsonl（保留单一 longitudinal stream 的运维便利），
# 但**默认禁止跨通道聚合**：任何统计必须先显式声明 channel。
CHANNELS = ("approved_full", "prereg_degraded", "legacy_invalid")
UNCLASSIFIED = "unclassified"


def record_channel(rec: dict) -> str:
    """判定单条 shadow 记录归属的证据通道（判定顺序即优先级）。

    - legacy_invalid：2026-09-11 前用已实现净值/历史 K 线生成、含未来标签，
      已被整体作废，**永不与前瞻样本同池**；
    - approved_full / prereg_degraded：读 contract.promotion_mode；
    - unclassified：两处都取不到（老格式/字段损坏）→ 只能在清点里出现，
      不得进任何 scorecard。
    """
    if rec.get("evidence_validity") == "legacy_invalid":
        return "legacy_invalid"
    mode = (rec.get("contract") or {}).get("promotion_mode")
    if mode in ("approved_full", "prereg_degraded"):
        return mode
    return UNCLASSIFIED


def load_records_by_channel(path: Path) -> dict[str, list[dict]]:
    """按通道分桶读取 jsonl（坏行跳过）→ {channel: [records]}。

    这是**唯一**被推荐的读取入口：调用方拿到的永远是单通道列表，
    跨通道比较必须自己显式合并（默认写不出聚合误用）。
    """
    buckets: dict[str, list[dict]] = {c: [] for c in (*CHANNELS, UNCLASSIFIED)}
    if not path.exists():
        return buckets
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        buckets[record_channel(rec)].append(rec)
    return buckets


def archive_legacy_records(path: Path, archive: Path) -> tuple[int, int]:
    """把 legacy_invalid 记录从活跃流搬进归档文件（幂等，保持原行序）。

    返回 ``(archived_count, remaining_count)``。legacy 记录已被整体作废，
    搬出后活跃流 = 纯前瞻样本，天然不可能被 scorecard 误聚合。
    """
    if not path.exists():
        return 0, 0
    legacy: list[dict] = []
    keep: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        (legacy if record_channel(rec) == "legacy_invalid" else keep).append(rec)
    if not legacy:
        return 0, len(keep)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with archive.open("a", encoding="utf-8") as fh:
        for r in legacy:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep),
                   encoding="utf-8")
    tmp.replace(path)
    return len(legacy), len(keep)


def append_records(path: Path, records: list[dict]) -> int:
    """把新记录追加进 jsonl（读旧 → 拼新 → 原子重写）。返回新增条数。"""
    existing = ([ln for ln in path.read_text(encoding="utf-8").splitlines()
                 if ln.strip()] if path.exists() else [])
    existing += [json.dumps(r, ensure_ascii=False) for r in records]
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(existing) + "\n", encoding="utf-8")
    tmp.replace(path)
    return len(records)


def load_post_inputs(date: str | None = None) -> tuple[str | None, dict[str, dict]]:
    """读取真实运行时冻结的 post 特征，不接触任何未来标签。

    返回 ``(decision_date, {fund: record})``。同日同基金的重复运行由
    intraday_feature_store 统一 last-write-wins；只接受当前模型协议版本。
    """
    records = feature_store.load_history(slot="post")
    if not records:
        return None, {}
    decision_date = date or max(r.get("date", "") for r in records)
    selected: dict[str, dict] = {}
    for rec in records:
        if rec.get("date") != decision_date:
            continue
        mv = rec.get("model_version")
        if mv is not None and int(mv) != fe.MODEL_VERSION:
            continue
        raw_features = rec.get("features") or {}
        selected[rec.get("fund", "")] = {
            "date": decision_date,
            "fund": rec.get("fund"),
            "timestamp": rec.get("timestamp"),
            "slot": "post",
            "model_version": mv,
            "features": {k: raw_features.get(k) for k in fe.FEATURE_KEYS},
            "context": rec.get("context") or {},
        }
    selected.pop("", None)
    return decision_date, selected


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
        "validation_decision": (entry.get("validation") or {}).get("decision"),
        "promotion_status": (entry.get("promotion") or {}).get("status"),
    }


def resolve_promotion_mode(engine) -> tuple[str | None, str]:
    """Shadow 双通道闸门（2026-09-13 D2 拍板）。

    返回 ``(promotion_mode, note)``；promotion_mode=None 表示两条通道都不过。
    - approved_full：完整批准（config.model_ready + registry 原子授权）。
    - prereg_degraded：预注册降级授权（data/promotion_prereg.json，仅纸面记录）。
      作用域硬边界：不影响 verify_approval（对外展示门）。
    """
    if engine.model_approved:
        return "approved_full", "完整批准契约通过"
    ok, reason = mr.evaluate_prereg_degradation(f"forecast_v{fe.MODEL_VERSION}.pkl")
    if ok:
        return "prereg_degraded", "预注册降级授权（仅纸面记录，对外展示门不受影响）"
    return None, reason


def mirror_relative_score(post_feats: dict[str, dict], code: str) -> dict:
    """用**生产真实现**算当日 `_score_relative`（不复制公式，防口径漂移）。

    post 裆的 `est_chg` 与 14:55 的 `est_return` 同为「当日重仓估算涨跌%」（已查源码），
    故直接以其为输入做「镜像分」，仅供事后对照，**不参与任何决策/下单**。
    惰性 import + 全异常吞掉：私有函数改签名只会让对照字段变 error，不影响主流程。
    """
    try:
        from core import decision_engine as de
    except Exception as e:                      # noqa: BLE001
        return {"error": f"import_failed:{type(e).__name__}"}
    me = (post_feats.get(code) or {}).get("est_chg")
    pool_est = {c: (f or {}).get("est_chg") for c, f in post_feats.items()}
    if me is None or len([v for v in pool_est.values() if v is not None]) < 2:
        return {"error": "est_chg_unavailable"}
    try:
        score, reasons = de._score_relative({"est_return": me}, pool_est, code)
        others = [v for k, v in pool_est.items() if k != code and v is not None]
        diff = float(me) - sum(others) / len(others)
    except Exception as e:                      # noqa: BLE001
        return {"error": f"call_failed:{type(e).__name__}"}
    return {"est_chg": round(float(me), 6),
            "diff_vs_pool_mean": round(diff, 6),
            "score_15": int(score),
            "reason": (reasons or [None])[0],
            "source": "decision_engine._score_relative (read-only mirror)",
            "semantics_note": "post 裆 est_chg 与 14:55 est_return 同为当日重仓估算涨跌%"}


def load_expert_a(date: str | None) -> dict:
    """调 lab venv 子进程给 A 专家打分（生产 venv 无 lightgbm，故隔环境执行）。

    不往生产 venv 装包、不改 cron——这是项目既有的 lab 依赖隔离约定。
    任何异常都只返回 {"error": ...}，由调用方写成逐条 error（主记录不得因此中断）。
    """
    lab_py = BASE_DIR / ".venv-lab" / "Scripts" / "python.exe"
    script = BASE_DIR / "experiments" / "forecast_lab" / "score_expert_a.py"
    if not lab_py.exists() or not script.exists():
        return {"error": "lab_env_or_script_missing"}
    cmd = [str(lab_py), "-X", "utf8", str(script)]
    if date:
        cmd += ["--date", date]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180,
                           cwd=str(BASE_DIR))
    except Exception as e:                      # noqa: BLE001
        return {"error": f"subprocess:{type(e).__name__}"}
    line = (r.stdout or "").strip().splitlines()
    if not line:
        return {"error": f"empty_stdout:{(r.stderr or '')[-200:]}"}
    try:
        out = json.loads(line[-1])
    except json.JSONDecodeError:
        return {"error": "unparsable_stdout"}
    if not out.get("ok"):
        return {"error": f"scorer:{out.get('reason')}"}
    return out


def expert_a_shadow_block(code: str, scores_out: dict,
                          post_feats: dict[str, dict]) -> dict:
    """组装一条记录的 `a_relrank` 块（两臂都记，不择优；G2-⑤′ 只读用途）。"""
    if "error" in scores_out:
        return {"error": scores_out["error"],
                "usage_lock": "relative_pool_only_no_abs_display"}
    scores = scores_out.get("scores") or {}
    arms_avail = list(scores_out.get("arms") or [])
    if code not in scores:
        return {"error": "fund_not_scored", "arms_available": arms_avail}
    # 池内排名：同一天四个标的按该臂分数降序（1 = 最看好）；无未来数据参与
    arms_block = {}
    for arm in arms_avail:
        v = scores[code].get(arm)
        if v is None:
            continue
        rank = 1 + sum(1 for s in scores.values()
                       if s.get(arm) is not None and float(s[arm]) > float(v))
        arms_block[arm] = {"score": round(float(v), 6),
                           "rank_in_pool": int(rank),
                           "n_pool": int(scores_out.get("n_pool") or len(scores))}
    mirror = mirror_relative_score(post_feats, code)
    # 分歧标注（预注册 §三）：以 a_mean 符号 vs 镜像 diff 符号，不调和、不改分
    divergence = "none"
    am = (arms_block.get("a_mean") or {}).get("score")
    diff = mirror.get("diff_vs_pool_mean")
    if am is not None and isinstance(diff, (int, float)) and am != 0 and diff != 0:
        if (am > 0) != (diff > 0):
            divergence = ("a_bullish_live_bearish" if am > 0
                          else "a_bearish_live_bullish")
    return {
        "artifact_sha256": str(scores_out.get("artifact_sha256", ""))[:16] + "…",
        "samples_sha256": str(scores_out.get("samples_sha256", ""))[:16] + "…",
        "frozen_at": scores_out.get("trained_at"),
        "n_train": scores_out.get("n_train"),
        "horizon": scores_out.get("horizon"),
        "label_basis": scores_out.get("label_basis"),
        "usage_lock": scores_out.get("usage_lock",
                                    "relative_pool_only_no_abs_display"),
        "arms": arms_block,
        "mirror": mirror,
        "divergence": divergence,
        "role": "shadow_only_not_executed",
        "contains_future_labels": False,
    }


def _fmt(v) -> str:
    return f"{v:+.4f}" if isinstance(v, (int, float)) else "  —  "


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None,
                    help="决策日期（缺省 = 数据中最新样本日）")
    ap.add_argument("--out", default=None,
                    help="输出文件名（默认 output/shadow_actions.jsonl）")
    ap.add_argument("--channels", action="store_true",
                    help="只清点各证据通道条数（不加载模型、不写任何文件）")
    ap.add_argument("--archive-legacy", action="store_true",
                    help="把 legacy_invalid 记录搬进 output/shadow_actions_legacy_invalid.jsonl")
    args = ap.parse_args()

    # 证据通道隔离入口（2026-09-16）：两条都不加载模型，纯文件操作。
    cli_out = BASE_DIR / "output" / (args.out or "shadow_actions.jsonl")
    if args.channels:
        counts = load_records_by_channel(cli_out)
        print(f"== 证据通道清点（{cli_out.name}·跨通道禁止聚合）==")
        for ch in (*CHANNELS, UNCLASSIFIED):
            print(f"  {ch:<16} {len(counts[ch])} 条")
        return 0
    if args.archive_legacy:
        n_arch, n_keep = archive_legacy_records(
            cli_out, BASE_DIR / "output" / "shadow_actions_legacy_invalid.jsonl")
        print(f"== [arch] legacy_invalid {n_arch} 条 → shadow_actions_legacy_invalid.jsonl；"
              f"活跃流剩 {n_keep} 条 ==")
        return 0

    t0 = time.time()
    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    funds = cfg["fund_pool"]

    # 冻结模型：安全加载不等于对外批准。完整批准之外另有一条预注册降级通道
    # （仅纸面记录；判据与有效期登记在 data/promotion_prereg.json，
    # 规则见 output/forecast_lab_prereg_rules_20260913.md §二 R2）。
    engine = fe.ForecastEngine(cfg)
    if not engine.load_models():
        print(f"[fail] 冻结模型加载失败（load_error={engine.load_error}）→ 拒绝启动。")
        return 1
    promotion_mode, gate_note = resolve_promotion_mode(engine)
    if promotion_mode is None:
        print(f"[fail] 模型未获完整批准（config_ready={engine.model_ready}, "
              f"approval_error={engine.approval_error}），且降级授权不通过"
              f"（{gate_note}）→ Shadow 不记录，避免失败模型污染前瞻证据。")
        return 1
    contract = model_contract()
    contract["promotion_mode"] = promotion_mode
    print(f"== [0] 冻结模型 v{fe.MODEL_VERSION} sha256={str(contract['model_sha256'])[:12]}… "
          f"trained={contract['trained_at']} oos_start={contract['oos_start']} "
          f"promotion={contract['promotion_status']} mode={promotion_mode}（{gate_note}）==")

    print("== [1] 加载当日 post 特征（无未来标签）==")
    d, post_inputs = load_post_inputs(args.date)
    if not d or not post_inputs:
        print(f"[fail] {'指定日期 '+args.date if args.date else '最新日期'} 无 post 特征")
        return 1
    # 历史样本仅用于 date < d 的路径波动参数；绝不用于选择决策日或当前特征。
    # 零网络硬纪律：只读冻结 JSONL，缺失即失败（不回退 load_samples，避免 TTL
    # 到期触发前复权 K 线重取→尺子漂移）。
    samples, samples_source = load_frozen_samples()
    if samples is None:
        print(f"[fail] 路径 σ 样本不可用（{samples_source}）→ 拒绝启动。")
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

    # 候选 A（rel_rank）**只读影子并列**：接法 ①，不改分、不入决策链（G2-⑤′）。
    # 打分跑在 .venv-lab（生产 venv 无 lightgbm）；**任何失败都不得阻断主记录**，
    # 只在逐条 a_relrank 里落 error——明晚 09-14 的 D2 主目的不能因为一个对照块落空。
    post_feats = {c: (r or {}).get("features") or {} for c, r in post_inputs.items()}
    a_out = load_expert_a(d)
    if "error" in a_out:
        print(f"== [1.5] 候选 A 影子打分：不可用（{a_out['error']}）→ 主记录照常产出 ==")
    else:
        print(f"== [1.5] 候选 A 影子打分：arms={a_out.get('arms')} "
              f"artifact={str(a_out.get('artifact_sha256'))[:12]}… "
              f"n_train={a_out.get('n_train')}（只记录不执行）==")

    records = []
    print(f"== [2] shadow 记录（决策日 {d} · 动作口径 T+1 · θ={POLICY_THR} · 只记录不执行）==")
    for code in funds:
        source_rec = post_inputs.get(code)
        if not source_rec:
            print(f"  {code}: 当日无真实 post 特征 → 跳过")
            continue
        feat = source_rec["features"]
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
            "source": {"store": "data/intraday_features.jsonl", "slot": "post",
                       "observed_at": source_rec.get("timestamp"),
                       "source_model_version": source_rec.get("model_version"),
                       "contains_future_labels": False,
                       "samples_source": samples_source},
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
            "a_relrank": expert_a_shadow_block(code, a_out, post_feats),
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
