# 测试运行指南（2026-10-01，面 6 D6-A-3/D6-D-3 重写；初版 2026-08-31 A6 分层）

项目测试为 unittest 风格（无需 pytest；pip 受 PEP 668 限制时用标准库跑法）。

## 主路径声明（以谁为准）

**分层跑测以仓库根 `run_tests.py` 为主路径**，`tests/layers.py` 是分层唯一事实来源：

```bash
.\.venv\Scripts\python.exe run_tests.py --layer fast    # 日常开发（真正的轻量快路径）
.\.venv\Scripts\python.exe run_tests.py --layer slow    # 提交前 / 夜间
.\.venv\Scripts\python.exe run_tests.py --all           # 全量
.\.venv\Scripts\python.exe run_tests.py --list          # 只看分层清单（含磁盘核对）
```

`unittest discover` 为兼容旧习惯保留；两口径的加载集合一致性由
`tests/test_layers.py` 守护（未登记文件 / pytest 风格命名 / 非 TestCase 风格都会红灯）。

## fast / slow 的真实语义（如实描述，勿凭印象）

- `--layer fast`：48 个文件，纯函数 / 契约 / 解析器；无重依赖（sklearn/scipy）、
  不**写**真实 `data/` 与 `output/`、零网络。日常回归跑这个。
  只读触点（如实列举）：节假日历（`test_holidays` 经 `from run import HOLIDAYS` →
  run.py 模块级读真实 `data/holidays.json`）、交易日历（`test_pit_guard`，缺席自动 skip）、
  冻结件/契约文本（`test_forecast_contract` / `test_validation_schema`，缺席自动 skip）。
- `--layer slow`：16 个文件，模型 / 回测 / 真实数据 / 网络耦合。

⚠️ **`unittest discover tests` 不是轻量快路径**：它会把 slow 层 16 个文件全部执行
（含 sklearn 重型训练、真实冻结件只读复算、`test_netutil` 起本地 http.server 等），
只有 `test_holdings_visibility` 的 3 个方法被 `RUN_SLOW_TESTS` 门控跳过。
历史上本文档曾宣称"重型测试默认跳过"——失实（slow 16 文件中仅 1 个有门），已改正。

## RUN_SLOW_TESTS 的现行作用域（如实）

`RUN_SLOW_TESTS=1` **只解锁 `test_holdings_visibility` 的 3 个方法**（持仓历史网络
可见性，约 76s），不再是"全量开关"——全量请用 `run_tests.py --all`。
slow 层其余 15 个文件在 discover 下本来就会执行，不受该环境变量影响。

## 单文件

```bash
python -m unittest tests.test_forecast_split -v
```

## 留档呈现协议（面 6 D6-D-2，2026-10-01）

任何自测留档（`evidence/probes/`、`output/daily_runs/`、INDEX）呈现测试结果时，
格式统一为：

```
Ran N tests ... OK (skipped=k: 理由列表)
```

k>0 时必须列理由（哪些文件哪些方法因何被跳过）。fast 层存在 3 个文件共 4 个潜伏 skip 点
（`test_validation_schema` 契约文本缺席 ×1、`test_forecast_contract` 冻结件缺席 ×1、
`test_pit_guard` 交易日历缺席 ×2——均为"环境缺文件自动 skip"），
在缺这些文件的机器上跑 fast 也会出 skip，
留档不带 skipped 数会构成隐性漏报。

## 新增测试文件的纪律

1. 命名必须 `test_*.py`（pytest 风格 `*_test.py` 三口径都收不到，守护测试会红）；
2. 必须是 `unittest.TestCase` 子类（模块级 `def test_*` 函数 discover 收不到，会红）；
3. 登记进 `tests/layers.py` 的 `LAYERS`（漏登记 `run_tests.py --layer fast` 会红）；
4. 归 fast 层的文件遵守：零网络、不**写**真实 `data/` 与 `output/`（只读触点限
   节假日历/交易日历/冻结件与契约文本，缺席自动 skip；registry 触点用
   tempdir 重定向，惯例见 `test_evidence_binding_anchor._RegistrySandbox`）。
