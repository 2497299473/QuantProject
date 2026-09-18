# 预注册 · T+3 RankIC 三拆（time-series / cross-sectional / pooled）

> 立文：2026-09-18 19:5x（跑前写死，跑后判据一字不改）。
> 背景：V4.2 封版后研究主线第一条 —— GPT 复核遗留项「RankIC 三拆未做」。
> 现有 T+3 证据全部是 **pooled** 口径（WF +0.036 CI[-0.023,+0.092] 不显著；rolling 分窗仅点估计），
> pooled 混淆了「同一天谁比谁好」（横截面尺度）与「同一只基金明天比今天好没好」（时序尺度），
> 二者对日频参谋的含义完全不同：CS 支持轮动排序，TS 支持单基金方向判断。
> 本文只裁决**研究轨道（D-lite 冻结面板）**；生产轨道（C2 重跑 WF/rolling）另行授权，不共用本文判据。

## 一、问题（唯一）

在冻结 D-lite 面板上，LGB(a158-50+mask) 逐折 WF 的 OOS 预测，其 **T+3** 预测力在
cross-sectional / time-series / pooled 三个尺度上各自是否成立？T+1 / T+5 同口径输出仅作**对照列**，
不进判据（T+5 已有裁决，见 §六 T3 / WF 09-08 基线）。

## 二、数据与环境（全部事前锁死）

- 面板：`forecast_outputs/panel_dlite_v2_20260917.jsonl`
  sha256 必须 == meta `c67dc6b97c0d2c3a3e89a66c208b6c91a84686c4c0f997ac53c582c22d0d8443`，漂移即中止。
  n_rows=36,856；date_max=2026-09-09（面板 09-17 冻结，尾部滞后生产数据 ≤7 交易日，如实记录，不补数据）。
- 池子：主判据 = kind ∈ {fund, gate_proxy, sector} 派生（本批 14 员，等价性由 run_t3_panel_power chk13b 已证）；
  17 全池（含 relaxed）仅敏感性参照——§七「relaxed 不并入主池出单一数」纪律继承，不得挪作裁决。
  〔更正（跑后记，2026-09-18）：kind **派生规则**不变；v2 冻结面板已扩档（extra_gate_proxies +9），
  派生结果 = 23 员（v1 批的 14 员等价性 chk13b 只对 v1 冻结件成立）。实际执行按派生规则走，23 员，非笔误改判据。〕
- 特征/标签：a158-50 + B1 掩码 = 100 维（与 T3 power 同空间）；端点 fwd1/fwd3/fwd5 绝对收益。
- 折：`build_wf_folds(window_days=63, max_horizon=5)`，oos_start 固定 **2025-04-30**（§七 不动）。
- 基线模型：`run_m0_power.LGB_PARAMS` / N_ROUNDS，seed=42，逐折 expanding 重训 → pooled OOS 预测拼接。
  三个端点各训一次（H=1/3/5），互不混用样本过滤逻辑（各自剔 label 缺失行）。
- 零网络：依赖装载后装 socket 守卫（继承 run_t3_panel_power chk16 实测口径），任何 connect 即 RuntimeError。
- 只读 forecast_outputs/；不碰 config / model_registry / 生产 .py / 定时任务。

## 三、三拆定义（写死）

设 OOS 预测集合 {(pred_i, y_i, date_i, member_i)}。秩相关一律 Spearman（复用 `backtest_forecast.rank_ic`）。

1. **pooled**：全体样本一次算 rank_ic(pred, y)。即现行口径（对照用，预期与 T3 power 的 IC 同数量级）。
2. **cross-sectional（CS）**：按 date 分组，日宽 n_d ≥ **4** 的日子逐日算 rank_ic(pred, y | d)；
   主统计 = 日 IC 均值；CI = 对「日」整块有放回重抽样 B=1000、seed=42（与 `ic_resample_sd` 同引擎）取 2.5/97.5 分位。
   日宽 <4 的日剔除（2–3 元的截面秩相关无解释力，写死为纪律而非调参）。
3. **time-series（TS）**：按 member 分组，成员内 n ≥ **60** 的成员逐员算 rank_ic(pred, y | member)；
   主统计 = 成员 IC 均值（并列报中位数、正值成员占比）；CI = 对「成员」整块重抽样 B=1000、seed=42。
   成员级重抽的理由：成员是 TS 的相关单位（同日不同成员在 TS 口径下是独立成员内观测，不整日绑定）。

## 四、判据（先写死；仅裁决 T+3）

- **A1 横截面成立**：T+3 CS 日 IC 均值的 95% CI 下界 > 0。
- **A2 时序成立**：T+3 TS 成员 IC 均值 > 0 **且** 正值成员占比 ≥ 2/3。
- **A3 pooled 虚高登记**：pooled 的日块 CI 下界 > 0 而 A1、A2 **均**不成立 → 记「pooled 膨胀」
  （size/时点混合效应主导），T+3 不得升格，README 的「T+1/T+3 仅点估计」维持。
- **升格规则**：A1 ∧ A2 同时成立 → T+3 从「点估计参考」升「证据积累」，解锁下一步 C2 生产侧三拆对比；
  仅其一成立 → 不升格，只把成立的那一尺度写进 README 注脚（不得笼统写「T+3 有效」）。
- 敏感性：pool17 全池同跑一遍，只报数字不作裁决（若 pool17 与 pool14 结论相反 → 明示「池子敏感」，降置信）。
- 可复现：pool14 T+3 全流程连跑两次，三拆全部主统计逐一 ≤1e-9 一致，否则中止不产报告。

## 五、产出

- `forecast_outputs/t3_decomp_20260918.json`（gitignore 区，本地留档）：逐池 × 逐端点的三拆数字 + 元数据（面板 sha、折序、参数）。
- 人读摘要打进 stdout，事后把裁决结果按本文口径誊入 README 与 Obsidian（誊录不得改判据解释）。

## 六、明确不做

- 不做 MDE/power/permutation（那是 T3 panel power 的职责，已裁决过）；
- 不做 RankIC 衰减曲线 / regime 切分 / 特征归因（三拆只做尺度分解）；
- 不改 `backtest_walk_forward.py` 等生产脚本（C2 用原脚本原口径重跑，三拆工具若日后上生产另行预注册）。
