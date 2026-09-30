# -*- coding: utf-8 -*-
"""D3-02/03/06 回填探针（只读，不写 registry）：
1. D3-06 验收：真实 registry 现有条目回填结果逐字节不变（attest + commit）
2. D3-03 验收：真实 registry 现有条目 commit 仍为 6e0c9dd66
3. D3-06 负向：前 16 位相同、后 48 位不同的伪造 sha 被拒
"""
import hashlib
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent   # evidence/probes/ -> 仓库根
sys.path.insert(0, str(BASE))

import bind_provenance as bp
from core import model_registry as mr

snaps = bp.manifest_snapshots()
reg = mr.load_registry()
print("snapshots parsed:", len(snaps), [s["path"].name for s in snaps])

ok_all = True
for name, entry in sorted(reg["models"].items()):
    sha = entry.get("sha256", "")
    s = bp.attest_snapshot(snaps, name, sha)
    commit, rel = bp.first_identical_commit(name, sha)
    man16 = bp.sha256_16(s["path"]) if s else None
    same_man = man16 == entry.get("data_manifest_sha256")
    same_commit = commit == entry.get("git_commit")
    print(f"{name}:")
    print(f"   attest            -> {s['path'].name if s else None}")
    print(f"   manifest_sha16    -> {man16}  (bound={entry.get('data_manifest_sha256')}) same={same_man}")
    print(f"   first_identical   -> {commit}  (bound={entry.get('git_commit')}) same={same_commit}")
    ok_all = ok_all and bool(same_man) and bool(same_commit)

print()
print("== D3-06 backfill unchanged:", "PASS" if ok_all else "FAIL", "==")

# ---- D3-06 负向：前 16 位相同、后 48 位不同 ⇒ 拒 ----
real = reg["models"]["forecast_v3.pkl"]["sha256"]
forged = real[:16] + "f" * 48
s_forged = bp.attest_snapshot(snaps, "forecast_v3.pkl", forged)
c_forged, _ = bp.first_identical_commit("forecast_v3.pkl", forged)
print("forged sha (same first16) attest ->", s_forged, "commit ->", c_forged)
neg_ok = (s_forged is None and c_forged is None)
print("== D3-06 forged-prefix16 rejected:", "PASS" if neg_ok else "FAIL", "==")

# ---- D3-02 预览：manifest snapshot_id 复算（只读）----
print()
import data_fingerprint as dfp
for s in snaps:
    d = s["payload"]
    files = d.get("files") or {}
    declared = d.get("snapshot_id")
    recomputed = dfp._snapshot_id(files)
    nf_ok = d.get("n_files") == len(files)
    print(f"{s['path'].name}: declared={declared} recomputed={recomputed} "
          f"match={declared == recomputed if declared else 'N/A(no snapshot_id key)'} "
          f"n_files_ok={nf_ok}")
