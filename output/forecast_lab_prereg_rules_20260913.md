# 规则预注册 · D1-c 双端点双门槛 + D2 晋升降级口径（2026-09-13）

> 依据 Summer 2026-09-13 16:59 拍板：D1 = **c（双端点双门槛）**、D2 = **预注册降级口径**、
> D3 随 D1-c（候选 C 保留待定，B 维持出局，D 排 PR-20260911-01 之后）。
> 本文**先于实现落盘**（登记表纪律：先把判据写死 → 再实现 → 再跑 → 再裁决，不得临场改规则）。
> 修订记录只追加，不覆盖。

---

## 一、R1 · 准入门双端点双门槛（D1-c）

### 1.1 两端点定义

| 端点 | 尺子 | 含义 |
|---|---|---|
| **primary** | 绝对 `fwd5` pooled WF RankIC（P1/M0 同协议） | 与生产绝对预测同语义，**决策层展示资格的唯一尺子** |
| **secondary** | 相对标签（`rel_mean`）pooled WF RankIC；**所有臂（含 base）共用同一把尺子** | 横截面「选谁」能力；只授予**相对排序专用资格** |

（候选 A 实测：primary 最佳臂 A_med Δ=+0.0139 / pct2.5(D)=−0.0420；secondary
Δ=+0.0703 / pct2.5(D)=+0.0158 / 逐折 4/4。见 `output/forecast_lab_cand_a_20260913.md`。）

### 1.2 门槛（写死）

- **G1 绝对资格（进决策层展示）**：现行三门槛逐字不变，只在 **primary** 端点判——
  ① 点估计 ≥ 同冻结样本当次复算基线；② 配对 cluster bootstrap 差值 CI 下界 > 0；
  ③ baseline hierarchy 逐级不劣于。
- **G2 相对资格（仅限横截面用途）**：只在 **secondary** 端点判，四条同时成立——
  ②′ pct2.5(D) > 0（配对 CI 下界，B=1000/seed=42 不变）；
  ③′ 逐折不劣于 **4/4**；
  ④′ **跨尺子稳健**：第二把相对尺子（`rel_med`）上 Δ 同号且 pct2.5(D)>0；
  ⑤′ **用途锁定**：G2 通过者**不得**进入绝对收益展示/下单链路，只能作为
  `decision_engine` 中 `relative_pool` 维（权重 0.15）的候选输入，接入时另行预注册。
- **判读纪律**：两端点结论**并列报告，不互相折算、不取均值**；G2 过而 G1 不过
  不是「接近通过」，是两个不同问题的答案。
- **功效声明义务**：任何「未判出」结论必须同时报告该臂 MDE 档（由 ρ_IC vs base
  查 0912 §三映射表）；Δ 落在盲区内只准记「证据不充分」，禁记「无效果」。
- **实现落点（本条先登记、暂不实现）**：待有候选真到过 G2 接入环节，再把上述
  判据代码化为 `run_a_relrank.py` 的结论段或独立裁决脚本——本阶段用人工比对，
  避免为用不上的自动化引入新代码面。

## 二、R2 · 晋升/Shadow 降级口径（D2）

### 2.1 问题（已证事实）

`derive_promotion` v1 要求「三周期 CI 下界全部 > 0」，在 n≈900 / 306 日块下对
T+1/T+3 结构性不可达（单模型 IC 日块 sd≈0.031 → 需 |IC|≳0.065；现值 0.005/0.002）。
后果：`verify_approval` 永假 → `shadow_policy` 每交易日拒记（09-14 起持续失血），
36 条历史样本已判废 → 前瞻证据链断裂。

### 2.2 降级规则（写死）

- **registry 主授权不动**：promotion 仍按 v1「三周期全过」推导，v3 维持 `blocked`，
  `config.model_ready` 维持 false，**对外展示/决策链行为零变化**。
- **新增旁路文件** `data/promotion_prereg.json`：把降级授权变成**可哈希审计的
  预注册工件**，登记内容五件套——model sha256、validation report sha256、
  降级判据文本、失效日期（expiry）、授权人（Summer 2026-09-13 拍板）。
- **降级判据 `promotion_shadow_v1`**（比 v1 弱、比放行严，逐条写死）：
  1. T+5 `decision=approved` 且 CI 下界 > 0（现有最强周期必须有显著证据）；
  2. T+1 / T+3 `rank_ic ≥ 0`（点估计不为负即可，**不要求**显著）；
  3. registry 中模型 sha256 与本文件登记值逐字节一致；
  4. 本文件 `expiry` 未过期；过期即回到全阻断。
- **作用域隔离（硬边界）**：降级状态**只**授予 `shadow_policy.py` 的纸面记录资格；
  `verify_approval()`（对外展示门）**不读取本文件、行为不变**。降级记录每条必须
  内嵌 `promotion_mode: "prereg_degraded"` + 触发原因，与完整批准样本可区分；
  两套证据**永不混池**。
- **回滚**：删除/清空 `promotion_prereg.json` 或改 `enabled:false` → shadow 回到
  拒记态；代码路径无其他状态残留。

### 2.3 实现落点（本次要做的最小改动）

| 文件 | 改动 |
|---|---|
| `core/model_registry.py` | 新增 `evaluate_prereg_degradation(pkl_name) -> (ok, reason)` 纯校验函数（读旁路文件 + 五件套核对）；不动 `derive_promotion` / `verify_approval` |
| `shadow_policy.py` | 闸门改为：`model_approved` 完整批准 **或** `evaluate_prereg_degradation` 通过；后者通过时记录打 `promotion_mode` 标记 |
| `data/promotion_prereg.json` | 新建，登记 forecast_v3 现值 + expiry=**2026-10-12**（30 天，到期须复核续期，与季度重估 09-26 天然衔接） |
| `tests/test_model_registry.py` / `tests/test_shadow_policy.py` | 各补降级路径测例：过期拒、sha 不符拒、T+5 非 approved 拒、T+1 负 IC 拒、`verify_approval` 不受影响 |

**不做**：不动 config / registry.json 任何字段；不改 `derive_promotion` v1；不碰定时任务。

## 三、R3 · 生效与复核节点

- 本规则 2026-09-13 落盘；R2 代码改动当晚合入 → **09-14（周一）22:30 Shadow
  应首次产出 `prereg_degraded` 记录**。
- expiry 2026-10-12 前必须复核：届时若 09-16~18 裁决窗口已定新判据或候选 D 已扩容，
  以新口径续期或废止，**禁止静默自动续期**。
- 与 `deploy/preregistered_items.md` 登记表同步挂条（只增不改）。

—— OpenSquilla · 2026-09-13 22:3x +08:00（Summer 拍板 D1-c / D2 / D3）
