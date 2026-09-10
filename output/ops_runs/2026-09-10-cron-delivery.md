# 运维变更：cron 投递链路修复 + 季度重估任务分阶段改造（2026-09-10）

> 触发：2026-09-10 23:00 日汇总发现「cron 汇报连续 2 日 `delivery failed`」，
> Summer 拍板「按你的建议执行」（三条建议全执行）。
> 本文件只记录**调度/投递基础设施**的改动，不涉及任何 QuantV1 业务脚本，不构成投资建议。

## 一、问题现象

- `scheduler_runs` 中多条任务 `success=0`，`error` 结尾一律是 `... delivery failed`：
  - `ed7ae8c8` 23:00 日汇总（本日）
  - `69b20a70` 22:30 Shadow（09-09、09-10 两日）
  - `93439da7` 09:30 晨检（本日）
  - `243d9df4` P0 收尾验收（09-09 三实例）
- 任务**实体本身跑完了**（产出文件齐全、小节完整），只有**汇报回不到 Summer 手上**——属「静默失败」。

## 二、根因（已定位，非猜测）

`logs/debug.log` 关键行：

```
opensquilla.scheduler.delivery: cron.reply_rendezvous job_id='ed7ae8c8-...' source='originating'
opensquilla.scheduler.delivery: delivery.webchat_forward_failed origin_session_key='agent:main:webchat:aubyp2tg'
  File ".../scheduler/delivery.py", line 256, in _deliver_origin_webchat_to_session
  File ".../session/manager.py", line 2059, in prepare_message
KeyError: 'Session not found: agent:main:webchat:aubyp2tg'
```

- 每个 cron 任务在**创建时**会把「创建它的会话」快照进 `delivery_json.originating_reply_target`
  （见 `scheduler/delivery.py: build_reply_rendezvous_envelope`）。
- 本批任务全部创建自 **webchat 临时会话**，其中有的是**嵌套 cron 运行会话**
  （如 `cron:10118c3d-...:run:2c5412be`）。这些会话早已销毁。
- 投递时因为快照是 webchat，走 `_deliver_origin_webchat_to_session` → 往那个会话 `append_message`
  → 会话不存在 → `KeyError` → `delivery_failed`。
- `gateway/routing.py: build_cron_route_envelope` 只从任务自身的 delivery 取目标，
  **没有**任何「配置级兜底」（`[channels.channels]` 里 feishu 的 `default_chat_id` 也是空的、
  且不参与 cron 路由）→ **唯一修法就是按任务修正投递目标**。

在册 6 个任务的原投递快照（全部为失效 webchat）：
`69b20a70 / 93439da7 → agent:main:webchat:co31bomk`；`ed7ae8c8 → agent:main:webchat:aubyp2tg`；
`1d99f4e7 → agent:main:webchat:eln7f3pu`；`fa6d9cdd → cron:10118c3d-...:run:2c5412be`；
`147825cf → cron:cfdb0938-...:run:786d5294`。

## 三、修复动作

1. **投递目标改为 feishu 实活会话**（6 个任务，仅改 `delivery_json`，任务正文/调度/超时全部不动）：
   - `mode: origin(webchat)` → `mode: channel`，`channel_name=feishu`，
     `channel_id=oc_366e320b4071d1e0577216e67840a7d1`
   - `originating_reply_target` 同步改写为 feishu（否则仍会走 webchat 分支）
   - 新增 `failure_destination` → 同一 feishu 会话：**任务失败（超时/异常）也会告警**，
     解决「静默失败」这一类问题，而不只是这次的投递失败
2. **09-26 季度重估任务分阶段改造**（研究类长任务与生产任务分开调度）：
   - 超时 `600s → 3600s`（触发在 10:00，远离 16:00 / 22:30 生产窗口）
   - 重试 `3 → 1`（整条管线重跑会重复抓取东财，禁止三连跑）
   - 正文改为引用新 runbook `deploy/quarterly_review_task.md`（提交 `12168de`），
     内含「先冻结后复算」口径 + 阶段划分 + 断点续跑规则
3. **生产任务不动**：09:30 晨检 / 16:00 板块K线 / 21:30 补拉 / 22:30 Shadow / 23:00 日汇总
   仍为 `600s / max_retries=3`——保留「超时＝停手防频控」这条防线。

## 四、验证证据（端到端，非推断）

临时建 agent_turn 探针任务（`2c34ba74`），按同样方式改投递后手动触发，随后删除：

- `scheduler_runs.delivery_status = 'delivered|ws:skipped|fwd:skipped'`，`success=1`
- `channel_outbox` 新增 `send_id=c97c7c5303f8454eaeed094703fcf389`，
  `channel_name=feishu`，`target_id=oc_366e320b4071d1e0577216e67840a7d1`，
  `state=sent_unconfirmed`（与历史成功投递同状态），内容即探针正文

→ 触发 → 执行 → 回投 整条链路已打通。

## 五、回滚路径

- `C:\Users\Turn-\.opensquilla\workspace\_cron_delivery_rollback.json`（6 任务改前 `delivery_json`）
- `C:\Users\Turn-\.opensquilla\workspace\_quarterly_job_rollback.json`（09-26 任务改前 name/payload/timeout/retry）
- 复现脚本：`_fix_cron_delivery.py`、`_fix_quarterly_job.py`（无参数=干跑，`--apply`=写入）
- runbook 可 `git revert 12168de`

## 六、遗留 / 待观察

- 「任务实体跑完但汇报未达」这类静默失败此前**没有任何自检**；本次靠 23:00 日汇总才发现。
  现有缓解：`failure_destination` 告警 + 日汇总每日核对 `scheduler_runs`。
- 09-08 / 09-09 的历史 `delivery_status` 字段为空（该列是后加的），无法回溯当时的投递成败。
- 09-26 任务改造后**尚未经过一次实跑验证**（下次真实触发是 09-26）；首个可验证点是
  09-11 的 09:30 晨检与 16:00 板块K线——它们会走修复后的投递链路。
