#!/usr/bin/env python3
"""Explicit v2 validation-evidence binding entrypoint (F3, 2026-09-24).

生产绑定只允许从「report .log + schema v2 evidence.json」进入 registry。
脚本负责输入校验与血统对账，最终写入统一走 core.model_registry.bind_validation()；
不在验证器主流程尾部做 registry I/O，不修改 stdout 复现纪律。

D3-01（2026-09-29 面 3 审查）新增两道门，位于 bind 之前：
  门 A（pairing proof）：report 的 PROVENANCE_JSON 必须携带 evidence_sha256
      锚点，且与提交的 evidence 文件内容现场重算值逐位相等（bind_validation
      内部会再执一次，此处前置给出可读拒绝原因）。
  门 B（computation proof）：对 evidence 声称的 frozen 件 + registry artifact
      字节**独立重算** pooled 指标（含 CI，门 2/门 3 的实际判据），与声明值
      逐位比对；不符 / 不可复算 ⇒ 拒绑。实现在 evidence_recompute.py
      （零网络：只读 forecast_outputs/ 冻结件与 data/models/ 字节）。
      重依赖（numpy/scipy/sklearn）只在本 CLI 入口与 evidence_recompute
      模块，不进 core.model_registry（D-04 import 代价裁决保持）。

用法：
  python bind_validation_evidence.py --report output/xxx.log \
      --evidence output/validation_evidence/xxx.json [--model forecast_v3.pkl]

未显式给 --model 时，用 evidence.provenance.artifact_sha256 在 registry 中唯一反查；
零个或多个匹配都 fail-closed，避免凭文件名误绑。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import model_registry, validation_schema  # noqa: E402

ANCHOR_KEY = "evidence_sha256"   # 与 model_registry._EVIDENCE_ANCHOR_KEY 同义


def _read_json(path_text: str) -> dict | None:
    path = Path(path_text)
    if not path.is_absolute():
        path = BASE_DIR / path
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _ev_value(evidence: dict, key: str):
    slot = (evidence.get("provenance") or {}).get(key)
    if not isinstance(slot, dict) or slot.get("status") != validation_schema.STATUS_OK:
        return None
    return slot.get("value")


def _find_model(model_name: str | None, artifact_sha: str) -> str | None:
    reg = model_registry.load_registry()
    models = reg.get("models") or {}
    if model_name:
        if model_name not in models:
            return None
        return model_name
    matches = [
        name for name, entry in models.items()
        if str((entry or {}).get("sha256") or "") == artifact_sha
    ]
    return matches[0] if len(matches) == 1 else None


def _check_evidence_against_registry(model_name: str, evidence: dict) -> tuple[bool, str]:
    entry = model_registry.get_model_entry(model_name)
    if entry is None:
        return False, "no_registry_entry"
    checks = {
        "artifact_sha256": str(entry.get("sha256") or ""),
        "dataset_sha256": str((entry.get("snapshot_provenance") or {}).get(
            "samples_sha256_lf") or ""),
        "git_commit": str(entry.get("git_commit") or ""),
    }
    for key, expected in checks.items():
        declared = str(_ev_value(evidence, key) or "")
        if not declared or not expected:
            return False, f"{key}_missing"
        if declared != expected:
            return False, f"{key}_mismatch"
    return True, "ok"


def _report_provenance(report_path_text: str) -> dict | None:
    """从 report 实际内容解析唯一 PROVENANCE_JSON 行（不信任调用者声明）。"""
    _p, _sha, prov = model_registry._read_validation_report(report_path_text)
    return prov if isinstance(prov, dict) else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True, help="验证报告 .log 路径")
    ap.add_argument("--evidence", required=True, help="schema v2 evidence JSON 路径")
    ap.add_argument("--model", default=None, help="registry 中的 artifact 文件名；缺省按 artifact SHA 唯一反查")
    args = ap.parse_args()

    evidence = _read_json(args.evidence)
    if evidence is None:
        print("[fail] evidence JSON 不可读或不是 object")
        return 2
    ok_ev, errs = validation_schema.validate_evidence(evidence)
    if not ok_ev:
        print(f"[fail] evidence schema v2 校验失败：{errs[:5]}")
        return 2
    if evidence.get("schema_version") != validation_schema.VALIDATION_SCHEMA_VERSION:
        print("[fail] 只允许 schema v2 evidence")
        return 2

    decision = evidence.get("decision")
    if decision not in ("approved", "rejected"):
        print(f"[fail] evidence.decision 必须为 approved/rejected，得到 {decision!r}")
        return 2

    # ---- D3-01 门 A（pairing proof）：report 声明的 evidence_sha256 必须等于
    # 提交的 evidence 内容重算值。旧格式（无锚点）在此死亡，bind 不会再被调到。----
    prov = _report_provenance(args.report)
    if prov is None:
        print("[fail] report 不可读或 PROVENANCE_JSON 行缺失/重复/损坏")
        return 2
    ok_anchor, anchor_reason = model_registry.check_evidence_anchor(
        evidence, prov.get(ANCHOR_KEY))
    if not ok_anchor:
        print(f"[fail] report↔evidence 配对锚点不成立：{anchor_reason}"
              f"（锚点方向 = report PROVENANCE_JSON 声明 evidence 内容哈希；"
              "无锚点的旧格式证据不得绑定，D3-01）")
        return 2

    artifact_sha = _ev_value(evidence, "artifact_sha256")
    model_name = _find_model(args.model, str(artifact_sha or ""))
    if model_name is None:
        print("[fail] 无法唯一定位 registry artifact（需 --model 或唯一 artifact_sha256）")
        return 2

    ok_link, link_reason = _check_evidence_against_registry(model_name, evidence)
    if not ok_link:
        print(f"[fail] evidence 与 registry 血统不一致：{link_reason}")
        return 2

    # ---- D3-01 门 B（computation proof）：对 frozen 件 + artifact 字节独立复算
    # pooled 指标（含 CI）并逐位比对。零网络（只读 forecast_outputs/ 与
    # data/models/）；重依赖在本 CLI 入口惰性导入，不进 core.model_registry。
    # **刻意不提供跳过开关**：门 A 锚点只证「配对」，攻击者能自算伪造 evidence 的
    # 哈希让门 A 通过——门 B 是唯一挡住「身份正确、数值凭空捏造」的门。可跳过 =
    # 伪造路径复活，与 A 单验收「旧路径死亡」直接冲突（2026-09-29 自审撤回）。----
    try:
        import evidence_recompute as _erc
    except ImportError as exc:
        print(f"[fail] 复算门依赖不可用（{exc}）——fail-closed 拒绑，"
              "不得降级为只验身份（D3-01）")
        return 2
    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    fc = cfg.get("forecast", {})
    ok_rc, reasons, _recomputed = _erc.recompute_and_check(
        evidence, base_dir=BASE_DIR,
        horizons=tuple(fc.get("horizons", [1, 3, 5])),
        flat_margin=float(fc.get("prob_flat_margin", 0.003)))
    if not ok_rc:
        for r in reasons[:8]:
            print(f"[fail] {r}")
        print("[fail] 复算门未过 ⇒ 拒绑（evidence 数值无法从冻结件 + artifact "
              "字节重算得出，Computation proof 失败，D3-01）")
        return 1
    print("[ok] 门 B 独立复算：pooled 指标（含 CI）与冻结件 + artifact 字节重算值逐位相等")

    report = str(args.report)
    # 唯一实际绑定入口：报告 SHA / PROVENANCE_JSON / registry 血统 / 锚点等规则
    # 全部由 bind_validation() 再次执行（门 A 在其内部重复强制，纵深防御）；
    # 本脚本不复制第二套 report 绑定逻辑。
    bound = model_registry.bind_validation(
        model_name,
        report,
        decision,
        auto_promotion=True,
        evidence=evidence,
    )
    if not bound:
        print("[fail] bind_validation 拒绝绑定（report/provenance/evidence/锚点任一门未过）")
        return 1

    entry = model_registry.get_model_entry(model_name) or {}
    promotion = entry.get("promotion") or {}
    print(f"[ok] validation.evidence 已绑定：{model_name}")
    print(f"[promotion] status={promotion.get('status')} reason={promotion.get('reason')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
