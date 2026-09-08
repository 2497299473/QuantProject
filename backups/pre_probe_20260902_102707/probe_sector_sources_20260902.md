# 板块数据源复测 2026-09-02（netutil 修复后）

## A: 个股 → 行业（f127 申万行业名 / f128 地域板块，禁用于行业）
| 代码 | 名称 | f127 行业 | f128(地域,勿用) |
|---|---|---|---|
| 002112 | FAIL RemoteDisconnected:Remote end closed connection without response | - | - |
| 300502 | FAIL RemoteDisconnected:Remote end closed connection without response | - | - |
| 601398 | FAIL RemoteDisconnected:Remote end closed connection without response | - | - |
| 600519 | FAIL RemoteDisconnected:Remote end closed connection without response | - | - |
| 000001 | FAIL RemoteDisconnected:Remote end closed connection without response | - | - |
| 300750 | FAIL RemoteDisconnected:Remote end closed connection without response | - | - |
| 600030 | FAIL RemoteDisconnected:Remote end closed connection without response | - | - |
| 601088 | FAIL RemoteDisconnected:Remote end closed connection without response | - | - |
| 300760 | FAIL RemoteDisconnected:Remote end closed connection without response | - | - |
| 601899 | FAIL RemoteDisconnected:Remote end closed connection without response | - | - |

## B1: 东财行业板块列表（fs=m:90 t:2，注意：东财板块，非申万官方目录）
clist FAIL: RemoteDisconnected: Remote end closed connection without response

## B2: 行业板块 K 线深度（beg=20150101，验证 OOS 覆盖）
| 板块码 | 板块名 | K线根数 | 首根 | 末根 |
|---|---|---:|---|---|

## C: ETF K 线（项目 fetch_stock_kline 主链路，ttl=0 强制实时拉取）
| 代码 | 名称 | 实际源 | K线根数 | 首根 | 末根 |
|---|---|---|---:|---|---|
| 512480 | 半导体ETF | tencent | 1754 | 2019-06-12 | 2026-09-01 |
| 510300 | 沪深300ETF | tencent | 3196 | 2013-07-12 | 2026-09-01 |
| 159915 | 创业板ETF | tencent | 3196 | 2013-07-11 | 2026-09-01 |
| 512880 | 证券ETF | tencent | 2445 | 2016-08-08 | 2026-09-01 |
| 515080 | 红利ETF | tencent | 1619 | 2019-12-27 | 2026-09-01 |

== done ==
