# 运维变更：D-lite 开跑窗口任务对齐预注册 R3 时间盒（2026-09-15 上午）

> 触发：09-15 09:30 晨检复盘 09-14 运行记录时，发现 09-14 23:00 日汇总三条待拍板虽已于 09-15 00:36
> （`dbf71c2`）在**文档层**落地，但 09-16 那条一次性任务（cron `7b8e2963`）的正文仍写「建议 08:00–15:00
> 内做完」「面板刷新尽量避开」，与预注册 R3 写死的硬时间盒不一致——属同一类「文本滞后于判据」缺口。
> 本文件只记录调度基础设施改动；未运行任何 QuantV1 脚本、零东财请求、未改生产 `.py` / `config.json` /
> Windows 计划任务表；不构成投资建议。

## 一、改动内容

`7b8e2963-31b0-46e4-a140-ceb4a8225b54`（`at 2026-09-16T08:00:00+08:00`，`payloadKind=reminder`，agent=main）
**原地 update**（job_id 不变）：

1. **正文写入 R3 硬时间盒**（`output/forecast_lab_prereg_D_panel_20260914.md` §R3 之二/之三）：
   - 08:00 起跑段只做零网络准备（读 §九 + R2 + R3 前提、核对三条勾选、检查频控征兆），此段不得发东财请求；
   - 受控一次批量刷新（17 序列 + 4 基金净值）**09:30 晨检跑完后开跑**，**11:20 起不得再发任何东财新请求**
     （避让同日 11:30 QuantFund_Mid / 14:55 QuantFund_Post / 16:00 / 21:30 / 22:30 / 23:00 共用出口 IP）；
   - 11:20 未完成 = 以已完成部分停手（继承 §五 RATE_MARKERS 纪律），当日不重试不续跑，缺口顺延
     **下一交易日 09:30 后**受控补刷并计入 §八 预算；禁止「当晚 21:30 后补刷」类临场改道；
   - 11:20 之后的 T1 / T2 为零网络，照 §六 执行，不受时间盒影响。
   - 原文的「08:00–15:00 内做完」与软性「尽量避开」措辞删除（与 R3 冲突）。
2. **display name 改为短标题**：「【QuantV1 · D-lite 面板开跑窗口】09-16 · 受控刷新 09:30 后开跑 /
   11:20 硬停线」（原 name 为正文全文重复，`cron list` 不可读；沿用 09-15 00:3x 晨检 job 的可读性口径）。
3. **未动**：`schedule_raw`（2026-09-16T08:00:00+08:00）、`tz`、`timeout_seconds`（600）、`max_retries`、
   `delivery`（origin / `webchat:agent:main:webchat:l14liiui`）、`payloadKind`、`enabled`。

**回滚**：`C:\Users\Turn-\.opensquilla\workspace\_dlite_job_rollback_20260915.json`（update 前
`cron status --json` 全量快照）；恢复 = `opensquilla cron update <job_id> --text <快照 text> --name <快照 name>`。

## 二、为何保留 08:00 触发而未移到 09:35

方案 A 的拍板原文是「**维持 08:00 起跑** + 11:20 硬停线」；R3 的收紧点是**网络行为**（受控刷新 09:30 后
开跑、11:20 起零新请求），不是任务的触发时刻。故按「08:00 起跑做零网络准备 + 网络窗口 09:30–11:20」
落地，两处口径同时满足。若 Summer 要纯对齐，只需一条 `--at 2026-09-16T09:35:00+08:00`，判据不受影响。

## 三、验证（无网络）

- update 返回体逐字段核对：job_id / `schedule_raw` / `next_run`（2026-09-16T00:00:00Z = 08:00 +08）/
  delivery / timeout 均未变；正文与源文件逐字符一致（1085 字符，CRLF）。
- `cron status --json` 复核：`status=pending`、`enabled=true`、`run_count=0`（尚未跑过）。
- 本次调用只写调度库与本地文件，未触发任何数据脚本或网络请求。

## 四、未做 / 边界与待观察

- 未改 `deploy/` 下任何任务指令文；未动 09:30 晨检、16:00、21:30、22:30、23:00 五条作业；未新建/删除任何
  cron job；未触碰 Windows 计划任务表、生产 `.py`、`config.json`。
- **待观察（已报未改）**：该 job 投递为 origin webchat 会话 `agent:main:webchat:l14liiui`（09-14 创建时
  的会话），非生产用 feishu 群。CLI 明确 primary delivery 不可 patch，改动需 remove + re-add（会换 job_id），
  故本次仅报不改，等 Summer 决定是否改投 feishu。
- **待观察**：11:20 硬停线是否够用——09-16 实跑若停线时未刷完，按 R3 顺延补刷并在当日运行记录留痕。
