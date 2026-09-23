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
- **历史绑定怎么办**：`P1-2` 的处置需 Summer 拍板（重跑并改绑 / 把历史绑定降级为
  档案事实）。在拍板前，该 FAIL 如实保留，不粉饰。

## probes/ · 探针与对照实验

| 日期 | 主题 | 文件 | 状态 | 备注 |
|:--|:--|:--|:--|:--|
| — | （待落档） | — | — | 探针输出仍在 `output/`（gitignored），后续按需迁入 |

## incidents/ · 事故取证

| 日期 | 事件 | 文件 | 状态 | 备注 |
|:--|:--|:--|:--|:--|
| 2026-09-23 | 14:55 post 轮证据链断档（报告生成前抛异常） | 待补 | OPEN | 已由 V4.5 FAILED-manifest 兜底修复；原始日志 `output/logs/run_20260923_145504.log`（本地） |
