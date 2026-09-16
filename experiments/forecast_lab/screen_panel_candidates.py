"""D2a §三 · 候选代理入池筛查（闸门 + 相关性自动淘汰，2026-09-16；零网络）。

依据：output/forecast_lab_prereg_D2a_width_draft_20260916.md §三（判据先写死）。
回答唯一问题：**候选码是否满足入主池条件** —— 两条都过才进，任一不过即淘汰。

筛查判据（预注册 §三 写死，跑完不改）
1. **闸门核验**（§三.1）：首根 ≤ 2020-01-01（basket 原纪律）。本地无缓存者 = 需采购，
   本脚本只报告「不可判（待刷新）」，不臆测首根。
2. **相关性测试**（§三.2）：对齐日收益窗 **2022-01-01 ~ 数据末**，算候选 vs 现 14 主池
   每个成员的 **Spearman ρ**；与任一主池成员 **max|ρ| > 0.85 → 自动淘汰**
   （判定先于人审，防挑数）。
3. 附报（不进判据）：候选对主池的 ρ 分布、与最相关成员的代码、以及日收益 sd 比（供
   09-26 D-full 参考）。

纪律
- 零网络：只读 data/ 已缓存序列；主流程装 socket 守卫（实测拦截，与 T3/D2b 同口径）。
- 不写面板、不改任何判据点；产物只落 forecast_outputs/（47MB 级面板件不受本脚本影响）。
- 基金取单位净值（data/klines），代理取 close（data/stock_klines），板块取 data/sector_klines。

用法：
  .\\.venv\\Scripts\\python.exe -X utf8 experiments\\forecast_lab\\screen_panel_candidates.py
  .\\.venv\\Scripts\\python.exe -X utf8 experiments\\forecast_lab\\screen_panel_candidates.py --selftest
  # 明日刷新后重跑即可覆盖全部 10 码（缓存变了结果自然变，本脚本无状态）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / "experiments" / "forecast_lab"))

from kline_fingerprint import _json_series, _nav_series        # noqa: E402  复用存量装载

DATA = BASE / "data"
OUT_DIR = BASE / "forecast_outputs"

# ---- 现 14 主池（与 build_panel_dlite §一 逐项一致，不重复造清单口径） ----
FUNDS = ("002112", "002207", "022853", "025687")
GATE = ("512480", "512880", "159915", "512660", "510880",
        "512800", "160225", "512010", "501030")
SECTOR = ("BK0457",)
POOL14 = FUNDS + GATE + SECTOR

# ---- D2a §二 定稿 10 码（Summer 2026-09-16 14:44 拍板；名称仅为可读性，不参与判据） ----
CANDIDATES = {
    "510300": "沪深300ETF", "512100": "中证1000ETF", "510500": "中证500ETF",
    "513100": "纳指ETF", "518880": "黄金ETF", "159928": "消费ETF",
    "512690": "酒ETF", "512170": "医疗ETF", "515050": "5GETF", "510050": "上证50ETF",
}

GATE_FIRST_MAX = "2020-01-01"     # §三.1 basket 原闸门（写死）
RHO_MAX = 0.85                    # §三.2 自动淘汰阈值（写死）
RET_START = "2022-01-01"          # §三.2 对齐日收益窗起点（写死）
MIN_OVERLAP = 200                 # 重叠交易日下限；不足 → 记「样本不足」不判
_BLOCK_MSG = "筛查主流程禁止任何网络连接"
MARKET_HINT = {"51": "sh", "60": "sh", "50": "sh", "56": "sh",
               "15": "sz", "16": "sz", "18": "sz"}


def load_series(code: str) -> list[tuple[str, float]] | None:
    """读单个码的已缓存序列（date, value）升序。三路：stock_klines → sector_klines → klines(NAV)。"""
    p = DATA / "stock_klines" / f"{code}.json"
    if p.exists():
        s = _json_series(p, "klines")
        if s:
            return [(str(d), float(v)) for d, v in s]
    p = DATA / "sector_klines" / f"{code}.json"
    if p.exists():
        s = _json_series(p, "klines")
        if s:
            return [(str(d), float(v)) for d, v in s]
    p = DATA / "klines" / f"{code}.json"
    if p.exists():
        s = _nav_series(p)
        if s:
            return [(str(d), float(v)) for d, v in s]
    return None


def daily_returns(series: list[tuple[str, float]]) -> dict[str, float]:
    """{date: r_t}，r_t = v_t/v_{t-1} − 1（同 series 内相邻交易日，非日历日）。"""
    out: dict[str, float] = {}
    for (d0, v0), (d1, v1) in zip(series, series[1:]):
        if v0 > 0:
            out[str(d1)] = v1 / v0 - 1.0
    return out


def align_ret(ra: dict[str, float], rb: dict[str, float],
              start: str = RET_START) -> tuple[np.ndarray, np.ndarray]:
    da = {d for d in ra if d >= start}
    db = {d for d in rb if d >= start}
    common = sorted(da & db)
    return np.array([ra[d] for d in common]), np.array([rb[d] for d in common])


def screen(code: str, pool_rets: dict[str, dict[str, float]]) -> dict:
    """单候选的完整筛查。返回含 gate / rho / verdict 的字典（verdict ∈ PASS/FAIL/UNKNOWN）。"""
    name = CANDIDATES.get(code, code)
    series = load_series(code)
    if series is None:
        return {"code": code, "name": name, "verdict": "UNKNOWN",
                "reason": "本地无缓存 → 待明日 09:30 后受控采购，首根与相关性均不可判"}
    first, last = series[0][0], series[-1][0]
    gate_ok = first <= GATE_FIRST_MAX
    ret = daily_returns(series)

    # §三.1 闸门：不过即淘汰（应进 relaxed 分层），无需再算相关性
    if not gate_ok:
        return {"code": code, "name": name, "verdict": "FAIL",
                "reason": f"闸门不过：首根 {first} > {GATE_FIRST_MAX}（应进 relaxed 分层）",
                "first": first, "last": last}

    rhos: dict[str, float] = {}
    min_overlap = None
    for m, mret in pool_rets.items():
        x, y = align_ret(ret, mret)
        if min_overlap is None or len(x) < min_overlap:
            min_overlap = len(x)
        if len(x) < MIN_OVERLAP:
            continue
        r, _ = spearmanr(x, y)
        if r == r:
            rhos[m] = float(r)

    # 未达 min_overlap 的成员 = 无证据可判（池内 022853/025687 为短史基金，必然与任何
    # 长史候选重叠不足）。§三.2 判据方向是「有共线证据才淘汰」，故未评估者不计入淘汰，
    # 但必须在产物里如实披露 not_assessed（防止把「没测」当成「测过且没问题」）。
    not_assessed = sorted(m for m in pool_rets if m not in rhos)
    if not rhos:
        return {"code": code, "name": name, "verdict": "UNKNOWN",
                "reason": f"全部主池成员重叠 < {MIN_OVERLAP}，相关性不可判",
                "first": first, "last": last}
    worst_m = max(rhos, key=lambda k: abs(rhos[k]))
    worst = rhos[worst_m]
    detail = {"first": first, "last": last, "rho_max": round(worst, 4),
              "rho_against": worst_m, "rho_median": round(float(np.median(list(rhos.values()))), 4),
              "n_assessed": len(rhos), "not_assessed": not_assessed,
              "rhos": {k: round(v, 4) for k, v in rhos.items()}}
    if abs(worst) > RHO_MAX:
        return {"code": code, "name": name, "verdict": "FAIL",
                "reason": f"相关性淘汰：max|ρ|={abs(worst):.3f}（对 {worst_m}）> {RHO_MAX}",
                **detail}
    return {"code": code, "name": name, "verdict": "PASS",
            "reason": f"闸门过（首根 {first}）+ max|ρ|={abs(worst):.3f} ≤ {RHO_MAX}"
                      + (f"；未评估 {len(not_assessed)} 员（重叠<{MIN_OVERLAP}）" if not_assessed else ""),
            **detail}


def run() -> dict:
    pool_rets = {}
    missing_pool = []
    for m in POOL14:
        s = load_series(m)
        if s is None:
            missing_pool.append(m)
            continue
        pool_rets[m] = daily_returns(s)
    if missing_pool:
        raise RuntimeError(f"主池成员缺缓存（不应发生）：{missing_pool}")
    results = [screen(c, pool_rets) for c in CANDIDATES]
    return {"kind": "d2a_candidate_screen", "rho_max_gate": RHO_MAX,
            "gate_first_max": GATE_FIRST_MAX, "ret_start": RET_START,
            "min_overlap": MIN_OVERLAP, "pool14": list(POOL14),
            "zero_network": True, "results": results}


def _selftest() -> int:
    ok = fail = 0

    def chk(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  [FAIL] {name}")

    chk("1 主池 14（4+9+1）", (len(FUNDS), len(GATE), len(SECTOR)) == (4, 9, 1)
        and len(set(POOL14)) == 14)
    chk("2 候选 10 码且不与主池重叠", len(CANDIDATES) == 10
        and not (set(CANDIDATES) & set(POOL14)))
    chk("3 判据常量写死（0.85 / 2020-01-01 / 2022-01-01）",
        RHO_MAX == 0.85 and GATE_FIRST_MAX == "2020-01-01" and RET_START == "2022-01-01")
    chk("4 relaxed 三码不在主池（§七 不混池）",
        not ({"159611", "515220", "159825"} & set(POOL14)))

    # 5) 日收益与对齐逻辑（合成数据）
    s = [("2022-01-03", 10.0), ("2022-01-04", 11.0), ("2022-01-05", 9.9)]
    r = daily_returns(s)
    chk("5 日收益正确", abs(r["2022-01-04"] - 0.1) < 1e-12
        and abs(r["2022-01-05"] + 0.1) < 1e-12)
    x, y = align_ret({"2021-12-31": 1.0, "2022-01-04": 0.5, "2022-01-05": 0.2},
                     {"2022-01-04": 0.1, "2022-01-05": 0.2, "2022-01-06": 0.3})
    chk("6 对齐窗含起点、剔窗前后", len(x) == 2 and list(x) == [0.5, 0.2] and list(y) == [0.1, 0.2])

    # 7) 完美正相关 → 必被 0.85 淘汰；独立噪声 → 必过
    rng = np.random.default_rng(7)
    base = rng.normal(size=600)
    dates = [f"2023-{(i // 20) % 12 + 1:02d}-{i % 28 + 1:02d}" for i in range(600)]
    pool = {"m": {d: v for d, v in zip(dates, base)}}

    def synth(vals):
        return {d: float(v) for d, v in zip(dates, vals)}

    hi = {d: float(v) for d, v in zip(dates, base * 2 + 0.01)}
    noise = {d: float(v) for d, v in zip(dates, rng.normal(size=600))}
    # 直接测 ρ 判据本身（不走 load_series）
    xh, yh = align_ret(hi, pool["m"])
    xn, yn = align_ret(noise, pool["m"])
    rho_hi = spearmanr(xh, yh)[0]
    rho_n = spearmanr(xn, yn)[0]
    chk("7 完全共线 → max|ρ|≈1 → 判 FAIL", rho_hi > RHO_MAX)
    chk("8 独立噪声 → max|ρ|≈0 → 判 PASS", abs(rho_n) < RHO_MAX)

    # 9) 无缓存 → UNKNOWN（不臆测）
    r9 = screen("999999", pool_rets={"m": {"2022-01-04": 0.01}})
    chk("9 缺缓存记 UNKNOWN 而非猜测", r9["verdict"] == "UNKNOWN" and "待明日" in r9["reason"])

    # 10) 零网络守卫实测
    import socket as _socket

    class _NoNet(_socket.socket):
        def connect(self, *a, **k):
            raise RuntimeError(_BLOCK_MSG)

    orig = _socket.socket
    _socket.socket = _NoNet
    msg = None
    try:
        try:
            _socket.socket().connect(("127.0.0.1", 1))
        except RuntimeError as e:
            msg = str(e)
        except OSError as e:
            msg = f"OS:{type(e).__name__}"
        loaded = load_series("002112")
    finally:
        _socket.socket = orig
    chk("10 守卫拦截 connect", msg == _BLOCK_MSG)
    chk("11 守卫下装载路径可用（真零网络）", loaded is not None and len(loaded) > 100)

    # 12) 主池成员全部可读
    n_ok = sum(1 for m in POOL14 if load_series(m) is not None)
    chk("12 主池 14 全部可装载", n_ok == 14)

    print(f"[screen_panel_candidates SELFTEST] {ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()

    import socket
    orig = socket.socket

    class NoNet(socket.socket):
        def connect(self, *a, **k):
            raise RuntimeError(_BLOCK_MSG)

    socket.socket = NoNet
    try:
        out = run()
    finally:
        socket.socket = orig

    OUT_DIR.mkdir(exist_ok=True)
    stamp = out["kind"] + "_20260916"
    p = OUT_DIR / f"{stamp}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print("== D2a §三 候选入池筛查（零网络）==")
    print(f"  主池 {len(out['pool14'])} 成员 | 阈值 max|ρ| ≤ {RHO_MAX} | "
          f"闸门首根 ≤ {GATE_FIRST_MAX} | 收益窗自 {RET_START}")
    print()
    print(f"  {'码':7}{'名称':12}{'判定':8}{'首根':12}{'max|ρ|':>8}  对端 / 说明")
    order = {"PASS": 0, "UNKNOWN": 1, "FAIL": 2}
    for r in sorted(out["results"], key=lambda x: (order[x["verdict"]], x["code"])):
        rho = f"{abs(r['rho_max']):.3f}" if r.get("rho_max") is not None else "  -  "
        print(f"  {r['code']:7}{r['name']:12}{r['verdict']:8}"
              f"{r.get('first', '-'):12}{rho:>8}  "
              + (f"{r['rho_against']}  " if r.get("rho_against") else "") + r["reason"])
    npass = sum(1 for r in out["results"] if r["verdict"] == "PASS")
    nunk = sum(1 for r in out["results"] if r["verdict"] == "UNKNOWN")
    nfail = sum(1 for r in out["results"] if r["verdict"] == "FAIL")
    print(f"\n  小结：今日可判 PASS {npass} / FAIL {nfail} / 待采购 UNKNOWN {nunk}"
          f"（净增达标需 ≥8 → 明日刷新后复筛）")
    print(f"  产物：{p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
