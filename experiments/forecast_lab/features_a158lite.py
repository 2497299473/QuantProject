"""P1-② A158-lite 特征模块（2026-09-10；Summer 09-09 11:18 拍板 P1 提前至 09-10）。

回答一个窄问题：「现有 v7 的 T+5 RankIC 上不去，是不是**特征瓶颈**？」
做法：取 Qlib Alpha158 的滚动/结构特征族（只取**时间序列族**，剔除需全市场横截面的项），
PIT 化后重算在**基金净值序列**上，得到 50 个纯净净值结构特征，与现有 7 特征对照。

口径（写死，不得事后放宽）
- 特征族 FAMILIES = roc/ma/std/rsqr/resi/rsv/rank/cntp/sump/corr（10 族）
- 窗口 WINDOWS = 5/10/20/30/60（5 档） → 10 × 5 = 50 个特征
- **纯净**：只用净值自身的滞后/滚动结构，不含 est_chg / macd / r20 / dd 等现有特征，
  也不含任何外部数据（零网络）。保住「是不是特征瓶颈」这一问题的干净性。
- PIT：调用方只传「截至 T-1 的已公布净值」（与 backtest_spread._nav_state_at 同口径——
  d 日净值约当日 21~22 点才公布，14:55 决策时不可见）。
- 短窗历史不足一律 NaN，**不缩窗**（缩窗会让不同日期的同名特征口径不一致）。
- 负下标防回绕：所有切片先做长度检查，绝不依赖 Python 负索引的静默回绕。

字段命名：a158_<family>_<window>，如 a158_roc_20。

CORR 的口径替代（诚实标注，不与 Alpha158 原始定义混谈）
Alpha158 的 CORR 是 corr(close, log(volume))；基金净值**没有成交量**，故本模块用
**收益率的滞后 1 阶自相关**替代（仍属结构族信息：动量 vs 均值回复）。该替代须在
实验报告中显式标注。

RSQR/RESI 口径：在**归一化价格** y_t = p_t / p_0（y_0 = 1）上对 t 做最小二乘线性回归。
  rsqr = 1 - SSres/SStot（SStot == 0 时取 0.0）
  resi = y_last - fit_last（无量纲相对残差）
RSV：max == min（平台窗）时取中性 0.5，不产生 0/0。
STD：收益率总体标准差（ddof=0）。
RANK：窗内 ≤ 末值的比例 ∈ [0,1]（含并列）。

用法：
  sys.path.insert(0, "experiments/forecast_lab")
  from features_a158lite import feature_keys, compute_features, build_feature_rows
  feats = compute_features(nav_hist)          # nav_hist = 截至 T-1 的净值（升序）
  python experiments/forecast_lab/features_a158lite.py   # 离线自检（零网络，退出码 0/1）
"""
from __future__ import annotations

import math
import sys

# 特征族与窗口（写死；改这里等于改实验口径，必须同步报告与单测）
FAMILIES = ("roc", "ma", "std", "rsqr", "resi", "rsv", "rank", "cntp", "sump", "corr")
WINDOWS = (5, 10, 20, 30, 60)
FEATURE_PREFIX = "a158_"
_NAN = float("nan")


def feature_keys(windows=WINDOWS, families=FAMILIES) -> list[str]:
    """按窗口优先排序的 50 个特征名（顺序即矩阵列序，训练/推理须一致）。"""
    return [f"{FEATURE_PREFIX}{fam}_{w}" for w in windows for fam in families]


def _mean(xs) -> float:
    return sum(xs) / len(xs) if xs else _NAN


def _std0(xs) -> float:
    """总体标准差（ddof=0）。"""
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _pearson(xs, ys) -> float:
    """皮尔逊相关；任一侧方差为 0 时返回 0.0（不产生 0/0 → NaN 污染）。"""
    n = len(xs)
    if n < 2 or n != len(ys):
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    vx = sum(d * d for d in dx)
    vy = sum(d * d for d in dy)
    if vx <= 1e-15 or vy <= 1e-15:
        return 0.0
    cov = sum(a * b for a, b in zip(dx, dy))
    return max(-1.0, min(1.0, cov / math.sqrt(vx * vy)))


def _linfit_rsqr_resi(ys) -> tuple[float, float]:
    """对 t=0..n-1 做最小二乘直线拟合 → (rsqr, 末点残差)。

    用归一化价格（首点=1）保证量纲无关；SStot==0（全平）时 rsqr=0、resi=0。
    """
    n = len(ys)
    if n < 3:
        return _NAN, _NAN
    xs = list(range(n))
    mx, my = (n - 1) / 2.0, sum(ys) / n
    sxx = sum((t - mx) ** 2 for t in xs)
    if sxx <= 1e-15:
        return _NAN, _NAN
    sxy = sum((t - mx) * (y - my) for t, y in zip(xs, ys))
    slope = sxy / sxx
    inter = my - slope * mx
    ss_res = sum((y - (inter + slope * t)) ** 2 for t, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    rsqr = 0.0 if ss_tot <= 1e-15 else max(0.0, min(1.0, 1.0 - ss_res / ss_tot))
    resi = ys[-1] - (inter + slope * xs[-1])
    return rsqr, resi


def compute_features(nav_hist, windows=WINDOWS) -> dict[str, float]:
    """nav_hist = 截至 T-1 的**已公布**净值序列（时序升序，末位=最新）。

    PIT 纪律由调用方保证：只传 ≤ T-1 的净值。本函数只用尾部窗口，永远不看未来。
    返回全部 50 个特征；窗口历史不足（需要 w+1 个点算 w 期收益）时该窗全 NaN。
    """
    keys = feature_keys(windows)
    out = {k: _NAN for k in keys}
    # 显式长度检查——绝不依赖负索引回绕（n < w+1 时必须保持 NaN，而非静默取错值）
    seq = [float(x) for x in nav_hist]
    n = len(seq)
    if n < 2:
        return out                      # 连一个收益都算不出
    for w in windows:
        if w < 1 or n < w + 1:
            continue                    # 短窗历史不足：不缩窗，保持 NaN
        prices = seq[n - w - 1: n]      # w+1 个价格点 → w 期收益
        rets = [prices[t] / prices[t - 1] - 1.0 if prices[t - 1] > 0 else 0.0
                for t in range(1, w + 1)]
        last, first = prices[-1], prices[0]
        hi, lo = max(prices), min(prices)
        p0 = first if abs(first) > 1e-15 else 1.0
        norm = [p / p0 for p in prices]  # 归一化：首点 = 1

        rsqr, resi = _linfit_rsqr_resi(norm)
        out[f"{FEATURE_PREFIX}roc_{w}"] = (last / first - 1.0) if first > 0 else _NAN
        out[f"{FEATURE_PREFIX}ma_{w}"] = _mean(prices) / last if last > 0 else _NAN
        out[f"{FEATURE_PREFIX}std_{w}"] = _std0(rets)
        out[f"{FEATURE_PREFIX}rsqr_{w}"] = rsqr
        out[f"{FEATURE_PREFIX}resi_{w}"] = resi
        out[f"{FEATURE_PREFIX}rsv_{w}"] = (last - lo) / (hi - lo) if hi > lo else 0.5
        out[f"{FEATURE_PREFIX}rank_{w}"] = sum(1 for p in prices if p <= last) / len(prices)
        out[f"{FEATURE_PREFIX}cntp_{w}"] = sum(1 for r in rets if r > 0) / w
        out[f"{FEATURE_PREFIX}sump_{w}"] = sum(r for r in rets if r > 0)
        out[f"{FEATURE_PREFIX}corr_{w}"] = _pearson(rets[:-1], rets[1:]) if w >= 3 else _NAN
    return out


def build_feature_rows(samples, nav_by_fund, windows=WINDOWS):
    """样本 [(fund,date)] × 各基金净值序列 → {(fund,date): 特征dict}（PIT 对齐）。

    nav_by_fund: {code: [(date, nav), ...]}（升序）。
    对齐规则：样本日 d 若在序列下标 i，则用 navs[:i]（即截至 T-1）算特征——
    与 backtest_spread._nav_state_at 的 k = i-1 完全同口径。
    样本日不在序列中、或 i < 1（无 T-1 可用）→ 返回全 NaN 行（不静默丢弃样本）。
    """
    keys = feature_keys(windows)
    nan_row = {k: _NAN for k in keys}
    out = {}
    idx_cache = {}
    for s in samples:
        fund, d = s["fund"], s["date"]
        if fund not in nav_by_fund:
            out[(fund, d)] = dict(nan_row)
            continue
        navs = nav_by_fund[fund]
        if fund not in idx_cache:
            idx_cache[fund] = {str(dd): i for i, (dd, _v) in enumerate(navs)}
        i = idx_cache[fund].get(str(d))
        if i is None or i < 1:
            out[(fund, d)] = dict(nan_row)
            continue
        out[(fund, d)] = compute_features([v for _d, v in navs[:i]], windows)
    return out


# ------------------------------------------------------------------ 离线自检
def _selftest() -> int:
    ok = fail = 0

    def check(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  [FAIL] {name}")

    keys = feature_keys()
    check("特征数 = 50", len(keys) == 50)
    check("特征名唯一", len(set(keys)) == 50)
    check("命名含窗口", "a158_roc_20" in keys and "a158_corr_60" in keys)

    # 1) 短窗历史不足 → 全 NaN（不缩窗、不回绕）
    for n_bad in (0, 1, 2, 4):
        f = compute_features([1.0 + 0.001 * i for i in range(n_bad)])
        check(f"历史 {n_bad} → 全 NaN", all(math.isnan(v) for v in f.values()))
    # 恰好 w+1 个点：第 w 窗起可用，更大窗仍 NaN
    f = compute_features([1.0 + 0.001 * i for i in range(6)])       # n=6 → w=5 可用
    check("n=6 时 w=5 非 NaN", not math.isnan(f["a158_roc_5"]))
    check("n=6 时 w=10 为 NaN", math.isnan(f["a158_roc_10"]))

    # 2) 因果性：把 k 之后的数据改成巨值，k 处特征不变（无未来泄漏）
    base = [1.0 + 0.001 * i + 0.0005 * ((i * 37) % 11) for i in range(70)]
    for k in (40, 55, 69):
        f_k = compute_features(base[: k + 1])
        poisoned = base[: k + 1] + [999.0, 888.0, 777.0]
        f_p = compute_features(poisoned[: k + 1])
        same = all(
            (math.isnan(f_k[a]) and math.isnan(f_p[a]))
            or abs(f_k[a] - f_p[a]) < 1e-12 for a in keys)
        check(f"因果性 k={k}（未来扰动不影响）", same)

    # 3) 确定性与幂等
    check("确定性（两次同值）", compute_features(base) == compute_features(base))

    # 4) 已知值
    mono = [1.0 * (1.01 ** i) for i in range(65)]
    fm = compute_features(mono)
    check("单调升 → cntp=1", abs(fm["a158_cntp_20"] - 1.0) < 1e-12)
    check("单调升 → rank=1", abs(fm["a158_rank_20"] - 1.0) < 1e-12)
    check("单调升 → rsv=1", abs(fm["a158_rsv_20"] - 1.0) < 1e-12)
    check("单调升 → rsqr≈1", fm["a158_rsqr_20"] > 0.999)
    check("单调升 → roc>0", fm["a158_roc_20"] > 0)
    flat = [1.0] * 65
    ff = compute_features(flat)
    check("平台 → roc=0", abs(ff["a158_roc_20"]) < 1e-12)
    check("平台 → std=0", abs(ff["a158_std_20"]) < 1e-12)
    check("平台 → cntp=0", abs(ff["a158_cntp_20"]) < 1e-12)
    check("平台 → rsv=0.5", abs(ff["a158_rsv_20"] - 0.5) < 1e-12)
    check("平台 → rsqr=0（不产生 NaN）", ff["a158_rsqr_20"] == 0.0)
    check("平台 → corr=0（不产生 NaN）", ff["a158_corr_20"] == 0.0)
    check("平台 → resi=0（不产生 NaN）", abs(ff["a158_resi_20"]) < 1e-12)

    # 5) 纯净性：不含现有特征名（防止把 est_chg/macd/r20/dd 混进来）
    banned = {"est_chg", "est_sign", "macd", "r20", "dd", "score", "composite"}
    check("无现有特征混入", not (set(keys) & banned))

    # 6) PIT 对齐：样本日 d 用 navs[:i]（截至 T-1），不用 navs[i]
    navs = [("2020-01-01", 1.0), ("2020-01-02", 1.1), ("2020-01-03", 1.2)]
    rows = build_feature_rows([{"fund": "X", "date": "2020-01-03"}],
                              {"X": navs}, windows=(5,))
    # 末尾净值 1.2 未公布 → 只用 [1.0, 1.1] → 2 点 < 5+1 → NaN（若误用 navs[i] 也不足，
    # 故补一个长序列验证数值确为 navs[:i]）
    check("PIT：样本日当天净值不参与", math.isnan(rows[("X", "2020-01-03")]["a158_roc_5"]))
    long_navs = [(f"2020-01-{i:02d}", 1.0 + 0.01 * i) for i in range(1, 11)]
    rows2 = build_feature_rows([{"fund": "Y", "date": "2020-01-10"}], {"Y": long_navs},
                               windows=(5,))
    want = compute_features([v for _d, v in long_navs[:9]], windows=(5,))
    check("PIT：等于 navs[:i] 计算值",
          abs(rows2[("Y", "2020-01-10")]["a158_roc_5"] - want["a158_roc_5"]) < 1e-12)
    check("PIT：i<1 → 全 NaN", all(math.isnan(v) for v in build_feature_rows(
        [{"fund": "Y", "date": "2020-01-01"}], {"Y": long_navs}, windows=(5,))
        [("Y", "2020-01-01")].values()))
    check("PIT：fund 缺失 → 全 NaN", all(math.isnan(v) for v in build_feature_rows(
        [{"fund": "Z", "date": "2020-01-10"}], {}, windows=(5,))[("Z", "2020-01-10")].values()))

    print(f"[features_a158lite SELFTEST] {ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(_selftest())
