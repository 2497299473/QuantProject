# evidence/ · 索引

每条证据一行。**状态**列：`FRESH`（可复现、输入在档）/ `STALE`（输入或口径已变，仅历史参照）
/ `SUPERSEDED`（被后续轮次取代）。

## validation/ · 验证报告

| 日期 | 主题 | 报告 | 元数据 | 输入 | 状态 | 裁决 |
|:--|:--|:--|:--|:--|:--|:--|
| 2026-09-23 | backtest_forecast（三周期 OOS） | `validation/backtest_forecast_0910frozen_20260923.log` | `…meta.json` | 0910 冻结件（G-A PASS / G-B DRIFTED） | FRESH | T+1/T+3/T+5 全 ❌（rank_ic +0.049/+0.048/−0.019，CI 全跨零）；model_ready 维持 false；**未绑 registry** |

### 说明

- 本件是 `evidence/` 目录建立后的**第一份**落档验证报告（V4.5，2026-09-23）。
- **补的是哪条断链**：`registry.json` 里两个模型绑定的验证报告
  （`backtest_forecast_v8_quantile_20260830.log`、`backtest_forecast_v3_b1mask_20260831.log`）
  在本地与 git 历史中**都不存在**，审计 `P1-2` 因此常年 FAIL。本件不改那两个历史绑定
  （改绑会覆盖现行授权链），只保证**从今往后**每轮验证都有在档原件。
- **历史绑定怎么办**：~~`P1-2` 的处置需 Summer 拍板（重跑并改绑 / 把历史绑定降级为
  档案事实）。在拍板前，该 FAIL 如实保留，不粉饰。~~
  **已处置（2026-09-30，Summer 裁决 D3-07 = 选项 2「降级为档案事实」）**：
  registry 两条目（forecast_v2/v3）的 `validation` 块打 `archived: true` 标记
  （含 `archived_reason` / `archived_at` / `archived_authorized_by`，其余字段含
  `report_sha256` 值逐字节不动——prereg D3 判据读的是该值，shadow 降级授权链
  不受影响，已实证 `prereg_degradation` 仍 True）；`audit_project.check_validation_binding`
  同步增分档：archived 条目的原件缺失 → WARN（detail 带「已处置档案」），
  **未打标的缺失/sha 不符仍 FAIL**（删标记即恢复红灯）。落地后 P1-2 转 WARN，
  `audit_health` 转 PASS_WITH_WARNINGS。
  **打标权红线**：`archived` 仅限 Summer 授权批次可打（本批授权记录 = 本节 +
  registry `archived_authorized_by` 字段 + commit message）；测例钉死「active 条目
  带 archived ⇒ P1-13 derive_promotion 推导不受扰」，防标记被滥用为绕过授权门
  可见性的通道（`tests/test_audit_hygiene.py::TestP12ArchivedDisposition`，11 条）。
  代价如实记录（A 单核实）：active model（forecast_v3）的报告缺失同降 WARN——
  「active 原件在档」的强制力从审计 FAIL 转移给「打标须 Summer 授权」纪律。

## probes/ · 探针与对照实验

| 日期 | 主题 | 文件 | 状态 | 备注 |
|:--|:--|:--|:--|:--|
| 2026-10-03 | 面 8 D8-05 批 4 自测（_pw argparse 化 + 重启封顶 MAX_RESTARTS=2） | `probes/face8_d805_pw_tests_2026-10-03.log` + `probes/face8_fast_layer_batch4_2026-10-03.log` | FRESH | 新增 test_pull_sector_klines_pw（fast）**7/7 OK**：--help 退出码 0 且 0 次 launch（拆掉「--help 当日期触发全量 65 码」事故载体，10-02 01:35 同型实录）；裸位置参数/未知旗标 → argparse 码 2 拒绝（旧路径死亡）；--date 显式路径行为一致；连败重启封顶 launch≤3=1+MAX_RESTARTS、报告 aborted=True；假 playwright 注入（venv 无 playwright），0 真实浏览器 0 网络；修复过程发现 Set-Content 写入带 BOM → test_layers ast.parse 报错（U+FEFF），已去 BOM 后 fast 全层 **891 OK** 零回归 |
| 2026-10-03 | 面 8 D8-01/02(pull侧)/09(报告) 批 3 自测（休市守卫上移 + 熔断整轮中止 + wire_attempts 报告） | `probes/face8_d801_d802_pull_tests_2026-10-03.log` + `probes/face8_fast_layer_batch3_2026-10-03.log` | FRESH | 新增 test_pull_sector_klines（fast）**19/19 OK**：休市判定 5 用例（周末/法定/正常/表缺 fail-open）、主脚本休市日 0 请求+报告「休市跳过」、ThrottleSuspected 整轮中止（主/evening 各 1）+ 普通 FAIL 不中止对照、fetch_and_store throttled 标记、evening 本地 _is_trading_day 旧路径死亡（源码+属性双断言）、wire_attempts delta 口径（绝对值不误报）；全程打桩 fetch_and_store + tempdir，**未运行脚本本体**（铁律 8）；fast 全层 **884 OK**（865+19，零回归） |
| 2026-10-03 | 面 8 D8-03/07 批 2 自测（fund_sina total_num + tushare 截断 fail-closed） | `probes/face8_d803_d807_provider_tests_2026-10-03.log` + `probes/face8_fast_layer_batch2_2026-10-03.log` | FRESH | 两 provider 测试 **44/44 OK**（含 7 新用例：sina 截断 got/total、容差不假红、缺失退化 MIN_ROWS；tushare 满页后空页截断、MAX_PAGES=10 兜底、正常末页不受影响）；fast 全层 **865 OK**（858+7，零回归）；total_num 路径未核实（无落档原始响应）→ 防御式多候选读取，解析不到退化现状防线不假红 |
| 2026-10-03 | 面 8 D8-02/06/09 批 1 自测（netutil v6 熔断器/prime 退避/wire 计数） | `probes/face8_d802_d806_d809_breaker_tests_2026-10-03.log` + `probes/face8_d809_netutil_loopback_2026-10-03.log` + `probes/face8_fast_layer_batch1_2026-10-03.log` | FRESH | 新增 test_netutil_breaker（fast，全 mock 零网络零 DNS 零建连）**14/14 OK**；test_netutil 扩 wire 用例（loopback 本地 http.server，零外网）**12/12 OK**；fast 全层 **858 OK**（844 既有+14 新增，零回归）；熔断验收：K=8 后同 host 0 建连快速失败、4xx/ECONNREFUSED 不计数、TTL 半开、成功清零 |
| 2026-10-03 | 面 8（网络数据域）Phase 1 基线指纹（Agent B，commit `baab86c`） | `probes/face8_baseline_fingerprint_2026-10-03.log` | FRESH | 在审 13 文件 + providers 7 文件 sha256 基线；纯只读采集（未运行任何 pull*/probe* 脚本，含 --help），供 Phase 2 期间自证与 A 核验「无基线漂移」；git status 确认工作区改动全部位于 data//output/，在审代码零未提交改动 |
| 2026-10-01 | 面 6 测试语义修复 · 终态自测（commit `13dbfca`） | `probes/face6_fast_final_2026-10-01.log` + `probes/face6_all_final_2026-10-01.log` + `probes/face6_selftest_summary_2026-10-01.meta.json` | FRESH | fast **844 OK (skipped=0)**；--all **1072 FAILED (failures=1, skipped=3)**——唯一失败 test_no_heavy_import_side_effect 为基线固有（下方 stash 同环境对照证明，归跨域 backlog）；summary meta 含七组注入验证归因与产品码零改动证明 |
| 2026-10-01 | 面 6 · 终态自测的中间态留档（after_fix） | `probes/face6_fast_after_fix_2026-10-01.log` + `probes/face6_all_after_fix_2026-10-01.log` | SUPERSEDED（被上行 final 取代；字节数相同 46856/268560，sha 仅差计时串，已复核） | fast 844 OK / --all 1072 FAILED(failures=1, skipped=3)，结论与 final 一致 |
| 2026-10-01 | 面 6 · --all 失败归因对照（stash 同环境基线） | `probes/face6_all_stash_baseline_2026-10-01.log` | FRESH | stash 本轮改动后 **1064 tests 同样 failures=1** 且失败点相同 ⇒ 该失败为基线固有、非本轮引入（+8 用例零新增失败） |
| 2026-10-01 | 面 6 · worktree 基线对照（环境不等价，仅存档） | `probes/face6_all_baseline_proof_2026-10-01.log` | SUPERSEDED（被上行 stash 同环境版取代；worktree 缺 gitignored 数据缓存致 skipped=9、test_reads_latest_frozen_file 假红） | 1058 tests failures=2，其中 1 条系环境不等价——不作归因依据 |
| 2026-10-01 | 面 6 · 注入验证（B-3/B-4/C-1/C-4/C-6 负向对照） | `probes/face6_inject_b3_guard_removed_2026-10-01.log` + `probes/face6_inject_b4_return2_dead_branch_2026-10-01.log` + `probes/face6_inject_c1c4c6_guards_removed_2026-10-01.log` | FRESH | 守卫短路后对应用例如期变红（B-3 prereg 钉住拒写 / C-1 锚点 sha / C-4 状态未知 HOLD）；B-4 证明字符串断言测不到语义、行为断言变红；全部注入已还原 |
| 2026-10-01 | 面 6 · C-6 G-A sha 门隔离探针（真实/注入/还原三态） | `probes/face6_c6_sha_gate_isolation_probe.py` + `probes/face6_c6_probe_real_2026-10-01.log` + `probes/face6_c6_probe_injected_2026-10-01.log` + `probes/face6_c6_probe_restored_2026-10-01.log` | FRESH | 「行数不变的内容篡改」：真实代码 exit=3（抓到）/ 注入 ga_ok=True 短路 exit=0（逃逸）/ 还原后 exit=3 ⇒ sha 门是唯一拦截者，负向测试真实 |
| 2026-10-01 | 面 6 · 分层清单与 --list 磁盘核对 fail-closed 验证 | `probes/face6_layers_list_2026-10-01.log` + `probes/face6_list_diskcheck_ok_2026-10-01.log` + `probes/face6_list_diskcheck_inject_2026-10-01.log` | FRESH | --list 常态 64 文件（fast 48/slow 16）exit=0；注入未登记 test_zz_scratch.py ⇒ exit=1 + 点名报告，如期红灯 |
| 2026-10-01 | 面 6 · 验收基线留档（commit `72f8fbc`） | `probes/face6_commit_list_2026-10-01.log` | FRESH | 2089d54..HEAD commit 清单 + diff --stat + 产品码零改动证明 |
| 2026-09-30 | D3-07/08 落地自测（archived 档案化 + sha 口径修复） | `probes/d3_batch_selftest_20260930.log` + `probes/d3_batch_selftest_20260930.meta.json`（同批重生成；探针新增 `probes/d307_archive_probe.py`） | FRESH | P1-2 由 FAIL 转 WARN（已处置档案 2 条），audit_health=PASS_WITH_WARNINGS；授权链零扰动实证（prereg 降级仍 True / verify_validation_report 仍拒 / derive 仍 blocked / pinned sha 未动）；D3-08：log_sha256 改为对落盘字节计算（修 Windows CRLF 假 sha，已独立复核 MATCH）；fast 781 OK / slow 222 OK(skipped=3)，含 P1-2 新测例 11 条 |
| 2026-09-30 | 面 3（证据与血缘）D3 批修复自测（D3-01~06；D3-07 待裁决） | `probes/d3_batch_selftest_20260930.log` + `.meta.json`（收集器 `d3_collect_selftest.py`，探针 `d3_recompute_probe.py` / `d3_backfill_probe.py`） | SUPERSEDED（被上行同批重生成取代；首版 log_sha256=3ede9cb6… 为字符串口径，即 D3-08 所修缺陷的实例） | D3-01 配对锚点+独立复算门（真实 forecast_v3.pkl + 0910 冻结件端到端：诚实件误差 0 / 篡改必拒）；D3-02 快照自洽校验；D3-03 first_identical_commit 三类测例+可达性过滤；D3-04/05 meta 畸形 fail-closed 成对测例；D3-06 pkl 身份全长比对（回填逐字节不变）。fast 770 OK / slow 222 OK(skipped=3) / audit 18 PASS·1 FAIL（P1-2=D3-07 既有项）·4 WARN |
| 2026-09-30 | 面 5 生产路径/编排加固自测（commit `2089d54`） | `probes/d5_fast_after_20260930.log` + `probes/d5_slow_after_20260930.log` | FRESH | fast **838 OK** / slow **226 OK (skipped=3)**；覆盖 D5-A1 资格标注、B1 幽灵 +4 原发位置死亡、B2 新鲜度防线、B3 阈值同源、D1-D3 净值新鲜度、F2 drift_monitor 双旗标守卫等 |
| 2026-09-30 | 面 5 · 代码修复过程中的中间态预测试 | `probes/d5_fast_codefix_pretest_20260930.log` | SUPERSEDED（被上行 after 取代；793→838 用例） | fast 793 OK——修复进行中的阶段性预测试，非终态口径 |
| 2026-09-30 | 面 4 PIT D-07 est_chg 误差带探针 | `probes/d07_est_chg_error_band.py` + `probes/d07_est_chg_error_band_20260930.json` | FRESH | 冻结池 n=3371：mean_abs 0.0063 / p90 0.0145 / max 0.1446（err=est_chg−a_T）；live fraction 轨 **PROVISIONAL**（切换日后仅 5 交易日<30，满 30 日后重跑本探针刷新——零网络） |
| 2026-09-30 | 面 4 · D-02/D-03 修复前后 WF 对照（0910 冻结件） | `probes/d4_wf_baseline_20260930.log` + `probes/d4_wf_after_d02d03_20260930.log` | FRESH | G-A PASS（sha=be8e55f02b39…）/ G-B DRIFTED 原样记录；Path-WF 修复前后数字不变（hit mdd10=9.5% / mfe50=29.6%）、判定 wf_fail——修复不动口径的证明 |
| 2026-09-29 | 审计 D-04/D-08 裁决落地自测 | `probes/audit_d04_d08_selftest_20260929.log` + `probes/audit_d04_d08_selftest_20260929.meta.json` | FRESH | P1-5 改 AST 结构判据（import 代价实测 18× 为裁决依据）；D-08 维持下游拦截零逻辑改动；fast 703 OK / slow 188 OK(skipped=3) / audit stderr 0 字节；P1-2 FAIL 与 P1-9 WARN 均为既有项非本批引入 |
| 2026-09-29 | 面 2 registry malformed fail-closed 自测（commit `25d3817`） | `probes/d_batch_face2_selftest_20260929.log` + `probes/d_batch_fast_layer_20260929.log` | FRESH | fast **723 OK**；--all 914 FAILED(failures=1) 唯一失败为 loader 顺序既有伪影（worktree 基线 891 同失败、隔离运行 OK，见 log [4] 节）；D-02 mutation 演示：mask_value 1.0→2.0 ⇒ 3 tests 红、复原后 12 OK；d_batch_fast_layer 为 fast 层原始输出 |
| 2026-09-29 | 面 2 · D-01 malformed 探针前后对照 | `probes/d01_malformed_probe.py` + `probes/d01_probe_prefix_589dc22.log` + `probes/d01_probe_postfix.log` | FRESH | prefix=修复前 `589dc22`：类型污染（'abc'/'1'/True）被放行；postfix=修复后全部 REJECTED（malformed_registry_protocol_version 等）——旧路径死亡证明 |
| 2026-09-28 | 审计 D 批修复自测（D-01/02/03/05/06/07/09） | `probes/audit_d_batch_selftest_20260928.log` + `probes/audit_d_batch_selftest_20260928.meta.json` | FRESH | fast 692 OK / slow 188 OK / audit 零 stderr；P1-2 FAIL 为既有预留项非本批引入 |

> 落档核对（2026-10-01，D7-04）：`git ls-files evidence/probes` 的 **40/40** 个文件已全部在上表登记；
> 探针输出**已全部迁入**（40 文件入库）。原「探针输出仍在 output/（gitignored），后续按需迁入」占位行
> 已被面 2-6 批次的实际入库证伪，按审计裁定删除。

## incidents/ · 事故取证

| 日期 | 事件 | 文件 | 状态 | 备注 |
|:--|:--|:--|:--|:--|
| 2026-09-23 | 14:55 post 轮证据链断档（报告生成前抛异常） | 待补 | OPEN | 已由 V4.5 FAILED-manifest 兜底修复；原始日志 `output/logs/run_20260923_145504.log`（本地） |
