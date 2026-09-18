# C2 生产轨 RankIC 三拆 · 预注册（判据跑前写死，2026-09-18 20:50）

## 一、要回答的唯一问题

现行生产口径（`backtest_walk_forward.py`：HGB 三分类 → 用 `p_up` 作为预测分数，
对 `fwd5` 真值算 rank_ic）报出的 **T+5 pooled WF RankIC 显著**，这个显著性究竟由哪个
尺度撑起——横截面（同一天谁比谁好）还是时序（单只基金自身方向）？

本文只裁决生产轨（fund_pool 4 码 + 冻结样本）。研究轨 C1（D-lite 面板 + LGB 回归）
判据见 `forecast_lab_prereg_T3decomp_20260918.md`，两轨**不得跨轨等值比较**：
模型、特征空间、样本池均不同。

## 二、输入（事前锁死）

- 样本文件：`forecast_outputs/samples_frozen_20260910.jsonl`（3371 行）。
  sha256 必须 == `0d59f663a5deaf936157b1dfa54045d8b3f5e5c77465caac987f68868be87d34`，漂移即中止。
- 为什么用它：它是 `backtest_spread.load_samples()` 的落盘冻结件，读它 ⇒ **零网络**，
  不碰东财，符合铁律 7 第 1 项（零网络计算可全自动）。
- 冻结切分：`split_date_oos`（80% 分位 + label-end purge，max_horizon=5）
  ⇒ 实测 oos_start=2025-05-14，train 2438 / OOS 923 样本（306 个交易日）。
- WF 折：`build_wf_folds(window_days=63, max_horizon=5)` ⇒ 4 折（与既有 WF 报告同窗）。
- 特征：`forecast_engine.FEATURE_KEYS` 7 维 + B1 missing_mask = 14 维（生产原口径，不改）。
- 模型：`backtest_walk_forward._fit_one_horizon`（HGB max_iter=200/lr=0.08/depth=3，
  random_state=42，逐折 expanding 重训）——**用原函数原口径，不另立模型**。
- flat_margin 取 `config.forecast.prob_flat_margin`（当前 0.003）。
- 零网络：装载完立即上 socket 守卫（`run_t3_panel_power._guard_network` 同款），任何 connect 即 RuntimeError。
- 只读：不改 config / model_registry / 生产 .py / 定时任务；输出只写 `forecast_outputs/`。

## 三、三拆定义（与 C1 §三逐字同，保证定义可比）

设 OOS 预测集合 {(pred_i, y_i, date_i, member_i)}，member = 基金代码。秩相关一律 Spearman
（`backtest_forecast.rank_ic`）。

1. **pooled**：全体样本一次 rank_ic。现行口径原样（对照用）。CI = 按「日」整块有放回重抽样。
2. **CS（横截面）**：日宽 ≥ **4** 的日子逐日 rank_ic；主统计 = 日 IC 均值；CI = 对「日」重抽样。
3. **TS（时序）**：成员内 n ≥ **60** 的成员逐员 rank_ic；主统计 = 成员 IC 均值（并列中位数、正值占比）；
   CI = 对「成员」整块重抽样。

B = 1000，seed = 42，与 C1 同。

## 四、结构性局限（跑前声明，不得事后追认）

生产轨与 C1 的 D-lite 面板量级不同，以下三条是**输入决定的事实**，不是可调参数：

- 池子仅 4 只基金；OOS 306 日的日宽分布实测为 {2 只: 30 天, 3 只: 241 天, 4 只: 35 天}
  ⇒ **CS 口径（日宽≥4）只剩 35 个观测**，bootstrap CI 必然宽。
- TS 口径（n≥60）会剔掉 025687（OOS 仅 35 样本）⇒ **只剩 3 个成员**，
  正值占比阈值 2/3 在此等价「3/3 全正」，一步定生死。
- 因此本文结论**只能用于「登记 T+5 显著性的尺度归属」**，属证据契约的诚实补全。
  **禁止**据此升格或降格任何周期、禁止改动 `model_ready`、禁止改动动作层口径。
  任何"要不要上/撤 T+5"的裁决仍归 09-26 季度重估（cron `147825cf`）。

## 五、判据（跑前写死）

- **B1 横截面成立**：T+5 CS 日 IC 均值 95% CI 下界 > 0。
- **B2 时序成立**：T+5 TS 成员 IC 均值 > 0 **且** 正值成员占比 ≥ 2/3。
- **B3 pooled 膨胀登记**：pooled 的日块 CI 下界 > 0 而 B1、B2 **均**不成立
  ⇒ 记「pooled 膨胀」，说明 T+5 的显著性主要来自尺度混合（基金水平差 + 日期结构），
  而非任一可操作单尺度。README 须把 T+5 表述由「pooled 显著」细化为「pooled 显著，
  单尺度未分离出可操作信号」。
- **B4 单尺度归属**：B1、B2 恰有一个成立 ⇒ 只把成立的那个尺度写进 README 注脚，
  不得笼统写「T+5 有效」。
- **B5 双尺度皆成立** ⇒ 登记「T+5 两尺度均独立成立」，为 09-26 重估提供输入（本次不升格）。
- 敏感性：CS 改用日宽 ≥3（276 天）与 TS 保留 4 成员（不剔 n<60）各并列跑一遍，
  **只报数字不作裁决**（因门槛放宽等于降低标准，必须显式标注为敏感性而非主判据）。
- 可复现：T+5 全流程连跑两次，三拆全部主统计逐一经 ≤1e-9 一致，否则中止不产报告。
- 外部参照（非等值断言）：09-08 WF 报告 pooled T+5 = +0.089 CI[+0.034,+0.155]
  系 `load_samples()` 当日实时版（919 样本）所得；本次输入为 09-10 冻结版（923 样本）。
  两者**只作方向对照**，不要求逐位一致；若 pooled 符号相反，立即停手报告，不重跑凑数。

## 六、产出

- `forecast_outputs/c2_prod_decomp_20260918.json`（gitignore 区，本地留档）：
  逐端点三拆数字 + 敏感性 + 元数据（样本 sha、折序、oos_start、参数）。
- 人读摘要进 stdout，事后按本文口径誊入 README 与 Obsidian（誊录不得改判据解释）。

## 七、明确不做

- **不发任何东财请求**：持仓刷新、样本重采一律留待下一交易日 11:20 时间盒内、
  四项闸门（上游跑完 / 无频控征兆 / 无并行拉取 / 在时间盒内）全 PASS 且 Summer 明示后另跑。
- 不做 MDE / power / permutation（那是 T3 panel power 的职责，已裁决过）。
- 不做 RankIC 衰减曲线 / regime 切分 / 特征归因。
- 不修改 `backtest_walk_forward.py`、`backtest_rolling_oos.py` 及其口径；
  本脚本只做「读同一份冻结样本 + 调同一批函数 + 换三拆统计」。
- `rolling_oos` 腿本次不重复：其输入与 WF 同源（`load_samples`），
  零网络版等价物即本文件的冻结样本；重跑不改变三拆结论。
