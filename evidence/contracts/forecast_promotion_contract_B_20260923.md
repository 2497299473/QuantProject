# Forecast Promotion Contract B · 四基金泛化 + Baseline Edge 正式晋升契约（Draft）

> **落档信息**：2026-09-23 由 Summer 拍板路线 A（decision_edge 正式进入 promotion 判据）后固化落档。
> **状态**：草案 / 待预注册——`power_threshold` 等 numeric 阈值留白，须 FINAL LOCKBOX 前经功效分析冻结（§6.2）。
> **施工状态（同日 B 批）**：本契约 §15 仅允许的三项工程改动已落地——
> B1 `backtest_action.bootstrap_ci` → 复用 `backtest_forecast.cluster_bootstrap_ci`（测例 `tests/test_action_bootstrap.py`）；
> B2 `verify_feature_protocol` → protocol_version/feature_keys/n_features/feature_dim/masking 整块 exact 校验（测例 `tests/test_feature_protocol_strict.py`）；
> B3 `load_samples(require_fwds=...)` → 样本构建与 target 可用性分离，Forecast 短周期不再被 fwd20 连坐（测例 `tests/test_b3_sample_retention.py`）。
> 本契约本身不执行训练、不执行外部数据请求、不消耗 FINAL LOCKBOX；不改变当前 `model_ready=false`、`model_promotion=BLOCKED`。
> 契约正文以下为原文（Summer 2026-09-23 审定稿），除本头注外未改写。

---

## 1. 契约目标

本契约回答的不是「模型在历史数据上有没有一定预测能力」，而是更严格的三个问题：

1. 模型是否对未来收益具有样本外排序/概率预测能力；
2. 模型是否**相对于 14:55 可观测的机械基线 `est_chg` 提供增量信息**；
3. 这种增量是否能够在项目的四只生产目标基金上成立，而不是被池级样本占比主导。

因此，production promotion 不再仅由 pooled 三周期结果决定。

## 2. 生产目标集合

```text
F1 = 002112
F2 = 002207
F3 = 022853
F4 = 025687
```

原则：`Pooled evidence ≠ Production-fund evidence`。池级统计只能作为总体证据，不能替代四基金证据。

## 3. 核心判据：Baseline Edge

### 3.1 唯一机械基线

Forecast 的增量基线固定为 `est_chg`（`fraction` 量纲）。禁止在 promotion 运行后临时替换 baseline。

### 3.2 Decision Edge 定义

对同一 OOS 样本、同一基金、同一 horizon：

```text
decision_edge(h) = RankIC(model_score_h, fwd_h) − RankIC(est_chg, fwd_h)
```

- `model_score_h` = 模型用于排序的预测分数；
- `fwd_h` = Forecast Contract C1 定义的最终 NAV 口径未来收益；
- 两者必须使用完全相同的样本行；不允许因 baseline 不利而更换样本。

## 4. Promotion 不再采用单一 pooled 闸门

```text
Pooled Evidence AND Production-Fund Evidence AND Baseline Edge Evidence
AND Calibration / Probability Evidence AND Frozen Provenance
AND Registry / Protocol Integrity
```

任何一层不满足：`production_status = BLOCKED`，`model_ready = false`。

## 5. Pooled Evidence

每个 horizon（T+1 / T+3 / T+5）分别计算：RankIC、RankIC 95% CI、Brier、Brier 95% CI、Calibration/ACE、`decision_edge`、`decision_edge` 95% CI、与多数类基线比较、与 `est_chg` 基线比较。

### 5.1 Baseline Edge 正式晋升条件

```text
decision_edge_CI_lower > 0
```

才可认定模型相对 `est_chg` 存在统计意义上的增量排序信息。仅 `RankIC(model) > RankIC(est_chg)` 不再视为充分证据。

### 5.2 绝对 RankIC

绝对 RankIC 保留，职责改为「模型自身预测能力」；`decision_edge` 负责「相对机械基线的增量能力」。`RankIC > 0` 不能替代 `decision_edge CI lower > 0`。

## 6. Production-Fund Evidence

四只生产目标基金必须逐基金单独出具 fund × horizon 矩阵（n、各周期 Edge、各周期 CI）。

### 6.1 小样本不是「自动通过」

对任意基金 `n < power_threshold`：结果标 `INSUFFICIENT_POWER`，不得解释为 PASS，且 `overall promotion = BLOCKED`。防止 025687（当前冻结件 n=35）之类小样本被迫产生二元结论。

### 6.2 Power Threshold 不在本契约中拍脑袋

`power_threshold` 在 FINAL LOCKBOX 前通过预注册的功效分析确定，须记录：
`effect_size_target` / `confidence_level` / `power_target` / cluster structure / expected OOS trading days / expected samples per fund。完成后写入并冻结 `N_POWER_FUND`、`N_POWER_HORIZON`。

## 7. 四基金晋升规则

- **F1 四基金全部可判定**：`∀ fund ∈ production_funds: n >= N_POWER`，否则 `BLOCKED_POWER`。
- **F2 增量能力不能只存在于 pooled**：每只满足功效要求的基金在主晋升周期上 `decision_edge_CI_lower > 0`；horizon 组合（方案 A：三周期全过 / 方案 B：T+5 主判据）在正式功效分析完成后于预注册中冻结，本草案不预判。
- **F3 不能出现「池级通过、目标基金整体失效」**：evidence 必须同时保存 `pooled_edge` 与四只 `fund_edge[*]`；禁止只以 pooled 汇总通过后覆盖/隐藏基金级结果。

## 8. Bootstrap 统计协议

所有涉及 OOS CI 的主要指标统一使用 **cluster bootstrap by trading day**（同日多基金样本视为同一 cluster 整块重采样）。禁止 promotion 使用逐基金日行独立 bootstrap。`backtest_action` 已于 2026-09-23 复用 `backtest_forecast.cluster_bootstrap_ci()`（B1），不得另造第二套实现。

## 9. Calibration

概率预测晋升至少保留 Brier 与 Calibration/ACE。Quantile（Q10/Q50/Q90）独立记录 Coverage / Pinball / CQR coverage。**区间校准 ≠ Alpha 证据**：CQR coverage 恢复目标区间不能反向证明方向预测能力。

## 10. OOS / FINAL LOCKBOX 纪律

当前 0910 OOS **永久降级为 development / selection evidence**，不得再称 FINAL LOCKBOX。FINAL LOCKBOX 必须在 B 契约冻结 + 数据入口修复 + 历史特征口径最终确定 + 功效阈值冻结之后**一次生成**。执行后不得根据结果修改：feature set、hyperparameters、baseline、acceptance criteria、horizon selection、bootstrap method、calibration rule——否则该轮重新降级为 development evidence。

## 11. Promotion 状态机

```text
RESEARCH_ONLY
     ├── BLOCKED_POWER
     ├── BLOCKED_PERFORMANCE
     ├── BLOCKED_BASELINE_EDGE
     ├── BLOCKED_CALIBRATION
     ├── BLOCKED_PROVENANCE
     └── APPROVED
```

`BLOCKED_POWER` 不代表模型失败，只代表目前证据不足以做生产授权。

## 12. `derive_promotion()` 新契约方向

当前「validation.decision + 三周期 approved」需扩展为能够读取 `validation.metrics / validation.pooled / validation.funds / validation.baseline_edge / validation.calibration`。最终纯函数签名保持 `derive_promotion(validation)`：同一 evidence 只能推出同一 promotion；人工不得手写 `promotion=approved`，唯一人工开关仍是 `config.forecast.model_ready=true`。
（本批不动 `derive_promotion`——schema 字段尚未由验证器产出，扩展属后续独立批次。）

## 13. 推荐的 validation evidence schema

```json
{
  "decision": "approved",
  "protocol": {"version": 1, "feature_dim": 14},
  "pooled": {
    "1": {"rank_ic": null, "rank_ic_ci": [null, null],
           "decision_edge": null, "decision_edge_ci": [null, null],
           "brier": null, "ace": null},
    "3": {}, "5": {}
  },
  "funds": {
    "002112": {"1": {}, "3": {}, "5": {}},
    "002207": {"1": {}, "3": {}, "5": {}},
    "022853": {"1": {}, "3": {}, "5": {}},
    "025687": {"1": {}, "3": {}, "5": {}}
  },
  "power": {"n_power_fund": null, "method": null, "effect_size_target": null,
             "confidence": null, "target_power": null},
  "calibration": {},
  "baseline": {"name": "est_chg", "unit": "fraction"}
}
```

`null` 不是「通过」。必须明确区分 `UNKNOWN` / `INSUFFICIENT_POWER` / `NOT_COMPUTABLE`，不能用一个 `null` 混掉三种状态。

## 14. 与现有架构的关系

本契约不新增独立 promotion engine。复用：`backtest_forecast.cluster_bootstrap_ci` / `backtest_forecast.rank_ic` / `backtest_forecast.calibration_curve` / `core.forecast_contract` / `model_registry.derive_promotion` / `model_registry.verify_approval` / `frozen_dataset.resolve_samples`。原则：**新增契约字段，而不是新增第二套事实源**。

## 15. 本阶段必须落地的最小代码修改（施工记录）

| 项 | 内容 | 状态 |
|:--|:--|:--|
| B1 | `backtest_action.bootstrap_ci` 复用 `cluster_bootstrap_ci` + 日聚类正/反测例 | ✅ 2026-09-23 落地 |
| B2 | `verify_feature_protocol` exact verification（version/keys/n_features/dim/masking 整块）+ 正反测例 | ✅ 2026-09-23 落地 |
| B3 | `load_samples` 拆分 sample construction 与 target availability（`require_fwds`），fwd1/3/5 独立可用；既有完整样本既有值逐位不变 | ✅ 2026-09-23 落地 |

## 16. 本阶段明确「不做」的事项

新模型 / 新特征 / 新因子 / 新 State Feature / 新市场 Context 输入 / 新数据源 / 历史 14:55 回补 / MODEL_VERSION bump / `model_ready=true` / `history_validated=true` / FINAL LOCKBOX 执行——均属后续独立批次。

## 17. 本契约最终要形成的唯一决策问题

B + C 完成后，FINAL LOCKBOX 只需回答：

> **在真实可观测输入口径下，Forecast 是否在四只生产基金、目标 horizon 上，相对于 `est_chg` 机械基线提供稳定且可复核的样本外增量信息？**

- YES → 进入 promotion review；
- NO → 继续 research-only；
- INSUFFICIENT_POWER → 继续积累样本，不把「不知道」伪装成「通过」。
