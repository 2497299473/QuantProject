# D-lite 面板受控批量刷新 · 2026-09-26T10:06:08

- 目标末根日期（最近已收盘交易日）：**2026-09-24**
- 面板成员：17 序列（base gate 9 + relaxed 3 + fund 4 + sector 1 + D2a 追加 0）
- R3 硬停线：2026-09-26T11:20（此后零东财新请求）
- dry-run：否

| 层 | 代码 | 指令 | 缓存末根（前） | 动作 |
|---|---|---|---|---|
| gate | 159915 | market=0 | 2026-09-23 | 刷新 |
| gate | 160225 | market=0 | 2026-09-23 | 刷新 |
| gate | 501030 | market=1 | 2026-09-23 | 刷新 |
| gate | 510880 | market=1 | 2026-09-23 | 刷新 |
| gate | 512010 | market=1 | 2026-09-23 | 刷新 |
| gate | 512480 | market=1 | 2026-09-23 | 刷新 |
| gate | 512660 | market=1 | 2026-09-23 | 刷新 |
| gate | 512800 | market=1 | 2026-09-23 | 刷新 |
| gate | 512880 | market=1 | 2026-09-23 | 刷新 |
| relaxed | 159611 | market=0 | 2026-09-23 | 刷新 |
| relaxed | 159825 | market=0 | 2026-09-23 | 刷新 |
| relaxed | 515220 | market=1 | 2026-09-23 | 刷新 |
| fund | 002112 | fund | 2026-09-23 | 刷新 |
| fund | 002207 | fund | 2026-09-23 | 刷新 |
| fund | 022853 | fund | 2026-09-23 | 刷新 |
| fund | 025687 | fund | 2026-09-23 | 刷新 |
| sector | BK0457 | prod | 2026-09-24 | 跳过（已到目标） |

## 执行结果

| 层 | 代码 | 末根 前 -> 后 | 结果 | 说明 |
|---|---|---|---|---|
| gate | 159915 | 2026-09-23 -> 2026-09-24 | REFRESHED | src=tencent bars=3196 (2.2s) |
| gate | 160225 | 2026-09-23 -> 2026-09-24 | REFRESHED | src=tencent bars=2474 (1.2s) |
| gate | 501030 | 2026-09-23 -> 2026-09-24 | REFRESHED | src=tencent bars=2321 (1.2s) |
| gate | 510880 | 2026-09-23 -> 2026-09-24 | REFRESHED | src=tencent bars=3196 (1.5s) |
| gate | 512010 | 2026-09-23 -> 2026-09-24 | REFRESHED | src=tencent bars=3142 (1.6s) |
| gate | 512480 | 2026-09-23 -> 2026-09-24 | REFRESHED | src=tencent bars=1771 (0.8s) |
| gate | 512660 | 2026-09-23 -> 2026-09-24 | REFRESHED | src=tencent bars=2462 (1.2s) |
| gate | 512800 | 2026-09-23 -> 2026-09-24 | REFRESHED | src=tencent bars=2222 (1.3s) |
| gate | 512880 | 2026-09-23 -> 2026-09-24 | REFRESHED | src=tencent bars=2462 (1.2s) |
| relaxed | 159611 | 2026-09-23 -> 2026-09-24 | REFRESHED | src=tencent bars=1144 (0.5s) |
| relaxed | 159825 | 2026-09-23 -> 2026-09-24 | REFRESHED | src=tencent bars=1393 (0.9s) |
| relaxed | 515220 | 2026-09-23 -> 2026-09-24 | REFRESHED | src=tencent bars=1597 (0.9s) |
| fund | 002112 | 2026-09-23 -> 2026-09-24 | REFRESHED | _source=fresh (1.7s) |
| fund | 002207 | 2026-09-23 -> 2026-09-24 | REFRESHED | _source=fresh (0.5s) |
| fund | 022853 | 2026-09-23 -> 2026-09-24 | REFRESHED | _source=fresh (0.5s) |
| fund | 025687 | 2026-09-23 -> 2026-09-24 | REFRESHED | _source=fresh (0.5s) |
| sector | BK0457 | 2026-09-24 | SKIP | 已到目标日 |

- 发出请求的序列数：16
- 完成（刷新或已到目标）：17/17
- 是否停手：否（全部按计划完成）
- 结束时时刻：2026-09-26T10:06:57

> 本文件只记录数据层刷新；不含任何信号/持仓/建议措辞，不构成投资建议。
