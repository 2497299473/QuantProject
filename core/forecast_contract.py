"""Forecast Contract（重定，V4.4，2026-09-23）——「到底什么叫一次有效的预测」的唯一权威。

为什么存在
==========
步 3 验收矩阵（2026-09-22，commit b1fcd22）暴露的语义矛盾：14:55 PIT 口径
（pit1455-v1）把 est_chg 放进 label 分母，而 est_chg 同时是模型输入特征 ⇒
机械耦合——零成本公式「预测分 = −est_chg」不训练就有 OOS RankIC +0.1278
（T+1，n=923），几乎全是算术，不含未来信息。P0-1 两轨 decision_edge
（2026-09-23，commit 5925a3e）在 old 口径下再次确认：模型相对 est_chg 单因子
无稳定超额排序信息（CI 全跨零）。「预测口径」与「成交的经济含义」从未在同一
条轴上对齐——本契约一次性回答「什么叫一次有效的预测」。

经济事实（唯一前提）
==========
用户在 T 日 14:55 下单，基金按 **T 日最终 NAV**（当日约 21~22 点公布）成交
⇒ **真执行价 = navs[T]**。14:55 的估计价 nav_hat 只是「决策时你以为付的价」，
不是实际付的价。预测要「有效」，必须相对执行价来量——否则量的是「决策时估值
估偏了多少」（那叫估值误差，不是预测能力）。

拍板（方向 (a)——证据偏向，2026-09-23 Summer 批准起草本契约文本）
==========
**C1 裁决 label（权威 = 生产口径，不变）**：
    fwd_h = navs[T+h] / navs[T] − 1
    分母为 T 日最终 NAV（= 真执行价）。est_chg **不进 label**——输入/输出
    耦合在定义上被拆掉。
**C2 14:55 的角色（从 label 分母降级为辅助口径）**：
    nav_hat_1455(T) = navs[T−1] × (1 + est_chg)
    只用于 ① 决策时点估值（盈亏核算基准）② 相对「决策时你以为付的价」的
    事后盈亏核算。**不进 label、不进训练目标、不进晋升门禁。**
    式子权威仍是 pit1455-v1（本模块委托，不重写公式，防两处漂移）。
**C3 裁决基线**：
    模型必须与 est_chg 单因子比较：
        decision_edge = RankIC(模型) − RankIC(est_chg)
    （同 OOS 样本、同 C1 label、按日块 CI——实现见 backtest_forecast.py）。
    old 口径池级 OOS 基线 IC ≈ 0（T+1/3/5 = −0.013/+0.025/−0.009，n=923）
    ⇒ 与 0 比当下成立，但写死纪律为单因子（防未来口径漂移）。
    若未来把 1455 口径重新拿来做裁决目标，必须先预注册 −est_chg 基线，
    事后补无效（步 3 B 轨即反例：表观 IC +0.1139 全是算术耦合，超额 −0.0139）。
**C4 量纲**：
    est_chg = fraction（小数值，0.0123 = 1.23%）；全链唯一换算桥
    pit1455_contract.est_chg_from_pct()；切换日 2026-09-23
    （EST_CHG_LIVE_FRACTION_SINCE）。
**C5 证据存档（为什么 1455 不作裁决口径）**：
    ① 耦合恒等式 1+fwd1455 = (1+fwd_old)·(1+a_T)/(1+est_chg)（实测残差 5e-7，
       舍入级）；② 步 3 矩阵 9 格仅 1 格（C 轨 T+3 超额 +0.0203）跑赢基线；
    ③ decision_edge T+1 +0.0615 [−0.042,+0.169] / T+3 +0.0229 / T+5 −0.0106，
       CI 全跨零。⇒ 「1455 口径下模型有排序能力」不成立；old 口径是唯一
       经济上诚实的裁决轴。

边界（本批不动什么）
==========
零网络；不动模型权重 / 训练 / 门禁 / model_ready / 生产 label 公式
（生产 label 本就是 C1 口径，本契约是既有语义的形式化 + 角色降级 + 基线纪律）；
可回滚。09-26 议程的「是否重训 v4」不受本契约影响；「1455 口径去留三选一」
经本批起草后按 (a) 落为 forecast-v1——若未来选 (b)/(c)，须重开契约版本。
"""
from __future__ import annotations

CONTRACT_VERSION = "forecast-v1"

# ---- 口径标识（全链用这两个名字指代，禁止自由文本）----
CALIBER_ADJUDICATION = "old_final_nav"
"""C1：唯一权威裁决口径——分母 T 日最终 NAV（= 14:55 订单真执行价）。"""

CALIBER_ACCOUNTING = "pit1455"
"""C2：1455 核算口径——仅决策输入与事后盈亏核算，不作裁决目标。"""

# 1455 口径角色降级（字符串常量，供 contract test 骨架钉死）
PIT1455_ROLE = "decision_input_and_pnl_accounting_only"


def adjudication_label(nav_t, nav_t_h) -> float | None:
    """C1：唯一权威裁决 label fwd_h = navs[T+h]/navs[T] − 1。

    nav_t:   T 日最终 NAV（分母；= 14:55 订单真执行价，必须 > 0）
    nav_t_h: 未来第 h 个交易日 NAV（分子）
    est_chg 不是参数——输入/输出耦合在定义上拆掉（C1/C5）。
    None / nav_t<=0 → None（调用方标该样本 label 不可算，不得造数）。
    """
    if nav_t is None or nav_t_h is None:
        return None
    denom = float(nav_t)
    if denom <= 0:
        return None
    return float(nav_t_h) / denom - 1.0


def adjudication_baseline_score(est_chg_fraction, caliber: str = CALIBER_ADJUDICATION):
    """C3：模型必须跑赢的零成本单因子基线分（分数域，不是 IC）。

    CALIBER_ADJUDICATION（old 口径）：+est_chg；
    CALIBER_ACCOUNTING（1455 口径）：−est_chg（仅当已预注册时可用，见 C3）。
    None 透传（缺失由 B1 mask 处理，不造基线分）。未知口径 raise（fail-closed）。
    """
    if est_chg_fraction is None:
        return None
    est = float(est_chg_fraction)
    if caliber == CALIBER_ADJUDICATION:
        return est
    if caliber == CALIBER_ACCOUNTING:
        return -est
    raise ValueError(
        f"unknown caliber: {caliber!r}（期望 CALIBER_ADJUDICATION / CALIBER_ACCOUNTING）")


def accounting_label(nav_t_minus_1, est_chg_fraction, nav_t_h):
    """C2：1455 核算口径——以决策时估计价为分母的未来收益（只做盈亏核算，不作裁决）。

    委托唯一式子权威 pit1455_contract（estimate_nav_at_1455 + fwd_from_1455），
    本模块不重写公式（防两处漂移）。任一为 None → None。
    """
    from . import pit1455_contract as C
    nav_hat = C.estimate_nav_at_1455(nav_t_minus_1, est_chg_fraction)
    return C.fwd_from_1455(nav_hat, nav_t_h)


def contract_provenance() -> dict:
    """供数据集 meta / 报告透传：本契约的裁决口径与版本（与 pit1455-v1 并存）。"""
    from . import pit1455_contract as C
    return {
        "contract_version": CONTRACT_VERSION,
        "adjudication_label": "fwd_h = navs[T+h]/navs[T] - 1（T 日最终 NAV = 真执行价）",
        "adjudication_baseline": "est_chg 单因子（decision_edge = RankIC(模型) - RankIC(est_chg)）",
        "pit1455_role": PIT1455_ROLE,
        "pit1455_contract_version": C.CONTRACT_VERSION,
        "est_chg_unit": C.EST_CHG_UNIT,
        "decided_on": "2026-09-23",
    }
