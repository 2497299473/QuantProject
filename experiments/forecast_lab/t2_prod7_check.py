r"""T2 的 7 个生产特征列 · 联网补跑核对（2026-09-16，Summer 拍板「A：补跑」）。

背景：build_panel_dlite 已让 T2 的 fwd* 标签列逐值一致（3371 身份行 / 20226 格 /
max|Δ|=0.0），但 §六 还要求 7 个生产特征列。这 7 列来自 backtest_spread.load_samples()
的 lookthrough 重放，其输入（228 只过期股票 K 线 + 基金持仓 fundf10）不在 §五
「17 序列」刷新预算内，故本脚本单独立项。

两条自证（本脚本存在的理由，freeze_samples.py 不具备）：
1. R3 硬停线：monkeypatch netutil.http_get，每次请求前查表，越过 DEADLINE 立即抛错停手，
   当日不重试（继承 §五 之 4）。
2. 面板输入未被扰动：跑前/跑后各取一次含板块的 K 线指纹，逐 17 成员比对 (date, close)
   序列 sha。本脚本只读缓存、只重取股票侧个股，若任一面板成员被改写 → PANEL_DISTURBED，
   当日面板冻结与指纹的对应关系即被破坏，须如实报告。

诚实边界：这 7 列**面板并不使用**（§二 特征空间 = a158-50；§九/R1 明示生产留出端同样取
a158 特征）。本核对只为「与 0910 冻结横向可比」，不改变 §六 T1~T3 任何阈值。
若比对 FAIL，须区分原因：生产链路自身的前复权改写（与 D 无关）vs D 面板实现错误。

用法：
  .\.venv\Scripts\python.exe -X utf8 experiments/forecast_lab/t2_prod7_check.py
退出码：0 通过 / 1 停手或异常（含 PANEL_DISTURBED）
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "experiments" / "forecast_lab"))

from core import netutil                        # noqa: E402
from kline_fingerprint import build_fingerprint  # noqa: E402
from build_panel_dlite import (FUNDS, GATE_PROXIES, RELAXED_PROXIES,   # noqa: E402
                               SECTOR_CODES)

DEADLINE = "2026-09-16T11:15:00"                # 留 5 分钟余量给 R3 的 11:20
OUT_DIR = BASE_DIR / "forecast_outputs"
REPORT = BASE_DIR / "output" / "ops_runs" / "2026-09-16-t2-prod7.md"
FROZEN_0910 = OUT_DIR / "samples_frozen_20260910.jsonl"

PROD7 = ("est_chg", "est_sign", "composite", "score",
         "breadth", "concentration", "covered_pct")
RATE_MARKERS = ("DegradedResponse", "SUSPECT_DEGRADED", "PARSE_MISMATCH",
                "持仓拉取失败", "年持仓拉取失败")

PANEL_KEYS = ([("fund", c) for c in FUNDS]
              + [("gate", c) for c in GATE_PROXIES]
              + [("relaxed", c) for c in RELAXED_PROXIES]
              + [("sector", c) for c in SECTOR_CODES])


class RateGuard(Exception):
    """越过 R3 硬停线。"""


REQUESTS = {"n": 0}
_orig_http_get = netutil.http_get


def _guarded_http_get(url, **kw):
    if datetime.now().isoformat(timespec="seconds") >= DEADLINE:
        raise RateGuard(f"已到 R3 硬停线 {DEADLINE}，停止发出新请求")
    REQUESTS["n"] += 1
    return _orig_http_get(url, **kw)


def panel_shas() -> dict[str, str]:
    fp = build_fingerprint(include_sector=True)
    out: dict[str, str] = {}
    for kind, code in PANEL_KEYS:
        pool = {"fund": fp["fund_sha"], "sector": fp["sector_sha"]}.get(kind, fp["stock_sha"])
        out[code] = pool.get(code, "<absent>")
    return out


def row_index(rows: list[dict]) -> dict[tuple[str, str], dict]:
    return {(r["fund"], r["date"]): r for r in rows}


def compare(a: dict, b: dict) -> dict:
    """按共有身份逐格比对 PROD7；返回统计与不一致样本。"""
    shared = sorted(set(a) & set(b))
    cells = mism = 0
    bad: list[str] = []
    max_abs = 0.0
    for k in shared:
        ra, rb = a[k], b[k]
        for col in PROD7:
            va, vb = ra.get(col), rb.get(col)
            if va is None or vb is None:
                if (va is None) != (vb is None):
                    mism += 1
                    if len(bad) < 12:
                        bad.append(f"{k[0]} {k[1]} {col}: None vs {vb!r}")
                continue
            fa, fb = float(va), float(vb)
            if math.isnan(fa) and math.isnan(fb):
                continue
            cells += 1
            d = abs(fa - fb)
            max_abs = max(max_abs, d)
            if d > 1e-9:
                mism += 1
                if len(bad) < 12:
                    bad.append(f"{k[0]} {k[1]} {col}: {fa!r} vs {fb!r} Δ={d:.3g}")
    # only_frozen / only_replay：a=0910 冻结、b=本次重算（早先版本标签映射写反，已正）
    return {"n_shared": len(shared), "cells": cells, "mismatch": mism,
            "max_abs_diff": max_abs, "only_frozen": len(set(a) - set(b)),
            "only_replay": len(set(b) - set(a)), "examples": bad}


def write_report(text: str, code: int) -> int:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(text, encoding="utf-8")
    print(text)
    return code


def main() -> int:
    if not FROZEN_0910.exists():
        print(f"[ABORT] 缺 0910 冻结：{FROZEN_0910}")
        return 1
    frozen = [json.loads(l) for l in
              FROZEN_0910.read_text(encoding="utf-8").splitlines() if l.strip()]

    before = panel_shas()
    t0 = time.time()
    netutil.http_get = _guarded_http_get          # 只在本进程内生效，不改仓库文件
    stopped: str | None = None
    samples: list[dict] = []
    flags: list[str] = []
    try:
        from backtest_spread import load_samples
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            samples = load_samples()
        flags = [str(x.message) for x in caught if any(k in str(x.message) for k in RATE_MARKERS)]
    except RateGuard as e:
        stopped = f"RATE_GUARD_TRIP: {e}"
    except Exception as e:                          # noqa: BLE001
        stopped = f"ERROR: {type(e).__name__}: {e}"
    finally:
        netutil.http_get = _orig_http_get
        after = panel_shas()
    elapsed = round(time.time() - t0, 1)

    disturbed = sorted(c for c in before if before[c] != after[c])
    panel_ok = not disturbed

    lines = [
        "# T2 生产特征 7 列 · 联网补跑核对（2026-09-16）",
        "",
        f"- 时刻：{datetime.now().isoformat(timespec='seconds')}（用时 {elapsed}s）",
        f"- R3 硬停线：{DEADLINE}（请求前逐个查表，越过即停手，当日不重试）",
        f"- 实际发出 HTTP 请求：{REQUESTS['n']}",
        f"- load_samples 产出：**{len(samples)}** 行"
        + (f"（⚠️ 中断：{stopped}）" if stopped else "（完整跑完）"),
        f"- 降级/频控征兆：{len(flags)}" + (" → 停手" if flags else ""),
        "",
        "## 面板输入未被扰动（自证）",
        "",
        f"- 结论：**{'PANEL_UNDISTURBED' if panel_ok else 'PANEL_DISTURBED'}**"
        f"（17 成员 (date,close) 序列 sha 跑前/跑后逐个比对）",
    ]
    if disturbed:
        lines.append(f"- ⚠️ 被改写成员：{', '.join(disturbed)}"
                     " → 当日面板冻结与指纹的对应关系已破坏，须重生成并重出指纹")
    else:
        lines.append("- 面板 17 个成员序列全部逐字节未变；"
                     "本脚本重取的是基金持仓**个股**，与面板成员无交集。")
    if flags:
        lines += ["", "### 征兆明细", ""] + [f"- `{m[:300]}`" for m in flags]

    if len(samples) < len(frozen) // 2 or stopped:
        lines += ["", "## 判定：未完成 —— 不做比对",
                  "",
                  f"样本行数 {len(samples)} 与 0910 冻结 {len(frozen)} 不可比"
                  f"（{'中途停手' if stopped else '产出行数过少'}），"
                  "按 §五 之 4 当日不重试，缺口如实顺延。"]
        return write_report("\n".join(lines) + "\n", 1)

    res = compare(row_index(frozen), row_index(samples))
    ok = res["mismatch"] == 0 and res["n_shared"] == len(frozen)
    lines += [
        "",
        "## 7 列逐值比对（0910 冻结 vs 本次重算）",
        "",
        f"- 共有身份：**{res['n_shared']}** / 冻结 {len(frozen)} 行"
        f"（仅冻结侧 {res['only_frozen']}、仅本次重算侧 {res['only_replay']}）",
        f"- 比对格数：**{res['cells']}**",
        f"- 不一致：**{res['mismatch']}**",
        f"- max|Δ| = **{res['max_abs_diff']:.3g}**（容差 1e-9）",
        f"- 判定：**{'PASS' if ok else 'FAIL'}**",
    ]
    if res["examples"]:
        lines += ["", "### 不一致样本（前 12 条）", ""] + [f"- `{e}`" for e in res["examples"]]
    if not ok:
        lines += ["", "> 若 FAIL 源于生产链路自身的前复权改写（09-10 曾一次重取 171/240 只），",
                  "> 则属既有取数行为，**与候选 D 面板实现无关**；面板不使用这 7 列。"]

    tag = "pass" if ok else "fail"
    out = OUT_DIR / f"samples_t2replay_20260916.jsonl"
    out.write_text("".join(json.dumps(s, ensure_ascii=False, sort_keys=True) + "\n"
                           for s in samples), encoding="utf-8")
    lines += ["", f"- 重算样本留档：`{out.name}`"
              f"（sha256 {hashlib.sha256(out.read_bytes()).hexdigest()[:16]}…，"
              f"仅供 T2 核对，非 §五 冻结产物，{tag}）",
              "",
              "> 本文件只记录数据层核对；不含任何信号/持仓/建议措辞，不构成投资建议。"]
    return write_report("\n".join(lines) + "\n", 0 if ok else 1)


if __name__ == "__main__":
    sys.exit(main())
