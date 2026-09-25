#!/usr/bin/env python3
"""Explicit v2 validation-evidence binding entrypoint (F3, 2026-09-24).

生产绑定只允许从「report .log + schema v2 evidence.json」进入 registry。
脚本负责输入校验与血统对账，最终写入统一走 core.model_registry.bind_validation()；
不在验证器主流程尾部做 registry I/O，不修改 stdout 复现纪律。

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


# R4-4（Round 4）：CLI 侧业务血缘判定已删除——唯一权威在
# core.model_registry.bind_validation_detailed() 的三向血缘核对，
# 防两处逻辑漂移（B 的 R3-2）。
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

    artifact_sha = _ev_value(evidence, "artifact_sha256")
    model_name = _find_model(args.model, str(artifact_sha or ""))
    if model_name is None:
        print("[fail] 无法唯一定位 registry artifact（需 --model 或唯一 artifact_sha256）")
        return 2

    # R4-4：registry 血统核对由 bind_validation_detailed() 唯一执行（原因可见）。

    report = str(args.report)
    # 唯一实际绑定入口：报告 SHA / PROVENANCE_JSON / registry 血统 / evidence
    # 三向血缘等规则全部由 bind_validation_detailed() 执行；本脚本不复制第二套
    # 绑定逻辑，只把失败原因如实打印（R1-4/R1-12：原因可见）。
    evidence_file = str(args.evidence)
    bound, fail_reasons = model_registry.bind_validation_detailed(
        model_name,
        report,
        decision,
        auto_promotion=True,
        evidence=evidence,
        evidence_file=evidence_file,
    )
    if not bound:
        print("[fail] bind_validation 拒绝绑定：")
        for reason in fail_reasons:
            print(f"  - {reason}")
        return 1

    entry = model_registry.get_model_entry(model_name) or {}
    promotion = entry.get("promotion") or {}
    validation = entry.get("validation") or {}
    print(f"[ok] validation.evidence 已绑定：{model_name}")
    print(f"[promotion] status={promotion.get('status')} reason={promotion.get('reason')}")

    # Phase A（Round 4，R4-2 口径）：producer receipt——身份五件套的可携带
    # 镜像（provenance 加固）。它不是 computation proof，不构成 R3-1 关闭
    # 条件（B 的 R4-2 裁决）；完整关闭判据 = Phase B 独立重算（单独立项）。
    receipt = {
        "receipt_version": 1,
        "model": model_name,
        "artifact_sha256": validation.get("provenance", {}).get("artifact_sha256"),
        "dataset_sha256": validation.get("provenance", {}).get("dataset_sha256"),
        "git_commit": validation.get("provenance", {}).get("git_commit"),
        "report_file": validation.get("report_file"),
        "report_sha256": validation.get("report_sha256"),
        "evidence_file": evidence_file,
        "evidence_sha256": validation.get("evidence_sha256"),
        "producer": _ev_value(evidence, "produced_by"),
        "decision": decision,
        "promotion_status": promotion.get("status"),
        "bound_at": validation.get("bound_at"),
    }
    receipt_path = Path(str(args.evidence) + ".receipt.json")
    if not receipt_path.is_absolute():
        receipt_path = BASE_DIR / receipt_path
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8")
    print(f"[ok] producer receipt 已写盘：{receipt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
