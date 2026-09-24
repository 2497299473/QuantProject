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
PKL_DIR = BASE_DIR / "data" / "models"
"""权重 pkl 的落盘目录（注意与 ``MODELS_DIR`` 区分：后者是**注册表**目录）。

命名历史遗留：``MODELS_DIR`` 指的是 ``data/model_registry/``（本模块的 registry.json
所在地），而权重在 ``data/models/``（``forecast_engine.MODELS_DIR``）。V4.5 归一化
需要按条目名回推权重路径，故显式声明，避免再拿 ``MODELS_DIR`` 去拼 pkl 路径。
"""


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tracked_path(pkl_path: Path) -> str:
    """pkl → 仓库相对路径（**正斜杠**），仓库根外或无法相对化时退回文件名。

    V4.5（2026-09-23）：``entry["path"]`` 的单一来源。旧实况里该字段是
    ``/home/summer/QuantV1/data/models/forecast_v2.pkl`` —— WSL 时代写入的绝对路径，
    Windows 迁移后**永远指不到真实文件**（消费点只能靠 ``Path(...).name`` 兜底，
    而路径字段本身已经不可判读）。改存相对路径：跨机器可读、无本地绝对路径泄漏、
    与 ``provenance.git_tracked_path`` 同口径（一处定义，两处引用）。

    不写绝对路径的另一理由：``output/run_manifest/`` 与 registry 均随仓库跟踪，
    落盘本地绝对路径既不可跨环境判读，也把运行账户名带进仓库。
    """
    try:
        return pkl_path.resolve().relative_to(BASE_DIR.resolve()).as_posix()
    except (ValueError, OSError):
        return pkl_path.name


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


def _is_absolute_path_str(p: str) -> bool:
    """Unix 绝对路径（``/`` 开头）或 Windows 绝对路径（盘符）。"""
    return bool(p) and (p.startswith("/") or (len(p) > 1 and p[1] == ":"))


def normalize_registry_paths(dry_run: bool = False) -> tuple[bool, list[str]]:
    """把 registry 里的旧绝对路径就地归一为仓库相对路径（V4.5，2026-09-23）。

    历史遗留：两个条目都存着 WSL 时代绝对路径（``/home/summer/QuantV1/...``），
    Windows 迁移后不可判读，却因消费点用 ``Path(...).name`` 兜底而**静默通过**
    （审计 P1-10 现把它显式化）。本函数按 ``tracked_path`` 的单一来源重写 path——
    **只动路径措辞，不动 sha256 / validation / promotion / provenance 等任何证据
    字段**；registry 的其余字节保持原样。

    返回 ``(changed, details)``；``dry_run=True`` 只报告不落盘。
    """
    reg = load_registry()
    details: list[str] = []
    for name, entry in (reg.get("models") or {}).items():
        raw = str(entry.get("path") or "")
        if not _is_absolute_path_str(raw):
            continue
        new = tracked_path(PKL_DIR / name)
        details.append(f"{name}: {raw} → {new}")
        entry["path"] = new
    if not details or dry_run:
        return bool(details), details
    return _save_registry(reg), details


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
        "path": tracked_path(pkl_path),
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
    # 历史条目的旧绝对路径在此就地归一（V4.5，2026-09-23）：下次登记该 pkl 时
    # 自动把 WSL 时代绝对路径改成仓库相对路径，无需单独跑迁移脚本。
    for old_name, old_entry in reg["models"].items():
        if _is_absolute_path_str(str(old_entry.get("path") or "")):
            old_entry["path"] = tracked_path(PKL_DIR / old_name)
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





def validate_validation_provenance(pkl_name: str, provenance: dict | None) -> tuple[bool, str]:
    """核对已从验证报告内容解析出的 provenance 与 registry 血统（A-final-1）。

    注意：这里不再接受“调用者声明”作为 bind_validation 的输入。
    唯一上游是 _read_validation_report() 从 report_file 实际读取出的
    PROVENANCE_JSON 行；本函数只负责将其与 registry 当前模型条目逐项比对。
    """
    if not isinstance(provenance, dict):
        return False, "validation_provenance_missing"
    required = ("artifact_sha256", "dataset_sha256", "git_commit")
    missing = [k for k in required if not str(provenance.get(k) or "").strip()]
    if missing:
        return False, "validation_provenance_missing:" + ",".join(missing)
    entry = get_model_entry(pkl_name)
    if entry is None:
        return False, "no_entry"
    artifact_sha = str(entry.get("sha256") or "").strip()
    snapshot = entry.get("snapshot_provenance") or {}
    dataset_sha = str(snapshot.get("samples_sha256_lf") or "").strip()
    train_commit = str(entry.get("git_commit") or "").strip()
    if not artifact_sha:
        return False, "registry_artifact_sha_missing"
    if not dataset_sha:
        return False, "registry_dataset_sha_missing"
    if not train_commit:
        return False, "registry_git_commit_missing"
    checks = (("artifact_sha256", str(provenance["artifact_sha256"]).strip(), artifact_sha),
              ("dataset_sha256", str(provenance["dataset_sha256"]).strip(), dataset_sha),
              ("git_commit", str(provenance["git_commit"]).strip(), train_commit))
    for key, declared, expected in checks:
        if declared != expected:
            return False, f"validation_provenance_{key}_mismatch"
    return True, "ok"


def _resolve_validation_report_path(report_file: str) -> Path | None:
    """把报告路径解析到仓库根；拒绝空路径。"""
    if not isinstance(report_file, str) or not report_file.strip():
        return None
    path = Path(report_file)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def _read_validation_report(report_file: str) -> tuple[Path | None, str | None, dict | None]:
    """读取实际验证报告、现场计算 SHA256，并解析唯一 provenance 行。

    报告必须包含恰一行 PROVENANCE_JSON={...}，JSON 至少提供
    artifact_sha256 / dataset_sha256 / git_commit。任何读取、编码或 JSON
    解析异常均 fail-closed。
    """
    path = _resolve_validation_report_path(report_file)
    if path is None:
        return None, None, None
    try:
        raw = path.read_bytes()
        actual_sha = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return path, None, None
    prefix = "PROVENANCE_JSON="
    payloads = [line[len(prefix):].strip()
                for line in text.splitlines() if line.startswith(prefix)]
    if len(payloads) != 1:
        return path, actual_sha, None
    try:
        provenance = json.loads(payloads[0])
    except (json.JSONDecodeError, TypeError):
        return path, actual_sha, None
    if not isinstance(provenance, dict):
        return path, actual_sha, None
    return path, actual_sha, provenance


# ---------- v2（2026-08-31，GPT 四审 P1）：validation/promotion 审计链 ----------
def bind_validation(pkl_name: str, report_file: str, decision: str,
                    metrics: dict | None = None,
                    auto_promotion: bool = True) -> bool:
    """把实际验证报告绑定进 registry。

    A-final-1 收口：
    - report_file 必须真实存在且可 UTF-8 读取；
    - report SHA256 在 bind 时现场重算，调用者不再传 report_sha256；
    - provenance 从报告唯一的 PROVENANCE_JSON=... 行解析，调用者不再传
      provenance 字典；
    - artifact/dataset/git 三项必须分别等于 registry 当前条目的模型 sha /
      冻结样本 sha / 训练代码 commit；
    - 任一缺失、不一致或重复 provenance 行均 fail-closed。
    """
    reg = load_registry()
    entry = reg["models"].get(pkl_name)
    if entry is None:
        return False

    report_path, actual_sha, provenance = _read_validation_report(report_file)
    if report_path is None or actual_sha is None or provenance is None:
        return False

    ok_prov, _ = validate_validation_provenance(pkl_name, provenance)
    if not ok_prov:
        return False

    entry["validation"] = {
        "report_file": report_file,
        "report_sha256": actual_sha,
        "decision": decision,
        "bound_at": datetime.now().isoformat(timespec="seconds"),
        "provenance": {
            "artifact_sha256": str(provenance["artifact_sha256"]).strip(),
            "dataset_sha256": str(provenance["dataset_sha256"]).strip(),
            "git_commit": str(provenance["git_commit"]).strip(),
        },
    }
    if metrics:
        entry["validation"]["metrics"] = metrics
    saved = _save_registry(reg)
    if saved and auto_promotion:
        apply_promotion(pkl_name)
    return saved

# ---------- v2（2026-08-31，GPT 四审 P1）：validation/promotion 审计链 ----------
def bind_validation(pkl_name: str, report_file: str, report_sha256: str,
                    decision: str, metrics: dict | None = None,
                    auto_promotion: bool = True,
                    provenance: dict | None = None) -> bool:
    """把「这份 pkl 对应哪次验证」绑进注册表（密码学绑定报告哈希）。

    pkl_name: registry 键（文件名，如 forecast_v2.pkl）
    report_file: 相对 BASE_DIR 的验证报告路径（如 output/backtest_forecast_v8_xxx.log）
    report_sha256: 该报告的 sha256（防报告事后被替换）
    decision: "approved" / "rejected"（该验证对 model_ready 的裁决）
    metrics: {horizon: {rank_ic, ric_ci, oos_brier, ..., decision}} 关键指标快照
             （每周期须含 decision 字段，供 promotion 纯函数核验）
    auto_promotion: 绑定成功后自动用 derive_promotion 纯函数推导并写入
             promotion（P3，2026-09-01）——正常路径不再手填 update_promotion。
    provenance: 报告侧声明的三项血统字段 artifact_sha256 / dataset_sha256 / git_commit。
             三项必须与 registry 当前条目的模型字节、冻结样本 sha、训练代码锚点
             逐项相等；缺失或不一致直接拒绝绑定。
    """
    reg = load_registry()
    entry = reg["models"].get(pkl_name)
    if entry is None:
        return False

    ok_prov, _ = validate_validation_provenance(pkl_name, provenance)
    if not ok_prov:
        return False

    entry["validation"] = {
        "report_file": report_file,
        "report_sha256": report_sha256,
        "decision": decision,
        "bound_at": datetime.now().isoformat(timespec="seconds"),
        "provenance": {
            "artifact_sha256": str(provenance["artifact_sha256"]).strip(),
            "dataset_sha256": str(provenance["dataset_sha256"]).strip(),
            "git_commit": str(provenance["git_commit"]).strip(),
        },
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

PROMOTION_RULE_VERSION = 2

# B++-3：pooled 校准门阈值——与验证器既有冻结判据同源（backtest_forecast.py
# ok 裁决的 `calib["ace"] < 0.25`；B++-1 起该口径在 schema v2 中具名为
# midpoint_calibration_error）。
_PROMOTION_CALIBRATION_MAX = 0.25


def _derive_promotion_v2_gates(evidence: dict, vs) -> dict:
    """schema v2 证据 → rule v2 五门顺序裁决（契约 B §4/§7/§11）。

    门序：power → performance → baseline_edge → calibration → provenance。
    「不可核验」与「不满足」同罪（fail-closed），各落到对应 blocked_*；
    五门全过 → approved。状态词为小写机读形——verify_approval 对
    "approved" 的字面比较零改动继承（B++-4）。
    """

    def _bad(status: str, reason: str) -> dict:
        return {"status": status, "rule_version": PROMOTION_RULE_VERSION,
                "failed_horizons": [], "reason": reason}

    ok_ev, errs_ev = vs.validate_evidence(evidence)
    if not ok_ev:
        return _bad("blocked_provenance",
                    f"证据 schema v2 自检未过（fail-closed）：{errs_ev[0]}")

    # ---- 门 1 power（契约 B §6：小样本不得被迫二元结论）----
    pw = evidence["power"]
    if pw["frozen"]["value"] is not True:
        return _bad("blocked_power",
                    "功效阈值未预注册冻结（power.frozen=False）——契约 B §6.2：须先经"
                    "功效分析冻结 N_POWER_*，APPROVED 在此之前结构性不可达")
    n_pf = pw["n_power_fund"]["value"]
    if not isinstance(n_pf, (int, float)) or isinstance(n_pf, bool) or n_pf <= 0:
        return _bad("blocked_power",
                    f"power.n_power_fund 不可用（{n_pf!r}）——无功效阈值无法判定基金样本充分性")
    for h in vs.HORIZONS:
        if evidence["pooled"][h]["decision_edge"]["status"] == vs.STATUS_INSUFFICIENT_POWER:
            return _bad("blocked_power", f"pooled T+{h} 样本不足（INSUFFICIENT_POWER）")
    for code, fund in evidence["funds"].items():
        for h, node in fund.items():
            if node["decision_edge"]["status"] == vs.STATUS_INSUFFICIENT_POWER:
                return _bad("blocked_power",
                            f"{code} T+{h} 样本不足（INSUFFICIENT_POWER）——契约 B §6.1："
                            "不得解释为 PASS（如 025687 冻结件 n=35）")
            n = node["n"]["value"]
            if node["n"]["status"] == vs.STATUS_OK and n < n_pf:
                return _bad("blocked_power", f"{code} T+{h} n={n} < N_POWER_FUND={n_pf}")

    # ---- 门 2 performance（pooled 绝对能力，对齐 v1「三周期全过」内涵）----
    for h in vs.HORIZONS:
        node = evidence["pooled"][h]
        ric, ric_ci = node["rank_ic"], node["rank_ic_ci"]
        if ric["status"] != vs.STATUS_OK or ric_ci["status"] != vs.STATUS_OK:
            return _bad("blocked_performance",
                        f"pooled T+{h} RankIC/CI 不可核验"
                        f"（{ric['status']}/{ric_ci['status']}）")
        if ric_ci["value"][0] <= 0:
            return _bad("blocked_performance",
                        f"pooled T+{h} RankIC CI 下界 {ric_ci['value'][0]} ≤ 0")
        br, bmaj = node["brier"], node["b_majority"]
        if br["status"] != vs.STATUS_OK or bmaj["status"] != vs.STATUS_OK:
            return _bad("blocked_performance", f"pooled T+{h} Brier/多数类不可核验")
        if br["value"] > max(bmaj["value"], 2.0 / 3.0):
            return _bad("blocked_performance",
                        f"pooled T+{h} Brier {br['value']} 劣于基线梯队"
                        f"（多数类 {bmaj['value']}）")

    # ---- 门 3 baseline edge（pooled + 逐基金；契约 B §3/§7 F2/F3）----
    for h in vs.HORIZONS:
        e = evidence["pooled"][h]["decision_edge"]
        ci = evidence["pooled"][h]["decision_edge_ci"]
        if e["status"] != vs.STATUS_OK or ci["status"] != vs.STATUS_OK:
            return _bad("blocked_baseline_edge",
                        f"pooled T+{h} decision_edge/CI 不可核验"
                        f"（{e['status']}/{ci['status']}）")
        if ci["value"][0] <= 0:
            return _bad("blocked_baseline_edge",
                        f"pooled T+{h} decision_edge CI 下界 {ci['value'][0]} ≤ 0——"
                        "相对 est_chg 的增量排序信息不成立")
    for code, fund in evidence["funds"].items():
        for h, node in fund.items():
            e, ci = node["decision_edge"], node["decision_edge_ci"]
            if e["status"] != vs.STATUS_OK or ci["status"] != vs.STATUS_OK:
                return _bad("blocked_baseline_edge",
                            f"{code} T+{h} decision_edge/CI 不可核验"
                            f"（{e['status']}/{ci['status']}）")
            if ci["value"][0] <= 0:
                return _bad("blocked_baseline_edge",
                            f"{code} T+{h} decision_edge CI 下界 {ci['value'][0]} ≤ 0——"
                            "基金级增量不成立（pooled 过不覆盖基金，契约 B §7/F3）")

    # ---- 门 4 calibration ----
    for h in vs.HORIZONS:
        mce = evidence["pooled"][h][vs.MIDPOINT_CALIBRATION_ERROR_KEY]
        if mce["status"] != vs.STATUS_OK or mce["value"] >= _PROMOTION_CALIBRATION_MAX:
            return _bad("blocked_calibration",
                        f"pooled T+{h} midpoint_calibration_error 不可核验或"
                        f" ≥{_PROMOTION_CALIBRATION_MAX}（got {mce['value']}）")

    # ---- 门 5 provenance ----
    for k, slot in evidence["provenance"].items():
        if slot["status"] != vs.STATUS_OK:
            return _bad("blocked_provenance",
                        f"provenance.{k} 不可核验（{slot['status']}）——证据链不完整")

    return {"status": "approved", "rule_version": PROMOTION_RULE_VERSION,
            "failed_horizons": [],
            "reason": "schema v2 证据五门全过（power/performance/baseline_edge/"
                      "calibration/provenance）——证据合格；config.forecast.model_ready "
                      "仍需人工复核措辞红线后置 true"}


def derive_promotion(validation: dict | None) -> dict:
    """从 validation 绑定纯函数推导 promotion（预注册规则 v2，B++-3，禁改判据）。

    签名不变（契约 B §12）：同一 evidence 只能推出同一 promotion；人工不得
    手写 promotion=approved，唯一人工开关仍是 config.forecast.model_ready=true。

    规则：
    R0 无 validation / decision 非法 → pending（无依据不强判，不动现有 promotion）。
    R1 validation.evidence 为 schema v2（B++-2 验证器产出）→ 五门顺序裁决
       （_derive_promotion_v2_gates）；decision=rejected → blocked。
    R2 legacy 绑定（无 schema v2 证据块）保留 v1 一致性核验：decision=approved
       但缺 per-horizon metrics → pending；decision=rejected 或任一周期
       metrics.decision != approved → blocked（failed_horizons 列未过周期）。
    R3 legacy 三周期全过不再 approved：pooled-only 证据无法核验
       fund/power/calibration/provenance 门（契约 B §4）→ research_only；
       升级路径 = 用 B++-2 验证器重新产出 schema v2 证据。

    返回 {status, reason, rule_version, failed_horizons}。纯函数：不读盘、
    不写盘、不依赖时间——同输入必同输出。
    """
    from core import validation_schema as _vs   # 局部 import（同 pit1455 委托惯例，防环）
    if not isinstance(validation, dict) or validation.get("decision") not in (
            "approved", "rejected"):
        return {"status": "pending", "rule_version": PROMOTION_RULE_VERSION,
                "failed_horizons": [],
                "reason": "无有效 validation 绑定（bind_validation）——promotion 无依据，保持 pending"}
    decision = validation["decision"]

    # ---- rule v2 主路径：validation.evidence 为 schema v2（B++-2 验证器产出）----
    evidence = validation.get("evidence")
    if isinstance(evidence, dict) and evidence.get(
            "schema_version") == _vs.VALIDATION_SCHEMA_VERSION:
        if decision == "rejected":
            return {"status": "blocked", "rule_version": PROMOTION_RULE_VERSION,
                    "failed_horizons": [],
                    "reason": "整体裁决 rejected（schema v2 证据）——model_ready 维持 false"}
        return _derive_promotion_v2_gates(evidence, _vs)

    # ---- legacy 兼容路径（无 schema v2 证据块）----
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
    # rule v2（B++-3）：v1 的 approved 出口关闭——legacy 绑定缺 fund×horizon /
    # power / calibration / provenance 证据块，无法核验五门（契约 B §4），
    # pooled-only 不再构成生产授权依据。
    return {"status": "research_only", "rule_version": PROMOTION_RULE_VERSION,
            "failed_horizons": [],
            "reason": ("legacy 绑定三周期全过仅证明 pooled 能力——rule v2 要求 schema v2 "
                       "证据过五门（fund×horizon/power/calibration/provenance），"
                       "pooled-only 不再构成生产授权依据（契约 B §4）；"
                       "升级须重新产出 schema v2 证据（B++-2 验证器）")}


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
    """校验登记协议与引擎当前协议逐字段严格一致（B 契约 §15-B2，2026-09-23）。

    校验范围：protocol_version / feature_keys / n_features / feature_dim /
    masking 整块（enabled+layout+missing_value+mask_value）exact 比较。
    旧实现只比 keys/dim/enabled——布局翻转（value_then_mask → mask_then_value）
    或填充值漂移在维度不变时无法被发现，属于「契约在、语义已漂」。

    ok=False 的 reason ∈ {"no_entry", "no_protocol", "protocol_version_mismatch",
    "feature_keys_mismatch", "n_features_mismatch", "feature_dim_mismatch",
    "masking_mismatch"}。
    """
    reg = load_registry()
    entry = reg["models"].get(pkl_name)
    if entry is None:
        return False, "no_entry"
    proto = entry.get("feature_protocol")
    if proto is None:
        return False, "no_protocol"
    if int(proto.get("protocol_version", -1)) != int(expected.get("protocol_version", -2)):
        return False, "protocol_version_mismatch"
    if list(proto.get("feature_keys", [])) != list(expected.get("feature_keys", [])):
        return False, "feature_keys_mismatch"
    if int(proto.get("n_features", -1)) != int(expected.get("n_features", -2)):
        return False, "n_features_mismatch"
    if int(proto.get("feature_dim", -1)) != int(expected.get("feature_dim", -2)):
        return False, "feature_dim_mismatch"
    if proto.get("masking") != expected.get("masking"):
        return False, "masking_mismatch"
    return True, "ok"
