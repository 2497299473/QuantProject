# evidence/ · 索引

每条证据一行。**状态**列：`FRESH`（可复现、输入在档）/ `STALE`（输入或口径已变，仅历史参照）
/ `SUPERSEDED`（被后续轮次取代）。

## validation/ · 验证报告

| 日期 | 主题 | 报告 | 元数据 | 输入 | 状态 | 裁决 |
|:--|:--|:--|:--|:--|:--|:--|
| 2026-09-23 | backtest_forecast（三周期 OOS） | `validation/backtest_forecast_0910frozen_20260923.log` | `…meta.json` | 0910 冻结件（G-A PASS / G-B DRIFTED） | FRESH | T+1/T+3/T+5 全 ❌（rank_ic +0.049/+0.048/−0.019，CI 全跨零）；model_ready 维持 false；**未绑 registry** |

### 说明

- 本件是 `evidence/` 目录建立后的**第一份**落档验证报告（V4.5，2026-09-23）。
- **补的是哪条断链**：`registry.json` 里两个模型绑定的验证报告
  （`backtest_forecast_v8_quantile_20260830.log`、`backtest_forecast_v3_b1mask_20260831.log`）
  在本地与 git 历史中**都不存在**，审计 `P1-2` 因此常年 FAIL。本件不改那两个历史绑定
  （改绑会覆盖现行授权链），只保证**从今往后**每轮验证都有在档原件。
- **历史绑定怎么办**：~~`P1-2` 的处置需 Summer 拍板（重跑并改绑 / 把历史绑定降级为
  档案事实）。在拍板前，该 FAIL 如实保留，不粉饰。~~
  **已处置（2026-09-30，Summer 裁决 D3-07 = 选项 2「降级为档案事实」）**：
  registry 两条目（forecast_v2/v3）的 `validation` 块打 `archived: true` 标记
  （含 `archived_reason` / `archived_at` / `archived_authorized_by`，其余字段含
  `report_sha256` 值逐字节不动——prereg D3 判据读的是该值，shadow 降级授权链
  不受影响，已实证 `prereg_degradation` 仍 True）；`audit_project.check_validation_binding`
  同步增分档：archived 条目的原件缺失 → WARN（detail 带「已处置档案」），
  **未打标的缺失/sha 不符仍 FAIL**（删标记即恢复红灯）。落地后 P1-2 转 WARN，
  `audit_health` 转 PASS_WITH_WARNINGS。
  **打标权红线**：`archived` 仅限 Summer 授权批次可打（本批授权记录 = 本节 +
  registry `archived_authorized_by` 字段 + commit message）；测例钉死「active 条目
  带 archived ⇒ P1-13 derive_promotion 推导不受扰」，防标记被滥用为绕过授权门
  可见性的通道（`tests/test_audit_hygiene.py::TestP12ArchivedDisposition`，11 条）。
  代价如实记录（A 单核实）：active model（forecast_v3）的报告缺失同降 WARN——
  「active 原件在档」的强制力从审计 FAIL 转移给「打标须 Summer 授权」纪律。

## probes/ · 探针与对照实验

| 日期 | 主题 | 文件 | 状态 | 备注 |
|:--|:--|:--|:--|:--|
| 2026-09-30 | D3-07/08 落地自测（archived 档案化 + sha 口径修复） | `probes/d3_batch_selftest_20260930.log` + `.meta.json`（同批重生成；探针新增 `d307_archive_probe.py`） | FRESH | P1-2 由 FAIL 转 WARN（已处置档案 2 条），audit_health=PASS_WITH_WARNINGS；授权链零扰动实证（prereg 降级仍 True / verify_validation_report 仍拒 / derive 仍 blocked / pinned sha 未动）；D3-08：log_sha256 改为对落盘字节计算（修 Windows CRLF 假 sha，已独立复核 MATCH）；fast 781 OK / slow 222 OK(skipped=3)，含 P1-2 新测例 11 条 |
| 2026-09-30 | 面 3（证据与血缘）D3 批修复自测（D3-01~06；D3-07 待裁决） | `probes/d3_batch_selftest_20260930.log` + `.meta.json`（收集器 `d3_collect_selftest.py`，探针 `d3_recompute_probe.py` / `d3_backfill_probe.py`） | SUPERSEDED（被上行同批重生成取代；首版 log_sha256=3ede9cb6… 为字符串口径，即 D3-08 所修缺陷的实例） | D3-01 配对锚点+独立复算门（真实 forecast_v3.pkl + 0910 冻结件端到端：诚实件误差 0 / 篡改必拒）；D3-02 快照自洽校验；D3-03 first_identical_commit 三类测例+可达性过滤；D3-04/05 meta 畸形 fail-closed 成对测例；D3-06 pkl 身份全长比对（回填逐字节不变）。fast 770 OK / slow 222 OK(skipped=3) / audit 18 PASS·1 FAIL（P1-2=D3-07 既有项）·4 WARN |
| 2026-09-29 | 审计 D-04/D-08 裁决落地自测 | `probes/audit_d04_d08_selftest_20260929.log` + `.meta.json` | FRESH | P1-5 改 AST 结构判据（import 代价实测 18× 为裁决依据）；D-08 维持下游拦截零逻辑改动；fast 703 OK / slow 188 OK(skipped=3) / audit stderr 0 字节；P1-2 FAIL 与 P1-9 WARN 均为既有项非本批引入 |
| 2026-09-28 | 审计 D 批修复自测（D-01/02/03/05/06/07/09） | `probes/audit_d_batch_selftest_20260928.log` + `.meta.json` | FRESH | fast 692 OK / slow 188 OK / audit 零 stderr；P1-2 FAIL 为既有预留项非本批引入 |
| — | （待落档） | — | — | 探针输出仍在 `output/`（gitignored），后续按需迁入 |

## incidents/ · 事故取证

| 日期 | 事件 | 文件 | 状态 | 备注 |
|:--|:--|:--|:--|:--|
| 2026-09-23 | 14:55 post 轮证据链断档（报告生成前抛异常） | 待补 | OPEN | 已由 V4.5 FAILED-manifest 兜底修复；原始日志 `output/logs/run_20260923_145504.log`（本地） |
