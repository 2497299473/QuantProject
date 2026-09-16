# D-lite 面板受控批量刷新 · 2026-09-16T09:51:04

- 目标末根日期（最近已收盘交易日）：**2026-09-15**
- 面板成员：17 序列（gate 9 + relaxed 3 + fund 4 + sector 1）
- R3 硬停线：2026-09-16T11:20（此后零东财新请求）
- dry-run：否

| 层 | 代码 | 指令 | 缓存末根（前） | 动作 |
|---|---|---|---|---|
| gate | 159915 | market=0 | 2026-09-14 | 刷新 |
| gate | 160225 | market=0 | 2026-09-14 | 刷新 |
| gate | 501030 | market=1 | 2026-09-14 | 刷新 |
| gate | 510880 | market=1 | 2026-09-14 | 刷新 |
| gate | 512010 | market=1 | 2026-09-14 | 刷新 |
| gate | 512480 | market=1 | 2026-09-14 | 刷新 |
| gate | 512660 | market=1 | 2026-09-14 | 刷新 |
| gate | 512800 | market=1 | 2026-09-14 | 刷新 |
| gate | 512880 | market=1 | 2026-09-14 | 刷新 |
| relaxed | 159611 | market=0 | 2026-09-14 | 刷新 |
| relaxed | 159825 | market=0 | 2026-09-14 | 刷新 |
| relaxed | 515220 | market=1 | 2026-09-14 | 刷新 |
| fund | 002112 | fund | 2026-09-14 | 刷新 |
| fund | 002207 | fund | 2026-09-14 | 刷新 |
| fund | 022853 | fund | 2026-09-14 | 刷新 |
| fund | 025687 | fund | 2026-09-14 | 刷新 |
| sector | BK0457 | prod | 2026-09-15 | 跳过（已到目标） |

## 执行结果

| 层 | 代码 | 末根 前 -> 后 | 结果 | 说明 |
|---|---|---|---|---|
| gate | 159915 | 2026-09-14 -> 2026-09-15 | REFRESHED | src=tencent bars=3196 (1.5s) |
| gate | 160225 | 2026-09-14 -> 2026-09-15 | REFRESHED | src=tencent bars=2467 (1.1s) |
| gate | 501030 | 2026-09-14 -> 2026-09-15 | REFRESHED | src=tencent bars=2314 (1.1s) |
| gate | 510880 | 2026-09-14 -> 2026-09-15 | REFRESHED | src=tencent bars=3196 (1.3s) |
| gate | 512010 | 2026-09-14 -> 2026-09-15 | REFRESHED | src=tencent bars=3135 (1.4s) |
| gate | 512480 | 2026-09-14 -> 2026-09-15 | REFRESHED | src=tencent bars=1764 (0.7s) |
| gate | 512660 | 2026-09-14 -> 2026-09-15 | REFRESHED | src=tencent bars=2455 (1.2s) |
| gate | 512800 | 2026-09-14 -> 2026-09-15 | REFRESHED | src=tencent bars=2215 (1.1s) |
| gate | 512880 | 2026-09-14 -> 2026-09-15 | REFRESHED | src=tencent bars=2455 (1.2s) |
| relaxed | 159611 | 2026-09-14 -> 2026-09-15 | REFRESHED | src=tencent bars=1137 (0.6s) |
| relaxed | 159825 | 2026-09-14 -> 2026-09-15 | REFRESHED | src=tencent bars=1386 (0.7s) |
| relaxed | 515220 | 2026-09-14 -> 2026-09-15 | REFRESHED | src=tencent bars=1590 (0.7s) |
| fund | 002112 | 2026-09-14 -> 2026-09-15 | REFRESHED | _source=fresh (0.7s) |
| fund | 002207 | 2026-09-14 -> 2026-09-15 | REFRESHED | _source=fresh (0.4s) |
| fund | 022853 | 2026-09-14 -> 2026-09-15 | REFRESHED | _source=fresh (0.5s) |
| fund | 025687 | 2026-09-14 -> 2026-09-15 | REFRESHED | _source=fresh (0.3s) |
| sector | BK0457 | 2026-09-15 | SKIP | 已到目标日 |

- 发出请求的序列数：16
- 完成（刷新或已到目标）：17/17
- 是否停手：否（全部按计划完成）
- 结束时时刻：2026-09-16T09:51:51

> 本文件只记录数据层刷新；不含任何信号/持仓/建议措辞，不构成投资建议。
