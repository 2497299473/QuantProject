# QuantV1 季度证据重估（09-26 预注册点）任务正文

- 创建：2026-09-10（Summer 拍板「按你 09-10 日汇总的三条建议执行」）
- 形式：cron 任务正文只写守卫 + 读取本文件执行；本文件是完整指令（与 `daily_summary_task.md` 同惯例）
- 为什么存这里：`QuantV1/deploy/` 是部署与运维文档目录，git 内可追溯
- 本任务性质：**研究类长任务**——与生产四/五条（09:30 晨检、16:00 板块K线、21:30 板块K线补拉、22:30 Shadow、23:00 日汇总）分开调度：
  - 触发时刻 09-26 10:00（Asia/Shanghai），远离 16:00 / 22:30 生产窗口
  - 超时预算 3600s（生产任务仍是 600s，那条「超时＝停手防频控」的防线不动）
  - 重试 1 次（不是 3 次）——整条管线重跑会重复抓取东财，禁止三连跑

## 第零步 日期守卫

`Get-Date` 取今天。**只在 2026-09-26 当天执行**。09-26 是预注册固定日历日（v7 笔记写死，不得改成「每月最后一个工作日」）。
不是 09-26 → 只回一句「季度重估任务延迟触发（实际日期 X），未执行任何写操作」，不跑任何脚本，结束。

## 核心口径变更（2026-09-10 拍板，必须执行）

**一律用「同冻结样本当次复算值」做门槛，不再引用历史绝对值。** 依据见
`output/forecast_lab_p1_20260910.md` §六/§七：同一冻结样本上用生产函数本体复算 v7 基线，
T+5 = **−0.0324**，而 09-08 WF 报告写的是 **+0.089**（漂移 −0.121）。已定位机制（非猜测）：
`data/stock_klines` 240 只缓存中 171 只于 09-10 09:48 冻结时因 TTL 到期被重取；东财 K 线是
**前复权**价，重取会追溯性改写历史价量 → 历史 `est_chg` / `composite` / `score` 跟着变，
而 `est_chg` 是 v7 七特征中最重要的一列 → 各折 IC 整体位移。

结论：**v7 的 WF 基线在语义上不是「同一把尺子」**——冻结时点不同，基线就不同。
所以 09-01 的 rolling pooled +0.080、09-08 的 WF pooled +0.089 **只作为存档与漂移叙事**，
**不得**作为本次判定门槛。

## 分阶段执行（每阶段独立，目标各自 <600s；支持断点续跑）

统一执行形式：`Set-Location -LiteralPath 'D:\PythonProject\QuantV1'` 后用
`.\.venv\Scripts\python.exe -X utf8 <脚本>`（PowerShell 语法）。

### 阶段 0 — 前置闸

1. 净值 / K 线缓存完整性检查；**缺年即补拉**（08-31 教训）。这是本任务唯一允许的联网抓取，
   且只能在 10:00–11:00 时段做，避开 16:00 / 22:30 生产窗口。
2. 期间若出现 `DegradedResponse` 或任何频控征兆 → 立即停手、记录、等下一天续跑，
   **禁止重试第二轮**。

### 阶段 1 — 样本冻结（先冻结，后复算）

产出 `forecast_outputs/samples_frozen_YYYYMMDD.jsonl` + sha256 + meta，并同时产
`forecast_outputs/kline_fingerprint_YYYYMMDD.json`（逐标的 `(date, close)` 全序列 canonical
sha256 → 聚合总指纹；`freeze_samples.py` 已接入）。记录：样本数（预期 ~3343）、四基金分布、
样本 sha256、K 线聚合指纹（stock/fund 计数）。

### 阶段 2 — 基线复算（与阶段 1 同一冻结样本）

用**生产函数本体**在同一冻结样本上复算 `existing_v7`（T+1/T+3/T+5 pooled WF RankIC + CI）。
**本次判定门槛 = 这个值**。阶段 1 的样本 sha256 与 K 线指纹必须写进报告，作为「两次冻结是否等价」的证据。

### 阶段 3 — 当期评估管线

依次跑：`backtest_forecast.py` → `backtest_rolling_oos.py --window-days 63 --n-boot 199` →
`backtest_walk_forward.py --window-days 63 --n-boot 199` → `drift_monitor.py` → `shadow_policy.py`

### 阶段 4 — 数据源与样本质量

1. 数据源稳定性：`push2his` HTTP 通道在 Windows 原生下的结果与旧 WSL 结果对比。
2. Market Context 首批样本质量：读 `data\market_context\history.jsonl`，按
   `usable_for_oos` / `usable_for_oos_reason` / `proxy_switch_themes` 统计。

### 阶段 5 — 报告与留痕

完整报告**追加**写入 `D:\PythonProject\QuantV1\output\daily_runs\2026-09-28.md`，小节标题
「[09-26 季度重估]」。

> 文件名说明（2026-09-09 拍板，勿改）：原定写 `2026-09-26.md`，但该文件已存 09-08 提前跑的旧版报告，
> 当天真跑若仍写同名，会误以为当天没跑或与旧版混淆；故固定写 `2026-09-28.md`。

报告必须含：关键数字（同冻结样本的 v7 复算基线、最近窗 T+5 RankIC 与 CI、逐折、
drift、shadow 的 ADD/REDUCE/HOLD 计数）、样本 sha256 与 K 线指纹、FAIL 码、是否走兜底、
一句话结论。

另：对照并补记 Obsidian 笔记
`D:\Obsidian\My-First-Obsidian\量化交易工具\基金日频参谋-v7-诚实口径-20260829.md` 附录 4。

## 断点续跑规则（重要）

若本次在某阶段被超时掐断：**下次触发或手动续跑时，先读 `2026-09-28.md` 小节里已完成的阶段标记，
从下一个未完成阶段继续，禁止整条管线重跑**（重复抓取会打挂生产通道）。
每个阶段结束时，先把该阶段的完成标记与关键数字追加进报告文件，再进入下一阶段。

## 决策规则（预注册，不得事后改动）

- 最近窗 T+5 转正**且** pooled 显著（相对阶段 2 的同冻结样本基线）→ 把「分周期 ready 位」方案列为**待 Summer 拍板**；
- 持续衰减 → 维持 `model_ready=false` 并记录，**不改 config**。

## 纪律与铁律

- 观察层 v0.1 口径已冻结至满 60 个有效交易日，复核期间**不改任何指标定义**。
- 不改 `config.json` / `model_ready` / 生产 `.py`（`experiments/forecast_lab/` 下的研究脚本除外）。
- 提交时只 `git add` 自己新建/改动的具体路径，**严禁 `git add -A`**（仓内有并行会话 WIP）。
- 不新建周期性任务、不重注册 Windows 计划任务。
- 输出不构成投资建议。
