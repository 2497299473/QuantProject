"""14:55 PIT Forecast 契约（V4.4 步 1，2026-09-22）。

单一事实源：定义「14:55 可观测状态」的精确语义 + 由此推导的 label 分母口径。
本模块只做纯函数与常量，不读文件、不发网络、不改任何现有回测管线——
构建器（pit1455_dataset.py）与后续步骤都从这里取口径，避免两处漂移。

背景（Summer 拍板 + GPT 评审核出的三 P0 之一）：
1. label 分母：现行 fwd = navs[i+h]/navs[i] - 1，其中 navs[i] 是 **T 日最终 NAV**。
   但用户在 14:55 决策时 T 日 NAV 尚未公布（约当日 21~22 点才出）。以「当时看不到的
   价」为分母，等于让 label 的基准混入 T 日尾盘未观测信息 → 语义不纯。
   拍板改为「从 14:55 可观测状态起算」：分母 = 14:55 时对 T 日 NAV 的最优估计。

2. est_chg 量纲错位（本次核出的硬 P0，见文件末常量）：
   - 训练/冻结样本 est_chg 是「小数值」（backtest_spread 用 closes 比值 - 1）；
   - live/shadow 特征 est_chg 是「百分数」（腾讯行情 parts[32] 涨跌幅）。
   同一个 key 差 100×，72 条 live post 行 66 条（91.7%）落在训练支撑域外。
   本契约把量纲统一钉死为 fraction（小数），并给出唯一换算桥 est_chg_from_pct()。
   ⚠️ 本步只登记桥函数与常量、不改 run.py/shadow 的现有产出（避免一把梭）；
      接入是步 2 的事，届时所有消费点统一走 est_chg_from_pct，禁止就地 *100/÷100。

口径定义（唯一权威，勿在他处重写）：
    nav_hat_1455(T) = navs[T-1] * (1 + est_chg_fraction)
    fwd_from_1455(h) = navs[T+h] / nav_hat_1455(T) - 1
其中 est_chg_fraction 为「T 日重仓加权涨跌」的小数量纲（如 -0.0123 = -1.23%），
navs[T-1] 是 14:55 时点最后一个「已公布」净值（= T 的前一交易日 NAV）。
"""
from __future__ import annotations

CONTRACT_VERSION = "pit1455-v1"

# ---- est_chg 量纲钉死：全链唯一真源（fraction，非 percent）----
EST_CHG_UNIT = "fraction"
"""forecast_engine.FEATURE_KEYS['est_chg'] 的权威量纲：小数值（0.0123 == 1.23%）。"""


def est_chg_from_pct(pct) -> float | None:
    """百分数涨跌幅 → 契约量纲 fraction。None 透传（缺失由 B1 mask 处理）。

    这是全链唯一允许的「% → 小数」换算入口；调用方（live/shadow 侧）
    产出 est_chg 特征前必须过此函数，禁止各自 * 0.01。
    """
    if pct is None:
        return None
    return float(pct) / 100.0


def estimate_nav_at_1455(prev_nav, est_chg_fraction) -> float | None:
    """14:55 时对 T 日 NAV 的最优估计 = 已公布的前一净值 ×(1+估算涨跌小数)。

    prev_nav: navs[T-1]（14:55 可见的最后一支已公布净值）
    est_chg_fraction: fraction 量纲的 T 日加权估算涨跌（见 EST_CHG_UNIT）
    任一为 None / prev_nav<=0 → 返回 None（调用方据此决定该样本 label 不可算）。
    """
    if prev_nav is None or est_chg_fraction is None:
        return None
    base = float(prev_nav)
    if base <= 0:
        return None
    return base * (1.0 + float(est_chg_fraction))


def fwd_from_1455(nav_hat_1455, future_nav) -> float | None:
    """以 14:55 可观测估计 NAV 为分母的未来 h 日收益 label（契约核心式）。

    nav_hat_1455: estimate_nav_at_1455 的输出（分母，>0）
    future_nav:   navs[T+h]（未来第 h 个交易日的已公布净值）
    任一为 None / 分母<=0 → None。
    """
    if nav_hat_1455 is None or future_nav is None:
        return None
    denom = float(nav_hat_1455)
    if denom <= 0:
        return None
    return float(future_nav) / denom - 1.0


def contract_provenance() -> dict:
    """供数据集 meta / 报告透传：本批 label 使用的契约口径与版本。"""
    return {
        "contract_version": CONTRACT_VERSION,
        "label_denominator": "nav_hat_1455",
        "label_semantics": "fwd_h = navs[T+h]/(navs[T-1]*(1+est_chg)) - 1",
        "est_chg_unit": EST_CHG_UNIT,
        "decided_on": "2026-09-22",
    }
