# evidence_audit —— 晋升证据法院的攻击探针（审校留档）

来源：审校 Round 3/4（2026-09-25）。外部审校 B 提出「身份真 ≠ 指标真」（R3-1），
A 实跑复现后按 R4-2/R4-7 裁决入库：本目录的探针是 **R4-7 要求的 S3 红灯的
人工可读版**，配套的自动化断言在 `tests/test_evidence_lineage.py::TestS3RedPins`。

## 探针覆盖（@v4.4-r4fix-freeze-receipt 起的行为）

| 探针 | 场景 | 当前处置 | 依据 |
|---|---|---|---|
| P1 | 占位-OK 文本（frozen_dataset="unknown" 等） | 可入档，promotion `blocked_provenance` | R2-4 门 5 占位黑名单 |
| P2 | 真身份 + 伪造 `power.frozen=True`、无独立冻结记录 | 可入档，promotion `blocked_power` | R4-8-lite 门 1 对账 `data/power_freeze.json` |
| P3 | 三身份伪造（≠ registry/report） | CLI 反查失败（rc=2）；带 `--model` 时 bind 三向血缘拒（rc=1） | R2-1 三向血缘 |
| OPEN | 真身份 + 真文本 + 伪造指标 + 冻结记录在案 | **promotion approved** | **R3-1 computation lineage 未闭环**——Phase B（独立重算）单独立项 |

## 用法

    python tools/evidence_audit/r3_attack_metrics_forge.py [repo_root]

默认 repo_root = 本文件上两级。全程零网络、临时 registry、真实 registry 零改动。
测试层同等断言见 `tests/test_evidence_lineage.py::TestS3RedPins`
（S3-ID 为 expectedFailure 施工钉，Phase B 落地翻绿即拆）。

## 状态（2026-09-25）

- **Phase A**（producer receipt，provenance 加固）：已落地——
  `bind_validation_evidence.py` 绑定成功后写盘 `<evidence>.receipt.json`
  （身份五件套镜像）。定位按 R4-2 裁决：**加固，非 R3-1 关闭条件**。
- **Phase B**（independent computation verification）：**未落地**——R3-1 的
  最终关闭判据，单独立项；落地并通过攻击回归前，不宣称 R3-1 已关闭。
- 挂账（Summer）：N_POWER_HORIZON 契约语义、pooled 样本量量尺（R2-3）。
