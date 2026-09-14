# 预注册（生效）：候选 A 影子并列记录 · 接法 ①（2026-09-13）

> **状态：🟢 已生效 / 先于实现落盘。** 本文写于代码实现之前，判据与记录口径不得
> 在跑后修改。上游依据：`output/forecast_lab_cand_a_20260913.md`（G2 通过）、
> `output/forecast_lab_prereg_rules_20260913.md` §一 R1（G2-⑤′ 用途锁定）。
> 草案（含 ②/③ 两案）：`output/forecast_lab_prereg_A_relative_pool_draft_20260913.md`。

---

## 〇、Summer 拍板（2026-09-13 23:31）：「按你建议的执行」

| # | 问题 | 拍板结果 |
|---|---|---|
| Q1 | 是否先走接法 ①（影子并列、不改分） | ✅ 是 |
| Q2 | 记录落在哪 | ✅ 搭 `output/shadow_actions.jsonl` 新字段，**不新建文件** |
| Q3 | 是否现在排 ②/③ 回测 | ✅ 否——等 ① 的前瞻数据 |
| Q4 | 方向相反时怎么显示 | ✅ 并列显示 + 显式标注分歧，**不自动调和** |

**因此本次实现范围仅限接法 ①。②/③ 需要各自的预注册与回测，本文件不授权。**

---

## 一、要解决的核心问题（起草时发现，非猜测）

现有横截面维与 A 不是同一个量（已读源码核实）：

| | 现有 `_score_relative`（`core/decision_engine.py:129-149`） | 候选 A |
|---|---|---|
| 输入 | `est_return`（= post 槽的 `est_chg`，当日重仓估算涨跌%） | 同 7 特征 + 训练标签 |
| 打分量 | 本基金 − 池内其余均值，`tanh(diff/1.5)×15` | 预测 **T+5 同日横截面超额** |
| 性质 | 实时、无模型、无前瞻窗口 | 模型输出、T+5 前瞻 |

直接替换会改语义、并列相加会重复计算同一横截面信息 → 故 ① 阶段**两者都不做**，
只做只读记录。

---

## 二、冻结规格（实现前写死）

### 2.1 训练数据与截断（PIT）

- 源：`forecast_outputs/samples_frozen_20260910.jsonl`
  （sha256 `be8e55f02b39a25dc48f2efd67150c895ceebfebfe97c647b00610f61a05b034`）
- 训练集：该文件中 **`fwd5` 非空**的全部样本（末个有标签日 ≤ 2026-08-12）。
- 决策日 D ≥ 2026-09-11 > 所有训练标签日 → **无标签泄漏**（结构上保证，非人工检查）。
- 特征：`run_m0_power.matrix_existing()`（7 逻辑键 × 值/掩码 = 14 维），
  与 `forecast_engine.FEATURE_KEYS` / `MASKING_PROTOCOL` 同源。
- 超参：`run_m0_power.LGB_PARAMS` + `N_ROUNDS`，`seed=42`，`deterministic=True`（不调参）。

### 2.2 两条臂都冻结并记录（避免「挑表现好的那条」）

G2 中 `A_mean`（同日均值超额）与 `A_med`（同日中位超额）**双双通过**相同的四条判据，
仅点估计略有差别（+0.0652 / +0.0703）。若只冻结其中一条，等于按结果挑臂。
故**两条臂都训练、都冻结、都逐日记录**，把选择问题推给前瞻数据，而非现在。

### 2.3 产物与完整性

- 模型：`data/models/expert_a_relrank_v1.pkl`（含两条臂 + 训练元数据）
- 元数据：`data/models/expert_a_relrank_v1.meta.json`（含 pkl 的 sha256、样本 sha、n_train）
- **不进 `data/model_registry/registry.json`**：A 未获生产批准，登记会污染主证据链。
- `shadow_policy` 装载时**逐字节校验 sha256**，不符即拒用并在记录里标注原因（不静默降级）。

---

## 三、逐日记录口径（写进 `shadow_actions.jsonl` 的新字段 `a_relrank`）

每条基金记录新增：

```json
"a_relrank": {
  "model_sha256": "<pkl sha 前 12 位>",
  "frozen_at": "2026-09-13",
  "n_train": 3371,
  "horizon": 5,
  "label_basis": "same_day_cross_section_excess",
  "usage_lock": "relative_pool_only_no_abs_display",
  "arms": {
    "a_mean": {"score": 0.0123, "rank_in_pool": 1, "n_pool": 4},
    "a_med":  {"score": 0.0119, "rank_in_pool": 1, "n_pool": 4}
  },
  "mirror": {
    "est_chg": -1.0518,
    "diff_vs_pool_mean": +1.2345,
    "score_15": 12,
    "source": "decision_engine._score_relative（真实现，只读调用）",
    "semantics_note": "post 槽 est_chg 与 14:55 est_return 同为「当日重仓估算涨跌%」"
  },
  "divergence": "none | a_bullish_live_bearish | a_bearish_live_bullish"
}
```

- **`rank_in_pool`**：池内 4 只按该臂 score 降序排名（1 = 最看好）；同日池内比较，
  无未来数据参与。
- **`mirror`**：调用 **production 的 `_score_relative` 真实现**（不复制公式），
  输入用当日 post 槽的 `est_chg` 横截面。惰性 import + try/except 包裹：
  若该私有函数签名变化，只写 `{"error": ...}`，**绝不影响 shadow 主流程**。
- **`divergence`**（兑现 Q4）：A 两臂一致方向 vs `diff_vs_pool_mean` 符号相反时标注；
  不调和、不改分。符号判据：`sign(A_mean.score) != sign(diff) → divergence`，
  差值为 0 时记 `none`。
- **`contains_future_labels: false`** 对 `a_relrank` 同样成立：输入仅当日 post 特征。

---

## 四、判据（≥20 个交易日后才允许评估，先写死）

积累满 **20 个交易日**后，用这批**前瞻**记录判：

| 判据 | 内容 |
|---|---|
| P1 | A 的池内排名 与 后续 5 日真实同日超额收益的 rank 相关 > 0（配对 bootstrap，日块重抽样，CI 下界 > 0） |
| P2 | 逐日不劣于 `mirror.score_15` 的排名能力（≥3/4 的周分组） |
| P3 | 两臂（a_mean / a_med）结论不一致时**不得**择优报告，须并列 |

**20 日不足或判据不过 → 停在 ①，不升 ②/③。** 只有 P1~P3 全过，才另起预注册谈 ②/③
（含权重 w 与 T+5/T+1 周期错配的显式声明）。

---

## 五、边界（本文件明确不做）

- ❌ 不改 `core/decision_engine.py`（`_score_relative`、`_MAX_BY_DIM`、任何权重）；
- ❌ 不改 `config.json`、不改 `data/model_registry/registry.json`；
- ❌ 不把 A 引入绝对收益展示、`forecast_engine` 对外输出、或下单链路（守 G2-⑤′）；
- ❌ 不新建记录文件（复用 `shadow_actions.jsonl`，兑现 Q2）；
- ❌ 不在判据未过时动分数。

---

## 六、复现

- 冻结脚本：`experiments/forecast_lab/freeze_expert_a_relrank.py`
- 记录侧：`shadow_policy.py` 的 `load_expert_a()` / `expert_a_shadow()`
- 接口核实点：`core/decision_engine.py:37-38`（权重）、`:129-149`（`_score_relative`）；
  `core/forecast_engine.py` `FEATURE_KEYS`；`run_m0_power.py:82-108`（掩码矩阵与拟合件）

—— 预注册 · 2026-09-13 夜（实现前落盘）
