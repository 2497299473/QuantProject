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

    ok_link, link_reason = _check_evidence_against_registry(model_name, evidence)
    if not ok_link:
        print(f"[fail] evidence 与 registry 血统不一致：{link_reason}")
        return 2

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
    print(f"[ok] validation.evidence 已绑定：{model_name}")
    print(f"[promotion] status={promotion.get('status')} reason={promotion.get('reason')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
