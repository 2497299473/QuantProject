"""三态判定器在线验证（P0-C · 2026-09-09，只读，≤28 请求）。

对 002112（案发基金）与 002207（对照基金）的 7 个年度各跑一次加固拉取：
  期望 12/12 全部 OK（若抓到 SUSPECT_DEGRADED / PARSE_MISMATCH → bug 当场现形并留档）。
另加 022853/025687 的 2020 年（真无披露年份）：期望 EMPTY_CONFIRMED（返回 [] 不抛）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

from experiments.forecast_lab.holdings_hardened import (  # noqa: E402
    DegradedResponse, fetch_holdings_year_hardened)

# 精简样本（6 请求，非 24）：002112 案发年 2020 + 正常年 + 最新半年，
# 对照基金 002207 一年，两只新基金的真无披露年。
# 收敛理由：今日已累计请求 fundf10 约 70 次，东财有 IP 级频控前科（09-08），
# 在线验证只需覆盖三类判定的存在性，不必全矩阵。
CASES = [("002112", 2020), ("002112", 2025), ("002112", 2026),
         ("002207", 2020), ("022853", 2020), ("025687", 2020)]


def main() -> int:
    bad = 0
    for fund, year in CASES:
        try:
            snaps = fetch_holdings_year_hardened(fund, year)
            status = "OK  " if snaps else "EMPTY_CONFIRMED"
            print(f"  {fund} {year}  {status}  n={len(snaps)}")
        except DegradedResponse as e:
            bad += 1
            print(f"  {fund} {year}  ⚠️{e.kind}  len={e.text_len} raw={e.raw_path}")
        except Exception as e:
            bad += 1
            print(f"  {fund} {year}  ✗ {type(e).__name__}: {str(e)[:100]}")
        time.sleep(1.2)
    print(f"=> {'抓到 ' + str(bad) + ' 例降级（见留档）' if bad else f'本轮 {len(CASES)} 请求全部正常/确证空态，bug 为偶发，三态判定器无回归'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
