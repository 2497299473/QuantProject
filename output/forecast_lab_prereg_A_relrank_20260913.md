# 候选 A · `rel_rank` 预注册（2026-09-13，**跑前写死**）

> 依据：`output/forecast_lab_candidate_options_20260911.md` §候选 A（原始规格）
> ＋ `output/forecast_lab_review_v2_20260912.md` §四（功效修正：同族候选实测 MDE≈0.056 ≤ 0.06
> → v1 的「MDE≥0.10 才分叉」不再触发，A 属于「同族增量、功效够得着」档）。
> 纪律：本文件在 `run_a_relrank.py` 运行**之前**落盘；**跑完不得修改判据**，只可追加结果行。

---

## 一、待检验假设

绝对 `fwd5` 里的同期共同成分（大盘 / beta）对**排序**是纯噪声源。
把训练标签换成**同日横截面超额**，在同一套 7 特征、同一模型族下，应给出更好的排序能力。

## 二、冻结的设计（先写死，跑完不改）

1. **样本**：`forecast_outputs/samples_frozen_20260910.jsonl`
   （sha256 `be8e55f02b39a25d…`，零网络、**不重冻结**——本实验不需要重算 `est_chg`）。
2. **特征**：existing 7（与 P1 / M0 完全一致）。**不改特征、不改模型族、不改折协议。**
3. **折协议**：`split_date_oos(ratio=0.8, max_horizon=5)` + `build_wf_folds(window_days=63)`，
   expanding 重训，label-end purge 沿用。
4. **模型**：LightGBM，P1 参数（复用 `run_m0_power.LGB_PARAMS`，300 轮，seed 42）。
5. **实验臂（唯一变量 = 训练标签）**：
   | 臂 | 训练标签 | 角色 |
   |---|---|---|
   | `base` | `fwd5`（绝对） | 对照，须复现 P1 的 −0.0148 |
   | `A_mean` | `fwd5 − mean(同日池内 fwd5)` | **primary 候选** |
   | `A_med` | `fwd5 − median(同日池内 fwd5)` | robustness |
   - 同日横截面按 `date` 完整划分：折按日切分，不存在横跨训练/测试的同日样本 → 无跨段泄漏。
   - 基准篮子取「**池内等权均值**」为 primary、中位数为 robustness；**不测**主题 proxy
     （proxy 属方案 D/B 的口径，且会同时改变信息源，违反「只换标签」）。
6. **端点（先写死）**：
   - **primary 端点 = 对 `fwd5`（绝对）的 pooled RankIC** —— 与 P1 / M0 / 准入门②同一把尺子，可比。
   - **secondary 端点 = 对相对标签的 pooled RankIC** —— 诊断：增益是否只体现在横截面排序上。
7. **判据读数：并列报告，不预选**（因「判据本身要不要改」待 Summer 拍板）：
   - 现行门槛②：`pct2.5(D) > 0`，D = IC_cand − IC_base，日块有放回重抽 B=1000、seed=42。
   - 建议替代（v2 §四②）：`Δ点估计 ≥ 0.05` **且** `pct10(D) > 0` **且** 逐折 ≥3/4 不劣于。
8. **功效参照**：实测 `ρ_IC(cand, base)`，按 v2 §三.3 映射表给出该候选的 MDE 档位
   （用于判断「本次不达标」是「真没效果」还是「功效不够」）。
9. **自校验（跑前设定）**：`base` 臂必须逐位复现 `run_m0_power.run_wf()` 的预测；
   不通过即中止、不出结论。

## 三、禁止事项（防事后挑口径）

- 跑完后**不得**更换 primary 端点或基准篮子定义。
- 两个端点必须**同时**出现在结果表；不得只报有利的那个。
- **不替 Summer 定**「判据该不该改」——本实验只出数。
- 不碰任何 `config.json` / `registry.json` / 生产 `.py` / 定时任务。

## 四、预期产出

- `forecast_outputs/cand_a_relrank_20260913.json`（机器可读）
- `forecast_outputs/cand_a_relrank_20260913.log`（stdout 原文）
- 结果报告：`output/forecast_lab_cand_a_20260913.md`

—— OpenSquilla · 2026-09-13（跑前落盘）
