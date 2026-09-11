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


def register_model(pkl_path: Path, meta: dict) -> str | None:
    """把 pkl 登记进注册表，返回 sha256（失败返回 None）。

    meta: {model_version, feature_keys, horizons, flat_margin,
           trained_at, oos_start, n_train, n_oos, oos_metrics?}
    """
    if not pkl_path.exists():
        return None
    digest = _file_sha256(pkl_path)
    reg = load_registry()
    key = pkl_path.name
    entry = {
        "path": str(pkl_path),
        "sha256": digest,
        "size_bytes": pkl_path.stat().st_size,
        "registered_at": datetime.now().isoformat(timespec="seconds"),
        "meta": meta,
    }
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

    artifact hash、特征协议、validation 报告与 promotion 必须同时通过；调用方
    再与 ``config.forecast.model_ready`` 取 AND，形成最终原子授权。
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
    return True, "ok"


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
