# QuantV1 · 定时任务运行约定（ZCode 自动化用）

本目录是 QuantV1 的正式项目根（2026-09-08 起 Windows 原生，旧 WSL 路径已废弃）。
场外基金日频参考系统：**只出参考建议，绝不自动下单**。

## 执行环境（两套 Python，勿混用）

- 业务脚本一律用项目 venv：
  `Set-Location -LiteralPath 'D:\PythonProject\QuantV1'; .\.venv\Scripts\python.exe -X utf8 <脚本>.py`
- Playwright 兜底脚本 `pull_sector_klines_pw.py` 是**故意不装在 venv 里**的
  （见 requirements.txt 注释），必须用全局 Python：
  `D:\Python\python.exe -X utf8 pull_sector_klines_pw.py <YYYY-MM-DD>`
  （chromium 149 已验证可冷启动）
- `-X utf8` 必带：脚本输出含中文，缺了会在 PowerShell 里乱码/报错。

## 铁律（无人值守时必须遵守）

1. **数据源频控**：东财 push2his kline/get 有 IP 级频控，且是**按接口粒度**（2026-09-08
   实测：同主机 kamt 接口 200、kline/get 全掐；quote.eastmoney.com 页面能打开，但页面
   自己发的 K 线请求同样 ERR_EMPTY_RESPONSE，图是空白的 → 换浏览器/指纹/cookie 都无解，
   同 IP 的任何通道都一样）。09-05 / 09-08 两次停摆。
   `pull_sector_klines.py` 最多跑 2 轮（第 2 轮靠脚本自身 skip 已成功码补拉缺口）。
   两轮后仍有 fail → **禁止第三轮原生脚本，禁止盲跑 pw 兜底**（盲跑 = 拿被掐的 IP 反复
   撞，09-08 曾自动重启浏览器 12 次全灭）。先跑分类诊断：
   `.\.venv\Scripts\python.exe -X utf8 experiments\channel_diag\diag_20260908.py <任一FAIL码>`
   - B/C/D/E 全 FAIL = IP/接口层掐 → 停手，等 21:30 晚间补拉或下一交易日
   - 仅 C FAIL（B 或 D 成功）= CORS/姿势问题 → 此时才跑 pull_sector_klines_pw.py
   - A 恢复 200 = 频控已解除 → 回主通道再跑一轮
   晚间补拉 `pull_sector_klines_evening.py`（Windows 计划任务 QuantFund_KlineEvening，
   21:30，venv）单轮不重试、只补缺口码；报告写 output/pull_sector_klines_evening_*.md
   + zcode_runs 小节 [21:30 板块K线补拉]。
2. **shadow 只记录不执行**：`shadow_policy.py` 产出纸面样本。
   任何输出不得包含建议实盘操作的措辞，记录本身不构成投资建议。
3. **幂等优先**：报"全部幂等跳过"是正常状态，不要为重跑出结果而反复执行。
4. **不改数据**：只读 `data/`，脚本自身写 `output/`；不修改任何 .py / .md 笔记
   （季度重估任务除外，其 prompt 明确要求补记 Obsidian 对照笔记）。
5. **失败平铺直叙**：命令失败/退出码非 0 → 如实记录 stderr 末行，最多重试 1 次。
6. 已知顽固失败码：BK1137 存储芯片、BK1163 可控核聚变，偶发 RemoteDisconnected，
   单次失败不需人工介入。

## 代码习惯：ponytail lite（2026-09-14 接入）

来源 `DietrichGebert/ponytail`（精简/lite 档），仅约束**交互开发会话**里"新写或改代码"的行为；
不改变上面「铁律」对无人值守定时任务的约束（定时任务依旧不改任何 .py）。

- **适用范围**：工具/胶水层收敛重复。已核实的重复点（2026-09-14 扫描根目录 16 个
  `backtest_*.py`）：`main` 样板 ×16、`pearson` ×3、`_window_slices` ×3、`spread` ×2、
  `_fit_one_horizon` ×2 —— 这类先提公共、不再复制粘贴。
- **不碰清单（永不精简）**：因子逻辑、数据校验、错误处理、回测口径。ponytail 原规则同样
  规定"never cut validation, error handling, security"，与本仓库「八荣八耻」第 5 条一致。
- 动手改前按七级阶梯，停在成立的最早一档：
  1. 需要存在吗？投机需求 → 不写，一句话说明（YAGNI）。
  2. 仓库里已有？先查 `core/` 与相邻 `backtest_*.py` 的现成函数 → 复用，先查再写。
  3. 标准库能做 → 用标准库。
  4. 平台/依赖原生特性够 → 用原生。
  5. 已装依赖能做 → 用它；不为几行新增包。
  6. 能一行 → 一行。
  7. 到这一步才写最小实现。
- 刻意简化且有已知天花板处，加 `# ponytail: <上限与升级路径>` 注释（例：`# ponytail: O(n²) 扫描，数据量上来再建索引`）。
- 删代码优先于加代码；最短可工作 diff 优先——但前提是先读透问题：错位置的最小改动不是精简，是第二个 bug。
- 每次收敛后照旧验证：跑 `tests/` 相关用例或对应脚本的幂等重跑（正常态应报"幂等跳过"）。

## 运行报告落点（与 OpenSquilla 会话的同步通道）

每次定时任务结束（成功或失败）必须把执行报告**追加**写入：
`output\zcode_runs\<YYYY-MM-DD>.md`，小节标题带时段标识
（如 `[16:00 板块K线]` / `[22:30 Shadow]` / `[09-26 季度重估]`）。
内容：日期时间、关键数字（ok/skip/fail、ADD/REDUCE/HOLD 计数等）、是否走兜底、
FAIL 码、一句话结论。OpenSquilla 侧靠读这个目录汇报给用户，漏写=结果丢失。
