"""持仓拉取加固版（P0-C 诊断产物 · 2026-09-09）。

## 要修的 bug（本次探针实证）
同一天两次只读冻结（间隔 2 分钟）中，**002112 / 2020 年** 从 4 期静默变成 0 期，
且 `failed_year_warnings` 为空——即 `fetch_holdings_year` 返回 `[]` 但**没有抛异常**：

    snapshot_frozen_20260909_102726.json : 002112 n=26  {2020:4,2021:4,...}
    snapshot_frozen_20260909_102926.json : 002112 n=22  {2021:4,2022:4,...}   ← 2020 消失
    直连重探 9 次（002112/2020）         : 全部 boxitem=4 parsed=4，HTTP 200

根因链（`core/lookthrough.py`）：
    ① `fetch_holdings_year` 只在 HTTP 层抛异常时才算失败；
    ② 若响应 200 但页面是**偶发降级/反爬形态**（无 `<div class='boxitem'`，或结构变体），
       循环解析出 0 期 → `return []`；
    ③ `holdings_history` 用 `got = []` 视为成功、**不计入 failed_years、不告警**；
    ④ 下游样本少一整年 → 特征漂移 → RankIC 漂移 ±0.03~0.05（MEMORY 08-29 记录的正是此现象）。

## 本模块的修法（研究层先验证，通过后再提回 core/lookthrough.py）
把"HTTP 成功但零快照"从**静默正常**改成**显式三态**，只有确证为"该年真无披露"才返回空：

    EMPTY_CONFIRMED   响应含正常空态标记            → 返回 []（真无数据，新基金常见）
    SUSPECT_DEGRADED  无 boxitem 且无正常空态标记    → 抛 DegradedResponse（触发重试+告警）
    PARSE_MISMATCH    有 boxitem 但解析出 0 期       → 抛 DegradedResponse（结构变更/降级）

两种异常都带**原始响应留档**（forecast_outputs/f10_raw/），这样下次漂移可直接看到证据，
而不是只能从两次计数矩阵反推——08-29 那次就是因为没有留档，根因查不下去。

## 纪律
- 本模块**不改生产代码**、不写 data/；raw 只落在 forecast_outputs/（已 gitignore）。
- 供探针与后续 A/B 使用；生产接线需 Summer 拍板（见报告 §七）。
- 默认 `reuse` 逻辑不缓存网络结果，每次真请求，避免"修好了但被缓存掩盖"。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

from core import netutil   # noqa: E402

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
RAW_DIR = BASE_DIR / "forecast_outputs" / "f10_raw"

# 东财 F10 的正常空态标记（该年确无持仓披露时会出现的字样）。
# 宁可判窄：命中才认定"真无数据"，未命中一律按可疑降级处理并留档，
# 漏判的代价是多一次重试，误判的代价是又一轮静默缺年。
_EMPTY_MARKERS = ("暂无数据", "没有相关数据", 'content:""', "no data")


class DegradedResponse(RuntimeError):
    """HTTP 成功但响应形态可疑（降级/反爬/结构变更），不得当作"该年无持仓"。"""

    def __init__(self, msg: str, *, kind: str, raw_path: str | None, text_len: int):
        super().__init__(msg)
        self.kind = kind
        self.raw_path = raw_path
        self.text_len = text_len


def _dump_raw(fund: str, year: int, text: str) -> str | None:
    try:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        p = RAW_DIR / f"{fund}_{year}_{datetime.now().strftime('%H%M%S')}.html"
        p.write_text(text, encoding="utf-8", errors="replace")
        return str(p)
    except Exception:
        return None


def fetch_holdings_year_hardened(fund_code: str, year: int, *,
                                 top_n: int = 10, retries: int = 2) -> list[dict]:
    """带三态判定的持仓拉取。retries 只针对 DegradedResponse（HTTP 异常交给 netutil 自身重试）。"""
    url = (f"https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
           f"?type=jjcc&code={fund_code}&topline=10&year={year}")
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return _fetch_once(url, fund_code, year, top_n)
        except DegradedResponse as e:
            last = e
            if attempt < retries:
                import time
                time.sleep(2 * (attempt + 1))
    assert last is not None
    raise last


def _fetch_once(url: str, fund_code: str, year: int, top_n: int) -> list[dict]:
    text = netutil.http_get(url, headers={"User-Agent": UA,
                                         "Referer": "https://fundf10.eastmoney.com/"})
    snaps = _parse(text, top_n)
    n_box = len(re.findall(r"<div class='boxitem", text))

    if not snaps:
        confirmed_empty = any(m in text for m in _EMPTY_MARKERS)
        if n_box == 0 and confirmed_empty:
            return []                        # EMPTY_CONFIRMED：真无披露
        kind = "PARSE_MISMATCH" if n_box else "SUSPECT_DEGRADED"
        path = _dump_raw(fund_code, year, text)
        cfg = json.dumps({"n_boxitem": n_box, "markers_hit": [
            m for m in _EMPTY_MARKERS if m in text]}, ensure_ascii=False)
        raise DegradedResponse(
            f"F10 {fund_code}/{year} 返回 0 期但非确证空态（{cfg}，len={len(text)}，"
            f"留档 {path}）——按降级处理，勿当缺年",
            kind=kind, raw_path=path, text_len=len(text))
    return snaps


def _parse(text: str, top_n: int) -> list[dict]:
    """与 core/lookthrough.fetch_holdings_year 同构的解析（不 import 生产函数，避免行为耦合）。"""
    out = []
    for block in text.split("<div class='boxitem")[1:]:
        dm = re.search(r"截止至：<font class='px12'>(\d{4}-\d{2}-\d{2})", block)
        if not dm:
            continue
        holdings = []
        for row in block.split("<tr>")[1:]:
            cm = re.search(r"unify/r/([01])\.(\d{6})", row)
            nm = re.search(r"class='tol'><a[^>]*>([^<]+)</a>", row)
            pm = re.search(r"class='tor'>([\d.]+)%", row)
            if cm and nm and pm:
                holdings.append({"market": cm.group(1), "code": cm.group(2),
                                 "name": nm.group(1), "pct": float(pm.group(1))})
        if holdings:
            out.append({"date": dm.group(1), "holdings": holdings[:top_n]})
    out.sort(key=lambda s: s["date"])
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

    row = ("<tr><td class='tol'><a href='unify/r/1.600000'>浦发银行</a></td>"
           "<td class='tor'>9.00%</td></tr>")
    good = ("<div class='boxitem w790'><h4>截止至：<font class='px12'>2020-12-31</font>"
            f"</h4><table>{row}</table>")
    check("正常响应解析出 1 期", len(_parse(good, 10)) == 1)
    try:
        _fetch_parse_check(good)
        check("正常响应不抛", True)
    except Exception:
        check("正常响应不抛", False)
    # 降级页：200 但无 boxitem、无空态标记 → 必须抛
    try:
        _judge('<div class="antibot">verify</div>')
        check("降级页应抛异常", False)
    except DegradedResponse as e:
        check("降级页应抛异常", e.kind == "SUSPECT_DEGRADED")
    # 确证空态：无 boxitem 但有"暂无数据" → 正常返回 []
    check("确证空态返回 []", _judge_ok("暂无数据"))
    # 结构变更：有 boxitem 但解析 0 期 → PARSE_MISMATCH
    try:
        _judge("<div class='boxitem w790'><h4>改了版式</h4></div>")
        check("结构失配应抛异常", False)
    except DegradedResponse as e:
        check("结构失配应抛异常", e.kind == "PARSE_MISMATCH")
    print(f"[holdings_hardened SELFTEST] {ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


def _judge(text: str):
    snaps = _parse(text, 10)
    n_box = len(re.findall(r"<div class='boxitem", text))
    if not snaps:
        if n_box == 0 and any(m in text for m in _EMPTY_MARKERS):
            return []
        raise DegradedResponse("x", kind="PARSE_MISMATCH" if n_box else "SUSPECT_DEGRADED",
                               raw_path=None, text_len=len(text))
    return snaps


def _judge_ok(text: str) -> bool:
    try:
        return _judge(text) == []
    except DegradedResponse:
        return False


def _fetch_parse_check(text: str):
    return _judge(text)


if __name__ == "__main__":
    sys.exit(_selftest())
