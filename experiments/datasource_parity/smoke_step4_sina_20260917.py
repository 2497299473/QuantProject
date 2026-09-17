"""步 4 真实冒烟（2026-09-17，零东财请求）：SinaFundNavProvider vs 今日 16:01 实验脚本落盘结果。

新浪域不受铁律 7（东财频控）约束，口径同步 3 腾讯冒烟。只拉 002112 一只（最长序列
2637 条、约 27 页分页，压力最大），与 pull_sina_nav.py 今日产物逐条比 (date, nav)。
另用其结果对照东财缓存 data/klines/002112.json 的 navs（纯读盘，不发请求），
证明「若生产链回落到 sina，口径与既有校验一致」。
"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from core.datasource.providers.fund_sina import SinaFundNavProvider  # noqa: E402

CODE = "002112"
TODAY_CACHE = BASE / "output" / "sina_nav" / f"{CODE}.json"
EM_CACHE = BASE / "data" / "klines" / f"{CODE}.json"


def main() -> int:
    if not TODAY_CACHE.exists():
        print(f"FAIL: 缺今日新浪产物 {TODAY_CACHE}（16:00 后置任务未跑？）")
        return 1
    ref = json.loads(TODAY_CACHE.read_text(encoding="utf-8"))
    ref_rows = [[d, v] for d, v in ref["navs"]]

    res = SinaFundNavProvider().fetch(code=CODE)
    if not res.ok:
        print(f"FAIL: provider 返回失败 {res.error}（不重试，直接停手）")
        return 1
    got = [[d, v] for d, v in res.payload["navs"]]

    print(f"  sina 条数: provider={len(got)} vs 今日脚本={len(ref_rows)} "
          f"(latency {res.latency_ms/1000:.1f}s)")
    if got != ref_rows:
        diff = [(i, a, b) for i, (a, b) in enumerate(zip(got, ref_rows)) if a != b]
        print(f"FAIL: 与今日脚本逐条不一致 {len(diff)} 处，前 5 处：{diff[:5]}")
        return 1
    print("  ✓ 与今日 pull_sina_nav.py 产物逐条恒等（date+nav 全等）")

    em = json.loads(EM_CACHE.read_text(encoding="utf-8"))
    em_map = {d: v for d, v in em["navs"]}
    common = [(d, v) for d, v in got if d in em_map]
    mism = [(d, v, em_map[d]) for d, v in common if abs(v - em_map[d]) > 1e-6]
    print(f"  sina×东财缓存交集 {len(common)} 条，mismatch={len(mism)}"
          f"（对照：今日脚本报告 mismatch_n={ref['check'].get('mismatch_n')}）")
    if mism:
        print(f"FAIL: 交叉校验出现差异，前 5 处：{mism[:5]}")
        return 1
    print("\n== 结论 == PASS：sina provider 真实通路 + 逐条恒等 + 交叉校验零差异")
    return 0


if __name__ == "__main__":
    sys.exit(main())
