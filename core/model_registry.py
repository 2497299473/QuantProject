"""模型注册表 + SHA256 完整性校验（2026-08-29，GPT 三审 P1，提前落地）。

背景：
- 此前 pkl 落盘只验「版本/特征键/horizon」三重元数据，文件本身被篡改/损坏
  （哪怕改一字节）不会被发现，直到推理结果异常才可能察觉。
- 本模块为每个 pkl 维护 sha256 + 训练元数据（trained_at / oos_start / n_train
  / 关键 OOS 指标摘要）→ data/models/registry.json，单一事实来源。

纪律（与项目铁律「有证据才上线」对齐）：
1. save_models 原子写 pkl 后**立即**登记（先 pkl 后 registry，registry 缺失
   时 load 拒绝加载——宁缺毋滥）。
2. load_models 三重校验之外追加 sha256 校验；registry 缺失/hash 不符 →
   拒绝加载并保持未训练占位（不阻断主流程）。
3. 注册表本身只追加当前版本条目；历史版本条目保留（审计用），load 只认
   forecast_v{MODEL_VERSION}.pkl 对应的条目。
4. registry 写入失败不删已落盘 pkl（留孤儿文件由下次 save 覆盖），但会显式
   返回 False 让调用方知道「权重在、注册缺失」。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
MODELS_DIR = BASE_DIR / "data" / "model_registry"
REGISTRY_PATH = MODELS_DIR / "registry.json"


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_registry() -> dict:
    """读注册表；不存在/损坏返回空结构（不抛异常）。"""
    if not REGISTRY_PATH.exists():
        return {"models": {}}
    try:
        reg = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        if not isinstance(reg, dict) or not isinstance(reg.get("models"), dict):
            return {"models": {}}
        return reg
    except (json.JSONDecodeError, OSError):
        return {"models": {}}


def _save_registry(reg: dict) -> bool:
    """原子写注册表（tmp + replace）。失败返回 False 不抛异常。"""
    try:
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = REGISTRY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(REGISTRY_PATH)
        return True
    except OSError:
        return False


def capture_provenance() -> dict:
    """采集登记时点的数据快照指纹与代码版本（P1-8 审批绑定）。

    - ``data_manifest_sha256``：当前 ``data/manifest.json`` 的文件 sha256 前 16 位。
      与 shadow journal 内嵌、审计 allowed 集合同口径（见 data_fingerprint.py）。
    - ``git_commit``：``git rev-parse HEAD``（不可用时为 None，不编造）。

    任一取不到就只缺哪一项写 None —— 缺失留痕优于伪造值（宁可 FAIL 也不假 PASS）。
    """
    out: dict = {"data_manifest_sha256": None, "git_commit": None}
    man = BASE_DIR / "data" / "manifest.json"
    if man.is_file():
        try:
            # [:16] 口径与 shadow_policy.py 内嵌、audit_project.py allowed 集合三处一致；
            # 不 import shadow_policy 取常量：shadow_policy 反向依赖本模块（L73），会成环。
            out["data_manifest_sha256"] = _file_sha256(man)[:16]
        except OSError:
            pass
    try:
        import subprocess
        r = subprocess.run(["git", "-C", str(BASE_DIR), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            out["git_commit"] = r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def register_model(pkl_path: Path, meta: dict,
                   snapshot_provenance: dict | None = None) -> str | None:
    """把 pkl 登记进注册表，返回 sha256（失败返回 None）。

    meta: {model_version, feature_keys, horizons, flat_margin,
           trained_at, oos_start, n_train, n_oos, oos_metrics?}

    P1-8（2026-09-16）：登记时自动绑定数据快照指纹 + 代码版本，使**新**条目天然可审计；
    历史条目的回填走 ``bind_provenance.py``。

    V4.3.1 ③（2026-09-21，外部复审）：``snapshot_provenance`` 透传训练实际
    消费的冻结件三元组（snapshot_file + samples_sha256_lf + kfp 留档/重算/
    状态）。此前 registry 只记泛化 manifest sha，回答不了"这个模型是基于
    哪份冻结样本+哪套 K 线指纹训练的"。语义（GPT 复审约定）：
    - **照写全，不论 PASS**：G-B 为 UNKNOWN/INCOMPLETE/DRIFTED 时字段同样
      记录——指纹描述"实际消费的是什么"，不是"闸门过了才记"。
    - FRESH / 未提供 ⇒ 三字段诚实为 None：缺失留痕优于伪造值；**绝不**
      拿重算值冒充留档值（活拉没有留档）。
    - 键按白名单过滤（防调用方塞脏键），None 之外的值统一 str 化。
    """
    if not pkl_path.exists():
        return None
    digest = _file_sha256(pkl_path)
    reg = load_registry()
    key = pkl_path.name
    prov = capture_provenance()
    sp = snapshot_provenance or {}
    entry = {
        "path": str(pkl_path),
        "sha256": digest,
        "size_bytes": pkl_path.stat().st_size,
        "registered_at": datetime.now().isoformat(timespec="seconds"),
        "meta": meta,
        "data_manifest_sha256": prov["data_manifest_sha256"],
        "git_commit": prov["git_commit"],
        "snapshot_provenance": {
            "snapshot_file": None,
            "samples_sha256_lf": None,
            "kfp_recorded_sha256": None,
            "kfp_current_sha256": None,
            "kfp_comparability": None,
        },
    }
    for k in ("snapshot_file", "samples_sha256_lf",
              "kfp_recorded_sha256", "kfp_current_sha256", "kfp_comparability"):
        v = sp.get(k)
        if v is not None:
            entry["snapshot_provenance"][k] = str(v)
    reg["models"][key] = entry
    return digest if _save_registry(reg) else None


def read_verified_model_bytes(pkl_path: Path) -> tuple[bytes | None, str]:
    """先读取并校验模型原始字节，成功后才允许调用方反序列化。

    这是 pickle 的安全边界：hash 校验必须发生在 pickle.loads 之前，否则恶意
    ``__reduce__`` 可在“发现 hash 不匹配”之前执行任意代码。
    """
    if not pkl_path.exists():
        return None, "file_missing"
    entry = load_registry()["models"].get(pkl_path.name)
    if entry is None:
        return None, "no_entry"
    try:
        raw = pkl_path.read_bytes()
    except OSError:
        return None, "io_error"
    if hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
        return None, "hash_mismatch"
    return raw, "ok"


def verify_model(pkl_path: Path) -> tuple[bool, str]:
    """校验 pkl 与注册表一致性。返回 (ok, reason)。"""
    raw, reason = read_verified_model_bytes(pkl_path)
    return raw is not None, reason


def verify_validation_report(pkl_name: str) -> tuple[bool, str]:
    """验证 approved 裁决及其报告文件哈希，防止注册表与报告脱钩。"""
    entry = get_model_entry(pkl_name)
    if entry is None:
        return False, "no_entry"
    validation = entry.get("validation")
    if not isinstance(validation, dict):
        return False, "no_validation"
    if validation.get("decision") != "approved":
        return False, "validation_not_approved"
    report_file = validation.get("report_file")
    expected_sha = validation.get("report_sha256")
    if not report_file or not expected_sha:
        return False, "validation_report_unbound"
    report_path = Path(report_file)
    if not report_path.is_absolute():
        report_path = BASE_DIR / report_path
    if not report_path.exists():
        return False, "validation_report_missing"
    try:
        actual_sha = _file_sha256(report_path)
    except OSError:
        return False, "validation_report_io_error"
    if actual_sha != expected_sha:
        return False, "validation_report_hash_mismatch"
    return True, "ok"


def verify_approval(pkl_path: Path, expected_protocol: dict) -> tuple[bool, str]:
    """验证模型是否具备对外展示资格（不包含 config 人工开关）。

    artifact hash、特征协议、validation 报告、promotion 与冻结 provenance 必须
    同时通过（V4.3.1-⑤：``snapshot_provenance`` 五键齐全 = FROZEN 训练件；
    FRESH/历史无此块的条目 → research-only，不得对外展示）；调用方再与
    ``config.forecast.model_ready`` 取 AND，形成最终原子授权。
    """
    ok, reason = verify_model(pkl_path)
    if not ok:
        return False, f"model:{reason}"
    ok, reason = verify_feature_protocol(pkl_path.name, expected_protocol)
    if not ok:
        return False, f"feature_protocol:{reason}"
    ok, reason = verify_validation_report(pkl_path.name)
    if not ok:
        return False, reason
    entry = get_model_entry(pkl_path.name) or {}
    promotion = entry.get("promotion") or {}
    if promotion.get("status") != "approved":
        return False, "promotion_not_approved"
    if derive_promotion(entry.get("validation")).get("status") != "approved":
        return False, "promotion_evidence_inconsistent"
    # V4.3.1-⑤（2026-09-22，外部复审）冻结 provenance 门禁：生产授权要求训练
    # 实际消费的冻结件（FROZEN）五键齐全。FRESH（全 None）/ 历史无此块的
    # 条目 → 拒绝（research-only）。门禁只在本授权入口：--fresh 训练路径
    # 不经过 verify_approval，不受影响；derive_promotion 是五审契约纯函数，
    # 其判据不受本门禁改变。
    sp = entry.get("snapshot_provenance") or {}
    missing = [k for k in ("snapshot_file", "samples_sha256_lf",
                           "kfp_recorded_sha256", "kfp_current_sha256",
                           "kfp_comparability") if sp.get(k) in (None, "")]
    if missing:
        return False, "snapshot_provenance_incomplete:" + ",".join(missing)
    return True, "ok"


# ---------- v6（2026-09-13，Summer 拍板 D2）：Shadow 降级授权旁路 ----------
# 背景：derive_promotion v1「三周期 CI 下界全 > 0」在 n≈900 / 306 日块下对
# T+1/T+3 结构性不可达（单模型 IC 日块 sd≈0.031 → 需 |IC|≳0.065，见
# output/forecast_lab_mde_20260912.md + forecast_lab_review_v2_20260912.md §2.2）。
# 后果是 shadow_policy 每交易日拒记 → 前瞻证据链断裂。
# 降级授权（prereg_shadow_v1）判据预注册于 data/promotion_prereg.json，
# 规则文本见 output/forecast_lab_prereg_rules_20260913.md §二（2026-09-13 落盘）。
#
# 作用域硬边界（不得扩大）：本函数结果**只**供 shadow_policy.py 的纸面记录
# 闸门使用。verify_approval()（对外展示/决策链原子授权）**不读取本函数**、
# 行为不变；config.model_ready 与 registry promotion 字段也均不受影响。

PROMOTION_PREREG_PATH = BASE_DIR / "data" / "promotion_prereg.json"
PROMOTION_DEGRADE_RULE_VERSION = "prereg_shadow_v1"


def evaluate_prereg_degradation(pkl_name: str,
                                prereg_path: Path | None = None,
                                today: str | None = None) -> tuple[bool, str]:
    """评估模型是否获得「Shadow 降级授权」（仅纸面记录，非对外批准）。

    判据（预注册 promotion_shadow_v1，逐条写死、任一不满足即 (False, reason)）：
    D1 登记文件存在且 enabled=true；
    D2 条目中存在 pkl_name 的授权，且 registry 模型 sha256 与登记值逐字节一致；
    D3 registry validation.report_sha256 与登记值一致（报告与模型脱钩即拒）；
    D4 validation.metrics 的 T+5 decision=approved 且 ric_ci 下界 > 0；
    D5 T+1 / T+3 rank_ic ≥ 0（点估计不为负即可，不要求显著）；
    D6 未过期：today（缺省=今天）≤ expiry。
    任何解析失败/字段缺失一律 (False, ...)——降级路径比主路径更保守。

    返回 (ok, reason)。ok=True 时 reason="prereg_degraded"（供记录打标）。
    """
    import datetime as _dt

    path = prereg_path or PROMOTION_PREREG_PATH
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "prereg_file_missing_or_unreadable"
    if not isinstance(doc, dict) or doc.get("enabled") is not True:
        return False, "prereg_not_enabled"
    grants = doc.get("grants")
    if not isinstance(grants, dict) or pkl_name not in grants:
        return False, "prereg_no_grant_for_model"
    grant = grants[pkl_name]
    if not isinstance(grant, dict):
        return False, "prereg_grant_malformed"
    # rule_version 登记在文件顶层（一份文件一套判据）；grant 内可覆盖，但
    # 两处任一存在时必须等于本代码实现的版本，防止「旧判据配新代码」。
    for where, ver in (("file", doc.get("rule_version")),
                       ("grant", grant.get("rule_version"))):
        if ver is not None and ver != PROMOTION_DEGRADE_RULE_VERSION:
            return False, f"prereg_rule_version_mismatch_{where}"

    entry = get_model_entry(pkl_name)
    if entry is None:
        return False, "no_entry"
    if str(entry.get("sha256", "")) != str(grant.get("model_sha256", "")):
        return False, "model_sha_mismatch_vs_prereg"

    validation = entry.get("validation") or {}
    if str(validation.get("report_sha256", "")) != str(grant.get("report_sha256", "")):
        return False, "report_sha_mismatch_vs_prereg"
    metrics = validation.get("metrics") or {}

    # D4：T+5 必须 approved 且 CI 下界 > 0
    m5 = metrics.get("5") if isinstance(metrics.get("5"), dict) else None
    if m5 is None or m5.get("decision") != "approved":
        return False, "t5_not_approved"
    try:
        lo5 = float(m5["ric_ci"][0])
    except (KeyError, TypeError, ValueError, IndexError):
        return False, "t5_ci_missing"
    if not lo5 > 0.0:
        return False, "t5_ci_lower_bound_not_positive"

    # D5：T+1 / T+3 点估计不为负（不要求显著——这正是与 v1 的差别）
    for h in ("1", "3"):
        mh = metrics.get(h) if isinstance(metrics.get(h), dict) else None
        if mh is None:
            return False, f"t{h}_metrics_missing"
        try:
            ic = float(mh["rank_ic"])
        except (KeyError, TypeError, ValueError):
            return False, f"t{h}_rank_ic_missing"
        if not ic >= 0.0:
            return False, f"t{h}_rank_ic_negative"

    # D6：有效期
    expiry = str(grant.get("expiry", ""))
    try:
        exp_d = _dt.date.fromisoformat(expiry)
    except ValueError:
        return False, "prereg_expiry_unparsable"
    today_d = (_dt.date.fromisoformat(today) if today else _dt.date.today())
    if today_d > exp_d:
        return False, "prereg_expired"
    return True, "prereg_degraded"


def registry_summary() -> dict:
    """当前注册表摘要（供报告/诊断用）。"""
    reg = load_registry()
    out = {}
    for name, entry in reg["models"].items():
        meta = entry.get("meta", {})
        proto = entry.get("feature_protocol") or {}
        out[name] = {
            "sha256": entry.get("sha256", "")[:16] + "…",
            "trained_at": meta.get("trained_at"),
            "oos_start": meta.get("oos_start"),
            "n_train": meta.get("n_train"),
            "feature_dim": proto.get("feature_dim"),
            "registered_at": entry.get("registered_at"),
        }
    return out



# ---------- v2（2026-08-31，GPT 四审 P1）：validation/promotion 审计链 ----------
def bind_validation(pkl_name: str, report_file: str, report_sha256: str,
                    decision: str, metrics: dict | None = None,
                    auto_promotion: bool = True) -> bool:
    """把「这份 pkl 对应哪次验证」绑进注册表（密码学绑定报告哈希）。

    pkl_name: registry 键（文件名，如 forecast_v2.pkl）
    report_file: 相对 BASE_DIR 的验证报告路径（如 output/backtest_forecast_v8_xxx.log）
    report_sha256: 该报告的 sha256（防报告事后被替换）
    decision: "approved" / "rejected"（该验证对 model_ready 的裁决）
    metrics: {horizon: {rank_ic, ric_ci, oos_brier, ..., decision}} 关键指标快照
             （每周期须含 decision 字段，供 promotion 纯函数核验）
    auto_promotion: 绑定成功后自动用 derive_promotion 纯函数推导并写入
             promotion（P3，2026-09-01）——正常路径不再手填 update_promotion。
    """
    reg = load_registry()
    entry = reg["models"].get(pkl_name)
    if entry is None:
        return False
    entry["validation"] = {
        "report_file": report_file,
        "report_sha256": report_sha256,
        "decision": decision,
        "bound_at": datetime.now().isoformat(timespec="seconds"),
    }
    if metrics:
        entry["validation"]["metrics"] = metrics
    saved = _save_registry(reg)
    if saved and auto_promotion:
        # P3：promotion 由纯函数从本绑定推导（同一证据 → 同一结论）
        apply_promotion(pkl_name)
    return saved


def update_promotion(pkl_name: str, status: str, reason: str) -> bool:
    """手写 promotion（低层接口，仅限人工覆写/应急）。

    ⚠️ P3（2026-09-01）后正常路径是 bind_validation(auto_promotion=True)
    自动推导；本接口保留用于人工复核后的例外覆写，覆写会在 derived_by
    字段缺失上与纯函数产物可区分（审计可辨）。
    """
    reg = load_registry()
    entry = reg["models"].get(pkl_name)
    if entry is None:
        return False
    entry["promotion"] = {
        "status": status,
        "reason": reason,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    return _save_registry(reg)


def get_model_entry(pkl_name: str) -> dict | None:
    """按文件名取条目（含 validation/promotion）。"""
    return load_registry()["models"].get(pkl_name)


# ---------- v4（2026-09-01，GPT 五审 P3）：promotion 纯函数化 ----------
# 五审指出 promotion 一直是人工手填 status/reason（quantv1_a4_bind.py），
# 存在与 validation 绑定证据不一致的风险。现把裁决规则固化为纯函数：
# 只读 validation 绑定（decision + per-horizon metrics），输出确定性的
# promotion 状态与理由——同一证据永远推出同一结论，可单测、可审计。
# 人工仍保留的唯一动作：验证全过后把 config.forecast.model_ready 置 true
# （措辞红线复核）；registry promotion 本身不再需要手填。

PROMOTION_RULE_VERSION = 1


def derive_promotion(validation: dict | None) -> dict:
    """从 validation 绑定纯函数推导 promotion（预注册规则 v1，禁改判据）。

    规则（对齐 backtest_forecast「三周期全过」总口径）：
    R1 无 validation / decision 非法 → pending（无依据不强判，不动现有 promotion）
    R2 decision=approved 但缺 per-horizon metrics → pending（无法核验三周期）
    R3 decision=rejected 或任一周期 metrics.decision != approved → blocked
    R4 decision=approved 且全部周期 approved → approved
       （approved 仅代表证据合格；config.forecast.model_ready 仍人工置位）

    返回 {status, reason, rule_version, failed_horizons}。纯函数：不读盘、
    不写盘、不依赖时间——同输入必同输出。
    """
    if not isinstance(validation, dict) or validation.get("decision") not in (
            "approved", "rejected"):
        return {"status": "pending", "rule_version": PROMOTION_RULE_VERSION,
                "failed_horizons": [],
                "reason": "无有效 validation 绑定（bind_validation）——promotion 无依据，保持 pending"}
    decision = validation["decision"]
    metrics = validation.get("metrics") or {}
    if decision == "approved" and not metrics:
        return {"status": "pending", "rule_version": PROMOTION_RULE_VERSION,
                "failed_horizons": [],
                "reason": "validation.decision=approved 但缺 per-horizon metrics 快照，无法核验三周期，保持 pending"}

    def _hkey(h) -> float:
        try:
            return float(h)
        except (TypeError, ValueError):
            return float("inf")

    def _fmt_ci(ci) -> str:
        try:
            lo, hi = float(ci[0]), float(ci[1])
        except (TypeError, ValueError, IndexError):
            return "CI 缺失"
        return f"CI [{lo:+.3f},{hi:+.3f}]" + ("（跨零）" if lo <= 0 else "")

    failed, passed = [], []
    for h in sorted(metrics, key=_hkey):
        m = metrics[h] if isinstance(metrics[h], dict) else {}
        ok_h = m.get("decision") == "approved"
        line = f"T+{h} {'通过' if ok_h else '未通过'}（{_fmt_ci(m.get('ric_ci'))}）"
        (passed if ok_h else failed).append(line)

    if decision == "rejected" or failed:
        if failed:
            detail = "；".join(failed + passed)
            reason = f"验证未全过（需三周期全过）：{detail}——model_ready 维持 false"
        else:
            reason = ("整体裁决 rejected（周期明细均 approved，绑定不一致，以整体为准）"
                      "——model_ready 维持 false")
        return {"status": "blocked", "rule_version": PROMOTION_RULE_VERSION,
                "failed_horizons": [h for h in sorted(metrics, key=_hkey)
                                     if not isinstance(metrics[h], dict)
                                     or metrics[h].get("decision") != "approved"],
                "reason": reason}
    return {"status": "approved", "rule_version": PROMOTION_RULE_VERSION,
            "failed_horizons": [],
            "reason": "三周期全过（预注册口径）——证据合格；config.forecast.model_ready 仍需人工复核措辞红线后置 true"}


def apply_promotion(pkl_name: str, dry_run: bool = False) -> tuple[bool, dict]:
    """读取条目 validation，用 derive_promotion 推导并写入 promotion。

    返回 (written, derived)。written=False 的情形：
    - 条目不存在（derived.status="unknown"）
    - 无 validation 依据（保持现有 promotion 不动，不强写 pending）
    - dry_run=True（只推导不落盘，供审计核对历史手填一致性）
    """
    reg = load_registry()
    entry = reg["models"].get(pkl_name)
    if entry is None:
        return False, {"status": "unknown", "rule_version": PROMOTION_RULE_VERSION,
                       "failed_horizons": [], "reason": "no_entry"}
    derived = derive_promotion(entry.get("validation"))
    if entry.get("validation") is None:
        return False, derived
    if dry_run:
        return False, derived
    entry["promotion"] = {
        "status": derived["status"],
        "reason": derived["reason"],
        "rule_version": derived["rule_version"],
        "derived_by": "derive_promotion",   # 纯函数产物标记（区别于历史手填）
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    return _save_registry(reg), derived


# ---------- v3（2026-09-01，GPT 五审）：feature_protocol 特征协议 ----------
# GPT 五审指出：registry 只记 feature_keys（逻辑 7 键），而 B1 缺失掩码协议
# （2026-08-31，MODEL_VERSION 3）后模型实际消费 (值, missing_mask) 双列 = 14 维
# ——registry 与真实输入维度脱节。feature_protocol 显式登记：逻辑特征键 +
# 实际消费维度 + 缺失掩码布局，与引擎 FEATURE_KEYS/B1 实现对齐，单一事实来源。

FEATURE_PROTOCOL_VERSION = 1

# B1 缺失掩码布局（与 forecast_engine.MASKING_PROTOCOL 同构，两处须同步维护）：
#   每特征输出 (值, missing_mask) 双列 → 7 逻辑特征 = 14 实际维
B1_MASKING_PROTOCOL = {
    "enabled": True,
    "layout": "value_then_mask",   # [v0, m0, v1, m1, ...]
    "missing_value": 0.0,
    "mask_value": 1.0,
}


def make_feature_protocol(feature_keys: list[str],
                          masking: dict | None = None) -> dict:
    """构造特征协议（登记/校验共用）。

    masking=None → 纯值布局（v2 及更早，feature_dim = n_features）
    masking=B1_MASKING_PROTOCOL → 值+掩码双列（v3+，feature_dim = n_features × 2）
    """
    n = len(feature_keys)
    feature_dim = n * 2 if (masking or {}).get("enabled") else n
    return {
        "protocol_version": FEATURE_PROTOCOL_VERSION,
        "feature_keys": list(feature_keys),
        "n_features": n,
        "feature_dim": feature_dim,
        "masking": masking,
    }


def bind_feature_protocol(pkl_name: str, protocol: dict) -> bool:
    """把特征协议绑进注册表条目。未知模型返回 False（不新增幽灵条目）。"""
    reg = load_registry()
    entry = reg["models"].get(pkl_name)
    if entry is None:
        return False
    entry["feature_protocol"] = protocol
    return _save_registry(reg)


def verify_feature_protocol(pkl_name: str, expected: dict) -> tuple[bool, str]:
    """校验登记协议与引擎当前协议一致。返回 (ok, reason)。

    ok=False 的 reason ∈ {"no_entry", "no_protocol", "feature_keys_mismatch",
    "feature_dim_mismatch", "masking_mismatch"}。
    """
    reg = load_registry()
    entry = reg["models"].get(pkl_name)
    if entry is None:
        return False, "no_entry"
    proto = entry.get("feature_protocol")
    if proto is None:
        return False, "no_protocol"
    if list(proto.get("feature_keys", [])) != list(expected.get("feature_keys", [])):
        return False, "feature_keys_mismatch"
    if int(proto.get("feature_dim", -1)) != int(expected.get("feature_dim", -2)):
        return False, "feature_dim_mismatch"
    if bool((proto.get("masking") or {}).get("enabled")) != bool(
            (expected.get("masking") or {}).get("enabled")):
        return False, "masking_mismatch"
    return True, "ok"
