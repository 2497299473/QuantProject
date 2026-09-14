# 运维变更：23:00 日汇总三条待拍板落地（2026-09-15 凌晨）

> 触发：2026-09-14 23:00 日汇总（重试补做）提出三条待拍板，Summer 深夜拍板「三点都按建议执行」。
> 本文件只记录调度基础设施与运维文档的改动；未运行任何 QuantV1 脚本、零东财请求、
> 未改任何生产 `.py` / `config.json` / Windows 计划任务；不构成投资建议。

## 一、三条拍板与执行动作

### ① D-lite 窗口与生产任务出网重叠 → 11:20 硬停线

- 落点：`output/forecast_lab_prereg_D_panel_20260914.md` **新增修订节 R3**（追加，不改原文；
  §六 T1~T3、§七 G2 阈值零改动）：
  - 受控一次批量刷新（17 序列 + 4 基金）**09:30 晨检后开跑，11:20 起不得再发任何东财新请求**
    ——同日 11:30 / 14:55（QuantFund_Mid / Post）与 16:00 / 21:30 / 22:30 / 23:00（cron）共用出口 IP；
  - 11:20 未完成 = 停手不重试不续跑（继承 §五 RATE_MARKERS 纪律），缺口顺延**下一交易日 09:30 后**
    受控补刷，计入 §八 预算；禁止「晚 21:30 后补刷」类临场改道。
- 同拍板顺带闭环 R2 之三：预注册文收编已于 09-14 提交 `35176c7` 落地，R3 内注明。

### ② 09:30 晨检未覆盖 [23:00 日汇总] → 加入核对清单

- 落点：OpenSquilla cron job `93439da7`（09:30 晨检）**原地更新**（`opensquilla cron update`，
  00:3x，job_id 不变）：
  - ① 小节清单 `[16:00 板块K线]、[22:30 Shadow]` → 加 **`[23:00 日汇总]`**；
  - 新增缺失处置：只读（`mode=ro`）查 `scheduler.db` 的 `scheduler_runs`，过滤 `ed7ae8c8`
    在目标日的行，区分「未触发」vs「已触发但失败（附 error 摘要）」；**晨检只提醒，不代跑、不补写**
    （顺延机制本身会补，09-14 已实证：汇总首跑被供应商过载打断，当日重试补做）；
  - ③ 一句话示例改「三节齐全」；纪律行同步「只读上述文件与 scheduler.db 只读查询」。
  - 调度（`30 9 * * 1-5` / Asia/Shanghai）、超时 600s / max_retries 3、feishu 投递 +
    failure_destination 均未动（update 返回体已核）。
- display name 改为简短标题（payload 正文即完整指令，与其余 job 的全文 name 风格不同，属可读性改进）。
- **回滚**：`C:\Users\Turn-\.opensquilla\workspace\_morning_check_job_rollback_20260915.json`
  （改前 name / payload / delivery / timeout / retry 全量快照；恢复 = `cron update --text <快照内原文>`）。

### ③ 预注册登记表文本滞后 → 补修订行

- 落点：`deploy/preregistered_items.md` 在 PR-20260914-01 修订记录**追加**一行（只增不改）：
  R2 之三收编已由 `35176c7` 落地 + 11:20 硬停线入 §已知约束（指向预注册文 R3）；
  本条状态维持 🟢 已拍板 / 未实现。

## 二、验证（无网络）

- 两份文档：UTF-8 无 BOM、CRLF 统一、原文段落逐字节保留（diff 只有追加块）、判据阈值字符串未动。
- 晨检 job：update 返回 `status=pending`、`next_run=2026-09-15T01:30Z`（09:30 +08）、
  delivery 仍 `channel/feishu oc_366e…a7d1`、`failureDestination` 在位。
- 明早 09:30 首个实跑即验证新清单（目标日 = 09-14，三节齐全，预期一句话返回）。

## 三、未做 / 边界

- 未改 `deploy/daily_summary_task.md`（23:00 任务指令无变化）；未新建/删除任何 cron job；
  未触碰 Windows 计划任务表、生产 `.py`、`config.json`。
- 待观察：11:20 硬停线是否够用——09-16 实跑若停线时未刷完，按 R3 顺延补刷并在当日运行记录留痕。
