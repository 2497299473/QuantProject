# -*- coding: utf-8 -*-
"""core/path_forecast.py v1.3（2026-09-01，σ 窗口改基金级交易日口径）。

变更（相对 v1.2，逐段最小改动）：
- P0 修复（外部审阅 2026-09-01）：v1.2 的「最近 20 条」在 (基金×日期)
  pooled 样本上是 20 个混合行——4 基金池下 ≈ 5 个交易日，且窗口的基金
  构成随各基金历史长度失衡。v1.3 改基金级：逐基金 (fund, date) 去重
  （同键保留最后一条；生产数据一键一行时为恒等），每基金按日期取最近
  RECENT_WINDOW=20 个交易日，跨基金等权拼接成 σ 窗口；
- fit_path_params / sigma_source / forecast_path 新增 fund_code：给定则
  μ/σ 全部用该基金自己（μ 基金全样本均值、σ 基金最近 20 个交易日）；
  meta 增 sigma_scope（fund_pooled / fund:<code>）；
- sigma_src 标签沿用（recent20/full/bucket_full），含义改为「基金级
  20 交易日窗口 / 对应全样本回退」；
- 保留 v1.2 依据：09-01 分位数校准显示全样本 σ 对 OOS regime 偏小
  （mdd_q10 尾部低估 25.6% vs 10%、mfe_q50 上行低估 82.7% vs 50%）；
- RECENT_WINDOW=20 自 v1.3 起冻结（该值系看过 09-01 OOS 结果后选定，
  带后验调参性质）；调整需走 v1.4 重新预注册验证。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

try:
    import numpy as np
    _NUMPY = True
except Exception:  # pragma: no cover
    _NUMPY = False

DEFAULT_N_PATHS = 2000
DEFAULT_SEED = 42


@dataclass
class PathForecast:
    """单周期路径级预测输出。"""
    horizon: int
    e_return: float = 0.0            # 终点收益期望（模拟均值）
    q10: float = 0.0
    q90: float = 0.0
    mdd_q10: float = 0.0             # MDD 分布 10% 分位（最差端，负值）
    mdd_q50: float = 0.0
    mfe_q50: float = 0.0             # MFE 分布 50% 分位（正值）
    mid_path: list[float] = field(default_factory=list)   # 中点路径（逐日累计收益）
    model_ready: bool = False        # False = 占位（numpy 缺失/参数不足）
    meta: dict = field(default_factory=dict)   # v1.1：{bucket, n_state, fallback}；v1.2 +sigma_src


def _window_mdd(nav_path: list[float]) -> float:
    """路径窗口内最大回撤（含起点的相对回撤，负值或 0）。"""
    peak = nav_path[0]
    mdd = 0.0
    for v in nav_path:
        if v > peak:
            peak = v
        dd = v / peak - 1 if peak > 0 else 0.0
        if dd < mdd:
            mdd = dd
    return mdd


def _window_mfe(nav_path: float, nav_path_list: list[float]) -> float:
    """路径窗口内最大有利波动（相对起点，正值或 0）。"""
    return max(nav_path_list) / nav_path - 1 if nav_path > 0 else 0.0


RECENT_WINDOW = 20    # σ 用最近 N 个交易日（基金级；v1.3 冻结，改动需 v1.4 重验证）
MIN_RECENT = 10       # 近期窗口可用下限


def _dedup_fund_day(samples: list[dict]) -> list[dict]:
    """(fund, date) 去重，同键保留最后一条，按 (fund, date) 排序返回。

    生产样本一键（一基金一交易日）一行时为恒等操作；仅影响同键重复行
    （合成数据/脏数据）。ISO 日期字符串排序即时间序。
    """
    seen: dict[tuple[str, str], dict] = {}
    for s in samples:
        if s.get("fwd1") is None:
            continue
        seen[(str(s.get("fund", "?")), str(s.get("date", "")))] = s
    return [seen[k] for k in sorted(seen)]


def _per_fund_window(deduped: list[dict], window: int) -> list[float]:
    """逐基金取最近 window 个交易日，跨基金等权拼接。

    修 v1.2 P0：pooled rets[-20:] 是 (基金×日期) 混合行（20 行 ≈ 5 个
    交易日），且长历史基金主导尾部；基金级窗口每基金最多贡献 window 条。
    """
    by_fund: dict[str, list[float]] = {}
    for s in deduped:                        # 已按 (fund, date) 排序
        by_fund.setdefault(str(s.get("fund", "?")), []).append(float(s["fwd1"]))
    out: list[float] = []
    for f in sorted(by_fund):
        out.extend(by_fund[f][-window:])
    return out


def _sigma_from_window(window: list[float], full: list[float], mu: float,
                       fallback_label: str,
                       window_size: int = RECENT_WINDOW) -> tuple[float, str]:
    """近期窗口 σ；窗口退化（< MIN_RECENT 或零方差）回退全样本 σ。"""
    if len(window) >= MIN_RECENT:
        r_mu = sum(window) / len(window)
        r_var = sum((r - r_mu) ** 2 for r in window) / (len(window) - 1)
        if r_var > 1e-18:
            return math.sqrt(r_var), f"recent{window_size}"
    var = sum((r - mu) ** 2 for r in full) / (len(full) - 1)
    return math.sqrt(var), fallback_label


def sigma_source(samples: list[dict], fund_code: str | None = None) -> str:
    """返回 σ 的来源标记（recent20/full/none），与 fit_path_params 口径一致。"""
    deduped = _dedup_fund_day(samples)
    if fund_code is not None:
        full = [float(s["fwd1"]) for s in deduped
                if str(s.get("fund", "?")) == fund_code]
    else:
        full = [float(s["fwd1"]) for s in deduped]
    if len(full) < 30:
        return "none"
    mu = sum(full) / len(full)
    window = (full[-RECENT_WINDOW:] if fund_code is not None
              else _per_fund_window(deduped, RECENT_WINDOW))
    return _sigma_from_window(window, full, mu, "full")[1]


def fit_path_params(samples: list[dict], horizon: int,
                    daily_window: int = RECENT_WINDOW,
                    fund_code: str | None = None) -> tuple[float, float] | None:
    """从样本历史日收益估计 (μ_daily, σ_daily)。

    samples 需含逐日收益字段 fwd1（load_samples 输出：一键 = 一基金一
    交易日的 1 日收益）。v1.3 基金级口径（修 v1.2 P0：rets[-20:] 取的是
    (基金×日期) 混合行——4 基金池下 20 行 ≈ 5 个交易日，且窗口基金构成
    随各基金历史长度失衡）：
    - fund_code=None（池口径）：μ = 去重全样本均值；σ = 逐基金
      (fund, date) 去重、每基金最近 daily_window 个交易日、跨基金等权
      拼接窗口的标准差；
    - fund_code 给定（单基金口径）：μ/σ 只用该基金——μ 该基金全样本
      均值、σ 该基金最近 daily_window 个交易日。
    窗口退化（< MIN_RECENT 或零方差）回退对应全样本 σ（标签 full）。
    09-01 校准依据见模块 docstring；RECENT_WINDOW 自 v1.3 起冻结，
    调整需走 v1.4 重新验证（防后验调参）。
    返回 None = 数据不足（去重后 <30 条）。
    """
    deduped = _dedup_fund_day(samples)
    if fund_code is not None:
        full = [float(s["fwd1"]) for s in deduped
                if str(s.get("fund", "?")) == fund_code]
    else:
        full = [float(s["fwd1"]) for s in deduped]
    if len(full) < 30:
        return None
    mu = sum(full) / len(full)
    window = (full[-daily_window:] if fund_code is not None
              else _per_fund_window(deduped, daily_window))
    sigma, _src = _sigma_from_window(window, full, mu, "full",
                                     window_size=daily_window)
    if sigma <= 1e-9:
        return None
    return mu, sigma


def simulate_path(horizon: int, mu: float, sigma: float,
                  rng: random.Random) -> list[float]:
    """模拟一条逐日净值路径（起点=1.0，含起点共 horizon+1 点）。"""
    nav = 1.0
    path = [nav]
    for _ in range(horizon):
        nav *= (1.0 + rng.gauss(mu, sigma))
        path.append(nav)
    return path


def _mc_stats(horizon: int, mu: float, sigma: float,
              n_paths: int, seed: int) -> dict:
    """蒙特卡洛统计量（ends/mdds/mfeqs/mid_path），forecast_path 与条件版共用。"""
    rng = random.Random(seed)
    ends = np.empty(n_paths, dtype=float)
    mdds = np.empty(n_paths, dtype=float)
    mfeqs = np.empty(n_paths, dtype=float)
    for k in range(n_paths):
        p = simulate_path(horizon, mu, sigma, rng)
        ends[k] = p[-1] - 1.0
        mdds[k] = _window_mdd(p)
        mfeqs[k] = _window_mfe(p[0], p)
    mid_path = list(simulate_path(horizon, mu, 0.0, rng))   # 中点路径=确定性 μ 路径
    return {"ends": ends, "mdds": mdds, "mfeqs": mfeqs, "mid_path": mid_path}


def _mc_forecast(horizon: int, mu: float, sigma: float,
                 n_paths: int, seed: int, meta: dict) -> PathForecast:
    """由 μ/σ 构建 PathForecast（v1.1 条件版与全局版共用）。"""
    st = _mc_stats(horizon, mu, sigma, n_paths, seed)
    return PathForecast(
        horizon=horizon,
        e_return=float(st["ends"].mean()),
        q10=float(np.percentile(st["ends"], 10)),
        q90=float(np.percentile(st["ends"], 90)),
        mdd_q10=float(np.percentile(st["mdds"], 10)),
        mdd_q50=float(np.percentile(st["mdds"], 50)),
        mfe_q50=float(np.percentile(st["mfeqs"], 50)),
        mid_path=[round(v - 1.0, 6) for v in st["mid_path"]],
        model_ready=True,
        meta=meta,
    )


def forecast_path(samples: list[dict], horizon: int,
                  n_paths: int = DEFAULT_N_PATHS,
                  seed: int = DEFAULT_SEED,
                  fund_code: str | None = None) -> PathForecast:
    """路径级预测主入口。数据不足时返回 model_ready=False 占位。

    fund_code 给定 → 单基金口径（μ/σ 只用该基金，sigma_scope=fund:<code>）；
    缺省 → 池口径（基金级窗口等权拼接，sigma_scope=fund_pooled）。
    """
    if not _NUMPY:
        return PathForecast(horizon=horizon, model_ready=False)
    params = fit_path_params(samples, horizon, fund_code=fund_code)
    if params is None:
        return PathForecast(horizon=horizon, model_ready=False)
    mu, sigma = params
    return _mc_forecast(horizon, mu, sigma, n_paths, seed,
                        meta={"bucket": "global", "n_state": None, "fallback": False,
                              "sigma_src": sigma_source(samples, fund_code),
                              "sigma_scope": (f"fund:{fund_code}" if fund_code is not None
                                              else "fund_pooled")})


def path_forecast_cn(pf: PathForecast) -> dict:
    """PathForecast → 中文报告字典。"""
    if not pf.model_ready:
        return {"horizon": pf.horizon, "model_ready": False,
                "note": "路径模拟未就绪（数据不足）"}
    return {
        "horizon": pf.horizon,
        "model_ready": True,
        "e_return": round(pf.e_return, 4),
        "q10": round(pf.q10, 4), "q90": round(pf.q90, 4),
        "mdd_q10": round(pf.mdd_q10, 4), "mdd_q50": round(pf.mdd_q50, 4),
        "mfe_q50": round(pf.mfe_q50, 4),
        "mid_path": pf.mid_path,
        "bucket": pf.meta.get("bucket", "global"),
        "fallback": pf.meta.get("fallback", False),
        "sigma_src": pf.meta.get("sigma_src", "full"),
        "sigma_scope": pf.meta.get("sigma_scope", "fund_pooled"),
        "note": "蒙特卡洛路径模拟（日收益 N(μ,σ)；μ 全样本、σ 基金级最近 20 交易日 v1.3，状态条件化 v1.1），观察层非动作依据",
    }



# ---------- v1.1（2026-08-31，GPT 四审 P0）：state-conditioned μ/σ ----------
# 把全局日收益参数升级为「按结构状态分桶」：up / down / consolidation。
# 样本需带 state_trend（experiment_state_feature.add_state_features 产出：
#   1=up, -1=down, 0=consolidation/expand/na）；缺失或桶样本不足 → 回退全局。
STATE_TREND_BUCKETS = {1: "up", -1: "down", 0: "consolidation"}
TREND_TO_NUM = {"up": 1, "down": -1, "consolidation": 0}


def fit_path_params_by_state(samples: list[dict], horizon: int,
                             min_n: int = 20) -> dict:
    """按状态分桶估计 (μ_daily, σ_daily)。返回 {bucket: (mu, sigma, n, sigma_src)}。

    v1.3：桶内同样基金级——(fund, date) 去重后，每基金取该桶内最近
    RECENT_WINDOW 个状态日、跨基金等权拼接为窗口；窗口退化（< MIN_RECENT
    或零方差）回退桶全样本 σ（bucket_full），μ 用桶全样本均值——与全局
    口径一致。n 为桶内去重后样本数。
    """
    from collections import defaultdict
    groups: dict[str, list[dict]] = defaultdict(list)
    for s in _dedup_fund_day(samples):
        bucket = STATE_TREND_BUCKETS.get(s.get("state_trend"), "global")
        groups[bucket].append(s)
    out = {}
    for b, rows in groups.items():
        if len(rows) < min_n:
            continue
        rets = [float(s["fwd1"]) for s in rows]
        mu = sum(rets) / len(rets)
        window = _per_fund_window(rows, RECENT_WINDOW)
        sigma, src = _sigma_from_window(window, rets, mu, "bucket_full")
        if sigma > 1e-9:
            out[b] = (mu, sigma, len(rows), src)
    return out


def forecast_path_conditional(samples: list[dict], horizon: int,
                              trend: str = "up",
                              n_paths: int = DEFAULT_N_PATHS,
                              seed: int = DEFAULT_SEED,
                              min_n: int = 20) -> PathForecast:
    """state-conditioned 路径预测 v1.1：用指定状态桶 μ/σ 做蒙特卡洛。

    trend ∈ {"up", "down", "consolidation"}；桶样本不足或状态缺失 → 回退全局
    （v1.0 行为），meta.fallback=True 如实标注。回答「当前市场状态下，未来
    路径分布是否与全局分布不同」。
    """
    if not _NUMPY:
        return PathForecast(horizon=horizon, model_ready=False)
    params = fit_path_params_by_state(samples, horizon, min_n)
    bucket = STATE_TREND_BUCKETS.get(TREND_TO_NUM.get(trend), "global")
    if bucket in params:
        mu, sigma, n, src = params[bucket]
        meta = {"bucket": bucket, "n_state": n, "fallback": False,
                "sigma_src": src, "sigma_scope": "fund_pooled"}
    else:
        g = fit_path_params(samples, horizon)
        if g is None:
            return PathForecast(horizon=horizon, model_ready=False)
        mu, sigma = g
        meta = {"bucket": "global", "n_state": 0, "fallback": True,
                "sigma_src": sigma_source(samples), "sigma_scope": "fund_pooled"}
    return _mc_forecast(horizon, mu, sigma, n_paths, seed, meta)
