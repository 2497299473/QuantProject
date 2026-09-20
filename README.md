# 基金日频参谋 V4 · 多周期走势参谋（4.0.0-decision 引擎 + forecast v3 多周期条件分布预测）

> 场外开放式基金日频参考系统。**只出参考建议，绝不自动下单。**
> 依据：[[基金日频参谋-v2-重建记录-20260820]] · [[基金日频参谋-回测校准-20260822]] · [[基金日频参谋-v3-融合整合记录-20260823]] · [[基金日频参谋-v4-决策引擎重构-20260825]]
> 主线：QuantTestByGLM（v2.3.0-glm → v4.0.0-decision → **v5.0.0-forecast**，2026-08-26 引入多周期条件分布预测；纯 Python 标准库 + 可选 sklearn，无需 pip install 即可跑通核心）。

## V4 总原则（护栏 · 2026-09-16 拍板）

> **① 生产目标永远是 4 只实际基金的决策质量；扩展代理池只用于提高研究功效与机制诊断，不能替代生产基金验证。**
>
> **② 任何 pooled / proxy / relative endpoint 的显著结果，都不得直接解释为生产基金绝对收益预测能力。**

这两条写进顶层，是为了防住 D-lite / 候选 A 暴露出的那一类漂移：研究池成功率 ≠ 生产基金可用性。
> 融合：2026-08-23 单主干整合——以 GLM 版为骨架，吸收 quant_test 母本的账户面均值项（μ₂₀）、数据降级告警链路（_source）、缠论代理K线实验区（experiments/）。
> 穿透升级：2026-08-23 对齐 `book-to-skill/chanlun` skill（动力学口径 + 全量形态学 + 防狼术）
> v4 重构：2026-08-25 吸收 GPT-5.6 诊断 + 项目回测铁律融合——日内特征引擎 / 11:30→14:55 快照变化量 / 决策倾向引擎（五维加权 + 四道门槛）/ 动作收益回测。动作层经 `backtest_action.py` 裁决未通过 → `history_validated=false` 锁死，只输出倾向分与候选动作，实际动作恒为「保持不动/观察」。

## Project State（2026-09-08 · 迁移 Windows 原生 · Market Context v0.1 封版）

- 代际命名（V4 · 2026-09-16 拍板）：**V4 = 证据链闭环代（Evidence-Contract Generation）**。本行为代际名的**唯一权威落点**。三轴并存、互不覆盖：**Project Generation: V4** / **Engine Version: 4.0.0-decision** / **Forecast Model Version: 3（协议 B1）**。`config.json` 的 `version` 字段被代码引用，**不改**（改它不属于 bug 修复，违反 v0.1 冻结纪律）。历史代际 `V3 = Windows Native Generation / 数据架构重构代` 已封版；文档中**不再使用「README 标题 v5」这类代际混淆写法**。

- 运行环境: **Windows 原生**（2026-09-08 自 WSL Ubuntu 迁至 `D:\PythonProject\QuantV1`；项目自带 `.venv` Python 3.12.13，依赖版本与 WSL 侧锁死一致（见 `requirements.txt`）；全量单测入口见「快速开始」，不写死项数，forecast_v3.pkl 已实测 `load_models()=True`）
- Forecast model version: **3**（B1 双列 14 维，MODEL_VERSION 闸）
- Production status: **BLOCKED**（model_ready=false，动作层 history_validated=false 锁死）
- T+5 方向: **UNRESOLVED（V4.3 P0-4 证据清理，2026-09-20）**（当前结论 = 09-18 C2/CTRL WF 部署式 pooled RankIC **-0.079** CI[-0.158,-0.016]，不显著；**+0.080** CI[+0.020,+0.140] 为 2026-09-01 冻结模型 pooled 档案值（09-08 WF 曾 +0.089），已被特征非不变性（TTL 重取追溯改写历史 close）复算推翻——仅存档，不再引用为当前结论或门槛）
- Walk-Forward: **PARTIAL**（wf_partial：重训救不回最近两折 → regime shift 而非模型老化）
- T+3 三拆（研究轨，2026-09-18）: **三尺度均不显著，「仅点估计」维持**（D-lite 冻结面板 a158+LGB WF：pooled +0.0299 CI[-0.0061,+0.0647] / CS 日均 +0.009 CI[-0.0236,+0.0365]（333日，宽≥4） / TS 成员均 +0.0218 正值占比 0.652<2/3（23员，n≥60）；pool17 敏感性同向更弱；判据见 forecast_lab_prereg_T3decomp_20260918.md，生产轨 C2 重跑另行授权）
- Quantile: **CALIBRATED（CV conformal-style 诊断）**（原始 coverage 57~62% 过窄，CQR 后 ~76%；不绑 registry）
- CQR-WF: **DEPLOYMENT-STYLE READY（观察层）**（09-01 P1-③：逐折折内 split-conformal，T+1/3/5 pooled cov 85.6%/87.7%/84.2% 全部入带宽，qhat 折间窄；正式口径仍以 calib 报告为准）
- Path: **OBSERVATION ONLY**（v1.3 基金级 20 交易日 σ，RECENT_WINDOW 冻结；Path-WF pooled hit mdd10 10.8% ≈ 期望 10%，mfe50 31% 低估观察中）
- Path-WF: **OBSERVATION（部署式逐折校准）**（折间 hit 波动大 → 已接 Drift 监控）
- Policy: **BLOCKED**（ML 超额 -3.041% vs 单因子 +0.167%，θ=0.6 预注册不动）
- Shadow Policy: **ENABLED (paper-only)**（shadow_actions.jsonl：run.py post 时点自动 + 每日 22:30 cron；每条内嵌 Evidence Contract）
- Drift: **MONITOR LIVE**（drift_monitor_20260901：covered_pct/composite 真实漂移，est_chg 折 4 PSI 0.453 显著；观察层不绑 verdict）
- Market Context: **v0.1 封版，观察期**（09-02 上线 → 09-03 v0.1 → 09-04 封版；每日 11:30/14:55 快照落盘 `data/market_context/` + `history.jsonl`；指标定义冻结至满 60 有效交易日，期间不改口径）
- Market Context OOS: **EVIDENCE ACCUMULATION**（usable_for_oos 逐日标记；满 60 日先做独立 OOS，再测对 T+5 Forecast 的增量价值）
- 数据降级状态机: **cache:fallback 已入状态机（2026-09-18 V4.1 ③）**（`_source` 三态中 `cache:fallback` = **全链失败后退回旧缓存**，`load_fund()` 在此态**正常返回不抛异常**，故仅 `try/except` 抦不住它。现由 `data_loader.is_nav_fallback()` 作单一事实源，run.py 据此记 `fund_data_fallback:<codes>` 进 `degraded` ⇒ **exit=2** + `run_manifest.data.fallback` + `publish_gate` 逐基金归因拦截 + 报告层告警四处共享。修前漏洞：EastMoney+Sina 全挂 → 退旧缓存 → 返回成功 → exit=0 → 门禁看不见污染）
- 运行退出码: **0=SUCCESS / 2=DEGRADED / 1=FAILED**（2026-09-16 落地；2026-09-17 校正语义。降级项写 `output/run_manifest/run_manifest_<run_id>.json`，含 data / lookthrough / realtime / intraday_features / market_context / report / notification / shadow 逐环状态 + `degraded_reasons`。语义：`exit=0` = 本次运行**未触发任何已定义降级条件**；`exit=2` = 运行完成但证据链 DEGRADED；`exit=1` = 核心流程失败。**清单自身落盘失败也计 DEGRADED（exit=2）**，监控侧以「exit=2 且本次 run_id 无对应清单」识别 `run_manifest_write_failed`）
- 运行清单掩码: **ENFORCED（2026-09-18 V4.1 ④）**（`output/run_manifest/` 随仓库跟踪，而逐基金降级码 ⊆ `config.fund_pool`；旧实现把**真实 6 位代码**写进 `data.failed` / `data.fallback`，与 `run.py` 自己的注释「清单不得写代码」自相矛盾（P0-2 持仓隔离）。现落盘前由 `audit_project.mask_manifest_funds()` 把全部逐基金字段（`GATE_FUND_KEYED` 五处）与 `degraded_reasons` 的代码段换成**位置别名** `F1…Fn`（基准 = `config.fund_pool` **升序位**，故同一只基金在所有字段、所有清单里都是同一个 F 号），并留 `fund_refs.pool_sha256_8` 供反查。门禁侧 `resolve_fund_refs()` 按同一张表反查 ⇒ **逐基金归因能力不变**；反查不出来的别名（基准表缺失／指纹不符／F 号越界）**不推断**，一律走「不在论域」的 fail-closed 分支。掩码后仍残留 6 位码 ⇒ **拒绝落盘**（转 DEGRADED，exit=2）——宁可少一份证据，也不写出一份泄露持仓的清单。09-18 及更早的旧清单属在档证据，不回改）
- 证据通道隔离: **ENFORCED**（2026-09-16；`approved_full` / `prereg_degraded` / `legacy_invalid` 三通道默认禁止跨通道聚合；读取一律走 `shadow_policy.load_records_by_channel()`，`--channels` 清点、`--archive-legacy` 把 36 条 legacy 搬出活跃流）
- 发布资格门禁: **ENFORCED（2026-09-17）**（`run.py` 推送飞书前调 `audit_project.publish_gate()` 判「当日逐基金证据链此刻是否干净」：**任一只持仓基金带降级 ⇒ 整批不推**（缺一只的报告本身残缺）。证据 = 当日已落盘清单 + 本次内存 `degraded`（post 推送时本次清单尚未落盘）；逐基金前缀按结构化字段归因，归因不成立升运行级（宁严勿漏）。逐基金采 **last-run-wins**：干净与否以当日最后一次**完整评估**轮为准（mid 的行情抖动在 post 已恢复 ⇒ 不拿旧账拦最重要的决策推送），但**运行级问题全天粘性**（市场背景/池内数据缺位末轮干净也照拦）；末轮证据残缺则回落全天并集（不得洗白）。**防自锁**：`feishu_push_failed` 与清单 `notification` 节是门禁的输出，永不作输入——否则「今日推送失败 ⇒ 明日永不可推」。拦截记 `publish_gate_blocked:<reason>`、逃生口 `--no-publish-gate` 记 `publish_gate_bypassed`，两者均 ⇒ exit=2 留痕。审计侧 `P1-9` 仅呈现为 **WARN 不计 FAIL**（`audit_health` 继续只度量静态审计），入库 JSON 只存计数/掩码，真实持仓代码只进本地日志）
- 审计状态机: **四层独立（2026-09-17）+ active/historical 分离（2026-09-18 V4.1 ②）**（`audit_project.py` 输出 `audit_health` / `model_promotion` / `action_enable` / `production_status` 四层，互不替代。当前=`PASS_WITH_WARNINGS` / `BLOCKED` / `HOLD_ONLY` / `BLOCKED`。**审计健康 ≠ 生产资格**：0 FAIL 不得推出生产可用——旧版把 0 FAIL 直接打印成 `PRODUCTION: NOT BLOCKED` 属状态机错误，已修。**`model_promotion` 只看 `config.forecast.active_model` 指定的那一只**（当前 `forecast_v3.pkl`）：旧实现拿「registry 里全部 blocked 模型」当否决权，历史失败记录会**永久阻止**未来模型晋升（v4 即便 approved，v2 还在 registry 就永远 BLOCKED）；历史 promotion 是**档案事实**，现只作可见信息（`inputs.historical_blocked_models`）。active 未声明/未登记/promotion 非 approved ⇒ 一律 fail-closed 判 BLOCKED；换权重时必须同步 `active_model` 字段。结果落 `output/audit_current.json`（唯一当前指针）+ `output/audit_history/`（只增不删）；`--no-write` 为只读复核。JSON `schema_version=2.0`，旧 `production` 字段保留兼容但只反映审计 FAIL 数；旧 `inputs.blocked_models`（全量）也保留，新字段才表达 active/historical 拆分）
- P1-2 绑定的严格性: **active 缺报告即 FAIL（2026-09-18）**（旧实现 `if not rf: continue`——根本没写 `report_file` 的模型被静默跳过，「无绑定」与「绑定正确」同样得 PASS，与 `verify_validation_report()` 的严格语义相反。现 active 缺 `report_file`/`report_sha256` ⇒ **FAIL**，历史模型缺绑定 ⇒ WARN（档案事实不阻断当前））
- 审计「当前运行」验证: **V4.2 收紧（2026-09-18）**（P1-7 旧实现只要 `output/run_manifest/` 里存在**任意** JSON 就 PASS，且取 `files[-1]`——「今天没跑、昨天留了一份 SUCCESS」照样过关。现按四问核验最新清单：① 自洽（文件名与 `run_id` 一致、日期可解析）；② 齐备（`run_id`/`slot`/`status`/`data`/`lookthrough`/`realtime`/`degraded_reasons` 全在，`status` ∈ 三态词表）；③ 新鲜度（清单日期不得早于 `last_expected_run_date()`：交易日过 11:30 算今天、否则回溯到上一交易日，故周一 09:00 不会误报周五、周一 15:00 停在周五必报）；④ 掩码（逐基金字段残留 6 位码 ⇒ FAIL，V4.1 ④ 契约由审计常驻复核）。结论写进 payload 的 `evidence_as_of` / `evidence_expected_as_of` / `evidence_stale` / `evidence_latest_run`——**current 指针必须自证「描述的证据截至哪一天」**）
- 审计指针时效: **AUTO-REFRESH（2026-09-18 V4.2）**（README 说「所有人只看 `audit_current.json`」，但该文件旧版只在人手动跑审计时才更新，实况里曾停在上一交易日——名字与行为直接矛盾。现 `run.py` **post 收尾**调 `audit_project.refresh_current()` 重跑审计并刷新 current + 追加一份 history，指针时效由证据产出动作自己维持。刷新在**清单落盘之后**执行（指针必须描述「含本次运行在内」的证据状态，放前面会滞后一轮）；刷新失败**不**降级本次运行——指针是派生视图，过期与否已由 `evidence_stale` 自证，结果只进本地日志 `output/logs/`）
- 动作层状态门禁 + 信号流水幂等: **V4.2（2026-09-18）**（① `_lsjz` 取数失败时 `load_fund()` 照常返回、申购/赎回状态落「未知」并留 `_lsjz_error`，旧实现下这层信息不进任何状态机——未来动作层一打开就可能输出**执行不了**的加/减仓。现 `data_loader.is_fund_status_known()` 作单一事实源，`decision_engine._check_gates` 增硬门禁（状态未知 ⇒ invalid ⇒ 动作恒 HOLD），状态未知清单记进清单 `data.status_unknown`（掩码，不进发布门禁——它只锁动作层）。② 信号流水（Obsidian）改**同日幂等**：同一交易日只留一行，重跑**就地更新**当日行（last-run-wins，与发布门禁同口径），只做单行替换 + 原子写，其余行逐字节不动——旧实现无条件追加，修 bug 后重跑会与旧行并存、读者无从判断哪行是当日定论）
- 数据快照 manifest: **DATASET SNAPSHOT（2026-09-18 V4.1 ①）**（`data_fingerprint.py` → `data/manifest.json`，**scope=`dataset_inputs`**，355 个文件。关键变化：清单**不再包含** `model_registry/` 与 `models/`——旧版把 `registry.json` 也收录，而 `capture_provenance()` 又把清单文件的 sha256 写回 registry，构成自指循环依赖（manifest ─hash→ registry ─内嵌→ manifest 的 hash），**任何重生成都会使清单记录的 registry hash 立即过时**（实测：清单记 `2390f0f1…`、registry 实际 `54619525…`），Evidence Contract 从根上失去严格意义。现依赖单句：Dataset Snapshot → Model Provenance（registry）→ 模型权重 / git commit。**snapshot_id 改为内容寻址**（同数据必同 ID，与 mtime/生成时刻无关），键名统一正斜杠（旧版 Windows 反斜杠使跨环境键不可比）。防护两层：`_assert_acyclic()` 写盘前硬报错 + 审计 `P0-5` 每次复核生产文件。旧版全量清单已归档（`manifest_20260916T181240.json`），12 条 shadow 记录内嵌的 `afd156ab12a3bff1` 等历史引用仍可解析）
- V4.3 改造批次: **4 P0 硬契约（2026-09-20）**（① `frozen_dataset.py` 统一样本入口：12 个回测/评估脚本默认强制消费最新冻结件（G-A 硬 / G-B 软），无冻结件且非 --fresh ⇒ exit 4 fail-closed（拒绝静默活拉），--fresh 显式活拉且报告标 FRESH；② KFP as-of + universe：冻结/校验的指纹重算带 stock_codes + cutoff，留档带 scope 即同口径重算（09-20 前锚点件仍全量口径）；③ 个股K线失败契约：load_samples 不再静默跳过（fetch_stock_klines 记录 failures），冻结 meta 落 stock_data_failures，任一码失败 ⇒ SNAPSHOT_INVALID 不发布 canonical；④ T+5 证据清理：+0.080 降为历史档案，当前结论 = WF 部署式 -0.079（UNRESOLVED））
- Forecast/Policy 整合: **LOCKED**（Market Context 仅描述性标签，不进 Forecast、不进 Policy、不改任何门禁）
- Intraday delta: **DATA ACCUMULATION**（配对日 < 15 门槛，不产 verdict）
- 下次重估: **2026-09-26 阶段性复核**（数据源稳定性 + Market Context 首批样本质量 + Forecast 既有证据重估；**≠ 晋升评估**，Market Context 晋升在满 60 有效交易日后）

## 快速开始

```bash
cd D:\PythonProject\QuantV1    # 2026-09-08 起在 Windows 原生运行（旧 WSL 路径已废）
.\.venv\Scripts\Activate.ps1   # 激活项目 venv（Python 3.12 + requirements.txt）

python run.py                     # 自动判断时点（工作日，14:55 分界）
python run.py --slot mid          # 午盘 11:30 · 趋势状态扫描（特征快照落盘）
python run.py --slot post         # 收盘前 14:55 · 决策窗口（读 11:30 快照算变化量）
python run.py --slot post --force # 非交易日强制执行（周末/节假日调试）
python run.py --no-push           # 只落盘报告，不推送飞书
python run.py --no-lookthrough    # 跳过重仓股穿透（省网络请求）
python -m unittest discover -s tests -v    # 全量单测（套件位于 tests/，项数不写死）
python run_tests.py --layer fast           # 分层快跑：纯函数/契约（日常开发，秒级）
python run_tests.py --layer slow           # 分层慢跑：模型/回测/真实数据耦合
python audit_project.py                    # V4 审计契约 → audit_current.json + audit_history/
python backtest_action.py         # 动作收益回测（加/减/不动 vs 不动，解锁动作层的唯一证据）
python backtest_lookthrough.py    # 穿透信号回测（第四因子裁决依据）
python backtest_chanlun.py        # 缠论事件回测（观察层裁决依据）
python backtest_factors.py        # 三因子全历史复验 + 横截面轮动检验
```

> **定时入口（2026-09-08 迁移后为 Windows 计划任务，直调 venv python，无需手动跑）**
> `QuantFund_Mid` 工作日 11:30 → `run.py --slot mid`；`QuantFund_Post` 工作日 14:55 → `run.py --slot post`。
> 换盘符/搬家后重装：`powershell -File deploy\install_scheduled_tasks.ps1`（路径由 `$PSScriptRoot` 派生）。
> 板块 K 线拉取（16:00）与 Shadow Policy（22:30）由 OpenSquilla cron 调度，任务内已改 Windows 命令。

## 架构（v4.0.0-decision）

```text
市场行情（进攻/防守篮子代理 · ETF/BK）
        │
        ▼
   Market Context 观察层（core/market_context.py）
   PIT / replay / coverage / OOS 降级标记
        │
        ▼
   每日快照 data/market_context/{date}.json + history.jsonl
        │
        ▼
   报告「市场环境观察」栏（仅描述，🔒 不接 Forecast/Policy）
```

```
基金正式净值（T+1） → 最新季报前十大持仓 → 实时行情（腾讯）
        │                        │
        ▼                        ▼
   三因子弱参考            日内特征引擎（intraday_features.py）
   （中期趋势维度）        估算涨跌 / 同向度 / 集中度 / 可信度
                                  │
                    [mid 11:30 存快照] → [post 14:55 读快照] → 变化量 Δ
                                  │
                                  ▼
                        决策倾向引擎（decision_engine.py）
                        五维加权 → 倾向分 -100~+100
                                  │
                                  ▼
                        四道准入门槛（config.decision.gates）
                        history_validated=false → 动作恒为保持不动
                                  │
                                  ▼
                        报告 + 飞书推送 + 人工确认
```

```
QuantV1/
├── config.json              # v4.0.0-decision：三因子 + decision 配置块（五维权重/阈值/四道门槛）
├── holdings.json            # 持仓（本地私有，已被 .gitignore 排除、不入库）——支持可选 max_position_pct / consecutive_adds
├── holdings.example.json    # 持仓模板（首次使用复制为 holdings.json 再填真实值）
├── .env.example             # 飞书 webhook + 可选 TUSHARE_TOKEN（个股K线备源）
├── run.py                   # 主入口：实时行情→特征→快照/决策全链路 + 节假日过滤 + 日志
├── run_tests.py             # 分层测试执行器（fast/slow，unittest 原生，无需 pytest）
├── audit_project.py         # V4 审计契约（只读/零网络）→ 四层状态 + current/history 留档
├── backtest_action.py       # 【v4 新增】动作收益回测（加/减/不动 vs 不动 → 动作层解锁证据）
├── backtest_lookthrough.py  # 穿透信号回测（防前视）
├── backtest_chanlun.py      # 缠论买卖点事件回测（防前视）
├── backtest_factors.py      # 三因子全历史独立复验 + 横截面轮动检验
├── backtest_spread.py       # 方向差细分回测（v4 补 breadth/concentration 字段供 backtest_action 复用）
├── core/
│   ├── data_loader.py       # 东财免费接口 + 缓存 + **_source 降级标记**
│   ├── stock_data.py        # 个股日K：腾讯前复权主源 + Tushare 备源（需 token）
│   ├── lookthrough.py       # 穿透层：季报持仓 + 动力学指标（因果）+ 加权聚合 + 防狼术
│   ├── chanlun.py           # 缠论全量形态学：线段级中枢 + confirm_date 因果事件
│   ├── real_time.py         # 重仓股实时行情 → 底层持仓估算涨跌（措辞对齐 GPT 诊断）
│   ├── intraday_features.py # 【v4 新增】日内行为特征引擎（估算/同向度/集中度/覆盖率/新鲜度/可信度）
│   ├── intraday_store.py    # 【v4 新增】日内快照持久化：mid 存 → post 读，算变化量
│   ├── decision_engine.py   # 【v4 新增】仓位动作评分器：五维加权 + 四道门槛 + 硬门禁
│   ├── signal_engine.py     # 净值三因子弱参考（作为决策引擎「中期趋势」维度）
│   ├── rotation.py          # 池内横截面轮动参考（报告观察栏）
│   ├── account.py           # 账户面 + v4 决策约束输入（position_pct / 组合集中度 HHI）
│   ├── market_context.py    # 【v0.1 封版】市场环境观察层：篮子代理 PIT/重演/质量标记/history.jsonl
│   ├── report_generator.py  # 三时点报告（+ 决策倾向栏 + μ₂₀ 列 + 降级提示 + 穿透/轮动栏）
│   └── notify.py            # 飞书 webhook（蓝色弱参考卡片 + 动作倾向字段）
├── experiments/chanlun/     # 缠论代理K线 POC（归档自 quant_test 母本，只读存档，不接主线）
├── data/holidays.json       # 2026 全年 18 个休市工作日（**2027 待更新，run.py 会告警**）
├── data/klines/             # 基金净值缓存（TTL 12h，带 _source 标记）
├── data/stock_klines/       # 个股K线缓存（TTL 12h）
├── data/intraday/           # 【v4 新增】日内特征快照（保留最近 30 个）
├── backups/                 # 重构前代码备份（backups/20260825-v4/）
├── tests/                   # 单测：契约/PIT/预测/门禁/影子通道（分层清单见 tests/layers.py）
│   └── layers.py            # 测试分层唯一事实来源（fast/slow），conftest.py 映射 pytest marker
├── output/                  # 报告 + 回测报告 + logs/；audit_current.json（当前审计指针）+ audit_history/
└── deploy/                  # Windows 部署：定时任务（11:30/14:55）+ 桌面快捷方式
```

## 决策倾向层（v4 新增 · 观察层 + 硬门禁）

v4 把「实时数据 → 决策倾向」链路闭合，但**动作输出被回测铁律锁死**（GPT 建议的结构 + 项目自己的证据裁决）：

### 五维加权倾向分

| 维度 | 权重 | 口径 | 子分范围 |
|---|---:|:---|---:|
| 日内趋势 | 35% | 14:55 估算涨跌（tanh 映射 ±30）+ 午盘→尾盘变化量 Δ（±10） | ±40 |
| 重仓一致性 | 25% | 前十大同向度（±15）+ 集中度惩罚（-8，防「一票独大」） | ±20 |
| 池内相对 | 15% | 本基金实时估算 vs 池内其他基金估算均值（行业指数基准待接入） | ±15 |
| 中期趋势 | 15% | 原三因子总分（-3~+3 线性映射，保留为解释维度） | ±15 |
| 账户约束 | 10% | 仓位余量 / 成本保护 / 连续加仓限制 | ±10 |

倾向分归一化 -100~+100：≥+60 候选加仓 / ≤-60 候选减仓 / 其余候选不动。

### 四道准入门槛（config.decision.gates）

| 门槛 | 条件 | 不满足后果 |
|---|---|---|
| 数据完整 | 实时覆盖率 ≥60% 且持仓披露 ≤120 天 | action 强制 HOLD |
| 信号稳定 | 11:30→14:55 变化量参与日内趋势分（结构已含） | — |
| 历史有效 | `backtest_action.py` A+B+C+D 全过 → 人工置 `history_validated=true` | action 强制 HOLD |
| 账户允许 | 加仓时当前仓位 < 最大允许 80% | action 强制 HOLD |

### 动作收益回测裁决（backtest_action.py，2026-08-25 首跑）

| 判定 | 标准 | 结果 |
|---|---|---|
| A | ADD 桶优势 >0 且 bootstrap 95% CI 下界 >0 | ✅ +1.20% [+0.15%, +2.22%]（313 样本，胜率 55%） |
| B | REDUCE 桶优势 >0 且 CI 下界 >0 | ❌ -1.28% [-2.63%, +0.03%]（141 样本） |
| C | 前后两半 ADD 优势方向一致（均 >0） | ❌ 前半 -0.26% / 后半 +2.00% |
| D | 倾向分 ≥60 桶优势 > 中间桶（-20~20） | ✅ +1.20% > +0.00% |

**结论：❌ B/C 失败 → 动作层继续锁死（history_validated=false）**。与 2026-08-25 三轮回测（组合回测 B/D、方向差 E、OOS K）互为印证：当前信号组合无稳定动作 alpha。引擎结构已就绪，待信号迭代后重跑 `backtest_action.py`，A+B+C+D 全过才允许人工解锁动作输出。

## 信号逻辑（弱参考，净值三因子，总分 -3 ~ +3）

| # | 因子 | +1 | -1 | 证据 |
|---|---|---|---|---|
| 1 | MACD柱趋势（净值） | 柱>0 | 柱<0 | 横截面 IC +0.299（2026-08-22 回测） |
| 2 | 池内排名20日（净值） | 上半池 | 下半池 | 分桶单调 2.8→5.7→5.8% |
| 3 | 距60日新高（净值） | 回撤≤-3% | ≥-1% | 分桶 2.5→0.9→0.7% |

**≥+1 偏多 / ≤-1 偏空 / 其余中性**。v4 中三因子总分作为决策引擎「中期趋势」维度（15%），单独仍为弱参考。为什么恒为弱参考：旧 10 维引擎总分 IC=-0.041（方向性错误）；三因子时段依赖；穿透第四因子曾被扩展回测（2020-2026）否决（见下）——故永不输出买卖指令。

### 穿透观察层（报告展示，不进打分）

- **MACD背驰（动力学口径·缠论背驰近似）**：同向柱簇力度 = 面积÷时间（课24），B 段黄白线须回拉 0 轴（课24 A/B/C 前提），C 端须创新高/新低（课61）；已知简化：以回 0 轴近似「经过同一中枢」（课64 的严格比较对象）
- **吻结构** = MA5/20/60 多空排列，按课25 明确标注为**吻体系或然性辅助**，不称缠论指标
- **防狼术告警**（课103）：前十大 ≥50% 权重黄白线双双 0 轴下 → 报告 ⚠️ 行
- **缠论事件** = core/chanlun.py 全量形态学（线段级中枢）一/二/三类买卖点，30 日窗口展示
- **PIT 生效日口径（诚实声明）**：报告期持仓的「生效日」= 报告期 + 披露滞后（`core/lookthrough.py` 的 `_QTR_LAG`），
  该滞后是**披露法定时限的保守下界，不是真实公告日**——2026-09-17 实测东财 F10 `jjcc` 响应不含公告日字段
  （只有「截止至：<报告期>」），真实公告日无零网络来源。下界按法定时限（季报 15 个交易日 / 半年报 60 自然日 /
  年报 90 自然日）逐期核验；**09-30 值由 25 修正为 30**（原值小于法定下界，近 3 年该期存在 3~4 天前视），
  修正后样本选择仅变动 9/899 交易日（1.0%）。量化依据见 `output/ops_runs/2026-09-17-pit-lag-impact.md`，
  判据见 `audit_project.py: check_pit()`（P0-1）。
- 形态学已知简化（诚实记录）：线段终结未实现课67 缺口第二种情况；中枢 9 段不升级；无区间套/多级别联立

### 双回测裁决（2020-2026 全区间扩展版，证据厚度检验后）

扩展验证：东财 F10 持仓接口 `year` 参数实测**完整支持 2020-2022**，回测从单段
（2023-04~2026-07）扩为 2020-04~2026-07（3412 基金日），覆盖茅指数牛市/2022 熊市/结构市三种环境。

| 信号 | +1 桶超额 | 时间分段 | 裁决 |
|---|---:|---|---|
| 穿透 composite（动力学口径） | +1.66%（730 样本） | **前 -1.53% / 后 +3.40%（时段依赖）** | ❌ **退出引擎**（2026-08-23 扩展证据否决） |
| 缠论买卖点事件（全量形态学） | -1.01%（109 样本） | 前 -5.57% / 后 +0.15% | ❌ 仅观察层 |

**裁决记录（诚实）**：第四因子曾在单段样本（2023-2026）通过三条判定并一度开启；扩展到 2020-2026
后前半段（2020-04~2023-11）+1 超额为 **-1.53%**，时段依赖暴露，按事先标准关闭，退回观察层。
单段样本的「通过」是区间运气——这正是 YMOS 笔记「可证伪条件」（如果这次修改是错的，会看到什么）
的价值当场兑现。0 桶（无观点）全样本超额 -0.01%，说明两类信号整体均无稳定时序超额；
穿透/事件视图作为**观察层**保留（报告展示），打分引擎回到净值三因子（总分 -3~+3）。

### 三因子独立复验与轮动检验（2026-08-23，backtest_factors.py）

重建版三因子与原实现非逐行一致，在 2016-2026 全历史（5247 基金日）独立复验：

- 分桶单调成立：-2 桶 -1.42% → +2 桶 +1.34%；买入≥+2 超额 **+1.21%**（002112 +3.80% / 002207 +0.16%，
  与原笔记 +1.56%/+0.54% 方向一致量级可比）；**pooled 前后两半皆正**（+0.35%/+1.88%）——继承证据成立
- 横截面因子 IC 显著（20日收益 t=+2.99 / MACD柱 t=+3.94 / 距60日高 t=+3.96），**但 top1 轮动超额未复现**
  （-0.07%，t=-0.64；原笔记 +0.41%/t=3.44 系 3 只池·2023-2026 短样本口径）——轮动栏保留为
  纯相对强弱观察，报告脚注已如实标注，不构成任何轮动依据

## Windows 部署（可选）

```powershell
deploy\install_scheduled_tasks.ps1     # 管理员：注册周一~五 11:30/14:55 两任务
deploy\create_desktop_shortcut.ps1     # 桌面「基金日频参谋.lnk」双击即跑
copy .env.example .env                 # 填 FEISHU_WEBHOOK（可选签名 FEISHU_SECRET）
```

前提：**电脑开机且已登录**，错过（关机时段）默认不补跑。节假日过滤由 `run.py` 内部完成
（`data/holidays.json`），每年 12 月下旬更新该文件即可，代码零改动。

## 维护清单

| 事项 | 频率 | 操作 |
|---|---|---|
| 数据快照 manifest | 数据目录变动后 / 每月 | 跑 `python data_fingerprint.py` 重生成 `data/manifest.json`（scope=`dataset_inputs`，**不含** registry / 模型权重——否则与 `capture_provenance()` 构成自指环；shadow 契约内嵌其 sha256，过期即证据链失真；旧版自动归档到 `data/manifest_history/`，保证历史记录内嵌哈希永远可解析；生成后跑 `python audit_project.py` 确认 P0-5 PASS） |
| 换模型（新 MODEL_VERSION） | 每次重训后 | 同步 `config.json` 的 `forecast.active_model`（审计只看这一只；不改则新模型拿不到晋升资格，旧模型卸任也不产生死锁） |
| 节假日日历 | 每年 12 月 | 更新 `data/holidays.json`，代码零改动 |
| 持仓变动（基金） | 申赎成交后 | 改 `holdings.json`（份额/成本净值/可选 max_position_pct / consecutive_adds） |
| 持仓快照（重仓股） | 每季度 | 自动（季报披露后接口自动可见） |
| 025687 历史补齐 | 自动 | 净值满 120 条后自动去掉「数据不足」 |
| 动作层解锁复查 | 信号迭代后 | 重跑 `backtest_action.py`，A+B+C+D 全过才人工置 history_validated=true（仍需复核措辞红线） |
| 数据源互备 | 待办 | 个股K线现仅腾讯单源；可接 tdxrs（本地通达信）/Tushare 作备胎（见 YMOS 实测记录 Level 分级） |
