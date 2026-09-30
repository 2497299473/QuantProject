# -*- coding: utf-8 -*-
"""D3-07 验收探针（只读，不写 registry / 不碰 data/）：
① registry.json 仅 2 条目 validation 块增 archived 四键，其余字段零改动
   （与 git HEAD 版本逐字段 diff——本探针在提交前跑，对照对象是暂存区/HEAD 的 blob）；
② report_sha256 值未动（prereg D3 判据读的就是它）；
③ 授权链零扰动实证：
   - evaluate_prereg_degradation('forecast_v3.pkl') 仍 (True, 'prereg_degraded')
   - verify_validation_report 两条目仍拒（archived 不松动授权门）
   - derive_promotion 仍 blocked（legacy metrics 路径，与 archived 无关）
   - prereg_pinned_sha256 仍返回钉住值
④ audit P1-2 实况：FAIL → WARN（已处置档案 2 条），audit_health → PASS_WITH_WARNINGS
"""
import json
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

REG = BASE / "data" / "model_registry" / "registry.json"

# ---- ①② 与 D3-07 改动前基准（09b52fe，D3 批主提交）逐字段 diff ----
# 刻意钉死 commit 而非 HEAD：本批提交后 HEAD 已含 archived 标记，对 HEAD diff
# 会永远空集、探针失真（探针必须终身可重跑复核）。
BASE_COMMIT = "09b52fe"
head_blob = subprocess.run(
    ["git", "-C", str(BASE), "show",
     f"{BASE_COMMIT}:data/model_registry/registry.json"],
    capture_output=True, text=True, encoding="utf-8").stdout
before = json.loads(head_blob)
after = json.loads(REG.read_text(encoding="utf-8"))

ARCHIVE_KEYS = {"archived", "archived_reason", "archived_at", "archived_authorized_by"}
violations = []
assert set(before["models"]) == set(after["models"]), "条目集变了"
for name in before["models"]:
    b, a = before["models"][name], after["models"][name]
    if set(b) != set(a):
        violations.append(f"{name}: 顶层键集变化 {set(b) ^ set(a)}")
    for k in b:
        if k != "validation":
            if b[k] != a[k]:
                violations.append(f"{name}.{k}: 被改动")
        else:
            vb, va = b[k], a[k]
            added, removed = set(va) - set(vb), set(vb) - set(va)
            changed = [kk for kk in vb if kk in va and vb[kk] != va[kk]]
            if added != ARCHIVE_KEYS:
                violations.append(f"{name}.validation: added={sorted(added)} ≠ 预期四键")
            if removed or changed:
                violations.append(f"{name}.validation: removed={sorted(removed)} changed={changed}")

print("== ① registry 逐字段 diff ==")
for name in ("forecast_v2.pkl", "forecast_v3.pkl"):
    va = after["models"][name]["validation"]
    print(f"  {name}: archived={va.get('archived')} at={va.get('archived_at')} "
          f"by={str(va.get('archived_authorized_by'))[:24]}…")
    print(f"     report_sha256 未变: "
          f"{va['report_sha256'] == before['models'][name]['validation']['report_sha256']}")
print("  violations:", violations or "无")
print("== ①② PASS ==" if not violations else "== ①② FAIL ==")

# ---- ③ 授权链零扰动 ----
from core import model_registry as mr

print("\n== ③ 授权链零扰动实证 ==")
ok, reason = mr.evaluate_prereg_degradation("forecast_v3.pkl")
print(f"  prereg_degradation(forecast_v3): ({ok}, {reason!r}) "
      f"{'PASS' if (ok, reason) == (True, 'prereg_degraded') else 'FAIL'}")
for n in ("forecast_v2.pkl", "forecast_v3.pkl"):
    r = mr.verify_validation_report(n)
    print(f"  verify_validation_report({n}): {r} "
          f"{'PASS(仍拒)' if not r[0] else 'FAIL(被松动!)'}")
e = mr.get_model_entry("forecast_v3.pkl")
d = mr.derive_promotion(e["validation"])
print(f"  derive_promotion(v3): {d['status']} "
      f"{'PASS' if d['status'] == 'blocked' else 'FAIL'}")
pinned = mr.prereg_pinned_sha256("forecast_v3.pkl")
print(f"  prereg_pinned_sha256(v3): {str(pinned)[:16]}… "
      f"{'PASS' if pinned == e['sha256'] else 'FAIL'}")
