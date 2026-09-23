# evidence/ · 证据落档骨架（V4.5，2026-09-23）

## 这是什么

**可复现验证证据的归档层**：把「某轮验证怎么跑、跑出什么、当时的输入是什么」
固化成可逐字复核的文件，与代码、数据快照三者交叉锁定。

在此之前，验证报告散落在 `output/*.log`（**gitignored**）——`output/` 的忽略规则是
「高翻动运行产物」，但验证报告恰恰**不是**高翻动产物，它是结论的依据。后果已实测：

- `data/model_registry/registry.json` 里两个模型绑定的验证报告
  （`output/backtest_forecast_v8_quantile_20260830.log`、
  `output/backtest_forecast_v3_b1mask_20260831.log`）**在本地与 git 历史中都不存在**；
- 审计 `P1-2` 因此**常年 FAIL**：`verify_validation_report()` 拿不到报告，
  哈希无从比对，「模型 ↔ 验证」的密码学绑定**实际断链**。

本目录就是这条断链的落点。

## 目录约定

```
evidence/
  README.md                  ← 本文件：口径与纪律（唯一说明来源）
  INDEX.md                   ← 索引：每条证据一行（时间 / 主题 / 文件 / 状态）
  validation/                ← 验证报告（backtest_forecast / walk_forward / quantile …）
    <topic>_<YYYYMMDD>.log      原始输出，逐字节落档，不改写
    <topic>_<YYYYMMDD>.meta.json 运行元数据（输入指纹 / commit / 命令 / 结论）
  probes/                    ← 一次性探针与对照实验的输出
  incidents/                 ← 事故与修复的取证材料
```

## 落档纪律

1. **原文不改**：报告按原始输出逐字节落档。要加解释，另起 `*.meta.json` 或
   `INDEX.md` 的一行——**绝不**在证据文件里补注释（改过就不可复现）。
2. **元数据必带四件**：`command`（怎么跑的）、`inputs`（消费了哪份冻结件 / 快照，
   带 sha256）、`code_commit`（当时代码版本）、`verdict`（结论与是否绑 registry）。
   取不到就诚实写 `null` 并附 `note`——**缺失留痕优于伪造值**。
3. **G-A / G-B 状态原样记录**：冻结件的内部完整性（G-A）与 K 线指纹可比性（G-B）
   必须照写，**不论 PASS**。G-B 为 DRIFTED / INCOMPLETE 时该轮数字**不得**与历史
   锚定轮次直接比——这条是 09-22 重跑留档反复踩到的坑。
4. **不绑 registry 的轮次显式声明**：只留档证据、不做 `bind_validation` 的轮次要
   在 `meta.verdict.bound_to_registry = false` 写明理由（例如绑定会覆盖现行授权链）。
5. **隐私**：证据文件里不得出现真实持仓份额 / 成本 / 账户金额。基金**代码**在
   验证报告里是公开研究标的（非持仓隐私），可原样落档；持仓语义只进
   `holdings.json`（gitignored）。

## 与其它层的关系

```
data/manifest.json（数据集快照）
        ↓ hash
registry.json（模型 provenance：权重 sha + 快照 sha + commit）
        ↓ 指向
evidence/validation/<report>（本目录：验证怎么跑的）
        ↓ 交叉引用
git commit（代码版本）
```

四者环环相扣才叫 Evidence Contract；缺任何一环，「这个模型凭什么上线」就答不完整。
`evidence/` 的加入补的是最后一环。

## 为什么不放 output/

`output/*.log` 与 `output/*.md` 在 `.gitignore` 里（高翻动运行产物），例外只有
`output/forecast_lab_prereg_*.md`（预注册判据）。验证报告**不属于高翻动产物**，
放进 `output/` 要么被忽略（当前实况）、要么为它开一堆例外（口径混乱）。
独立目录 + 独立纪律更清楚。
