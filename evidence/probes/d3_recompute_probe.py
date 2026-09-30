# -*- coding: utf-8 -*-
"""D3-01② 复算门真实跑通探针（只读，不写 registry / 不碰 data/）。

用真实冻结件 samples_frozen_20260910.jsonl（3371 行）+ 真实 forecast_v3.pkl
（registry 已登记 sha）跑 evidence_recompute 管线，证明：
  ① 独立复算能从「冻结件 + artifact 字节」重算出 pooled 指标（含 CI）；
  ② 用复算真值构造的 evidence 过 check（误差 0）；
  ③ 篡改任一数值 ⇒ check 失败（Computation proof 生效）。
"""
import hashlib
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent   # evidence/probes/ -> 仓库根
sys.path.insert(0, str(BASE))

import evidence_recompute as erc
from core import validation_schema as S

FROZEN = "samples_frozen_20260910.jsonl"
MODEL = "forecast_v3.pkl"
HORIZONS = (1, 3, 5)
FLAT = 0.003

# 真实冻结件 sha（G-A 口径）与真实模型 sha（registry 登记值）
snap = BASE / "forecast_outputs" / FROZEN
dataset_sha = hashlib.sha256(snap.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
model_sha = json.loads((BASE / "data/model_registry/registry.json").read_text(
    encoding="utf-8"))["models"][MODEL]["sha256"]
print("frozen:", FROZEN, "dataset_sha=", dataset_sha[:16])
print("model :", MODEL, "artifact_sha=", model_sha[:16])

# ---- ① 独立复算（真实数据 + 真实模型字节）----
samples, err = erc.load_frozen_samples(dataset_sha, FROZEN, BASE)
assert samples is not None, f"冻结件加载失败: {err}"
print("samples loaded:", len(samples))
models, err = erc.load_artifact_models(model_sha, HORIZONS, FLAT, BASE)
assert models is not None, f"artifact 加载失败: {err}"
print("artifact models loaded:", sorted(models))
recomputed = erc.recompute_pooled(samples, models, HORIZONS, FLAT)
for h, node in recomputed.items():
    print(f"  T+{h}: n={node['n']} rank_ic={node['rank_ic']} "
          f"rank_ic_ci={node['rank_ic_ci']} decision_edge={node['decision_edge']} "
          f"decision_edge_ci={node['decision_edge_ci']} brier={node['brier']}")

# ---- ② 用复算真值构造诚实 evidence，check 应误差 0 ----
ev = S.blank_evidence()
ev["decision"] = "approved"
for h in HORIZONS:
    hs = str(h)
    rec = recomputed[hs]
    node = ev["pooled"][hs]
    for slot in erc.SCALAR_SLOTS:
        v = rec.get(slot)
        node[slot] = S.ev_ok(v) if v is not None else S.ev_na(S.STATUS_NOT_COMPUTABLE)
    for slot in erc.CI_SLOTS:
        v = rec.get(slot)
        node[slot] = S.ev_ok(v) if v is not None else S.ev_na(S.STATUS_NOT_COMPUTABLE)
ok, mismatches = erc.check_evidence_recompute(ev, recomputed)
print()
print("== ② honest evidence check:", "PASS(误差0)" if ok else f"FAIL {mismatches[:3]}", "==")

# ---- ③ 篡改 rank_ic ⇒ check 必须失败 ----
ev_bad = json.loads(json.dumps(ev))
orig = ev_bad["pooled"]["1"]["rank_ic"]["value"]
ev_bad["pooled"]["1"]["rank_ic"]["value"] = (orig or 0) + 0.5
ok3, mis3 = erc.check_evidence_recompute(ev_bad, recomputed)
print("== ③ tampered rank_ic rejected:", "PASS" if not ok3 else "FAIL(篡改未被抓)", "==")
print("   mismatch sample:", (mis3[0] if mis3 else None))

# ---- ④ 凭空声明一个复算不可得的槽位（NOT_COMPUTABLE→OK 假值）⇒ 失败 ----
ev_fab = json.loads(json.dumps(ev))
# 找一个复算为 None 的槽位强行填 OK
fab_slot = None
for h in HORIZONS:
    for slot in erc.SCALAR_SLOTS + erc.CI_SLOTS:
        if recomputed[str(h)].get(slot) is None:
            fab_slot = (str(h), slot)
            break
    if fab_slot:
        break
if fab_slot:
    hh, ss = fab_slot
    ev_fab["pooled"][hh][ss] = S.ev_ok(0.123 if ss not in erc.CI_SLOTS else [0.1, 0.2])
    ok4, mis4 = erc.check_evidence_recompute(ev_fab, recomputed)
    print(f"== ④ fabricated {ss}@T+{hh} rejected:", "PASS" if not ok4 else "FAIL", "==")
    print("   mismatch sample:", (mis4[0] if mis4 else None))
else:
    print("== ④ 无 None 槽位可测（全部可算），跳过 ==")
