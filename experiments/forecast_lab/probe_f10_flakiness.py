"""定位 002112/2020 静默缺年根因（2026-09-09，只读，≤6 请求）。

假设 H1：eastmoney F10 对同一 year 参数返回**偶发空/降级页**（HTTP 200 但无 boxitem），
        → fetch_holdings_year 返回 []，而 holdings_history 只在**抛异常**时记 failed_years，
          空列表被视为"该年无披露"→ 静默缺年。这正是 v7 ±0.03~0.05 漂移的机制。
假设 H2：真限速，返回封禁页。

区分法：同年连续多请求，若**时有时无**且返回体含内容差异 → H1；若稳定空/稳定含封禁字样 → H2。
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

from core import lookthrough, netutil   # noqa: E402

UA = lookthrough.UA


def raw_year(fund: str, year: int) -> dict:
    url = (f"https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
           f"?type=jjcc&code={fund}&topline=10&year={year}")
    t = netutil.http_get(url, headers={"User-Agent": UA,
                                      "Referer": "https://fundf10.eastmoney.com/"})
    n_box = len(re.findall(r"<div class='boxitem", t))
    parsed = lookthrough.fetch_holdings_year(fund, year)
    return {"len": len(t), "boxitem": n_box, "parsed": len(parsed),
            "head": t[:160].replace("\n", " ")}


def main() -> None:
    for fund, year in (("002112", 2020), ("002207", 2020)):
        print(f"== {fund} year={year} × 3 ==")
        for i in range(3):
            try:
                r = raw_year(fund, year)
                print(f"  #{i} len={r['len']:7d} boxitem={r['boxitem']} "
                      f"parsed={r['parsed']} head={r['head'][:90]!r}")
            except Exception as e:
                print(f"  #{i} EXC {type(e).__name__}: {str(e)[:120]}")
            time.sleep(3)
    print("\n判读：boxitem>0 但 parsed=0 → 解析正则失配；boxitem=0 且 len 正常 → 服务端返空；"
          "三次忽 0 忽非 0 → 偶发降级（静默缺年根因）。")


if __name__ == "__main__":
    main()
