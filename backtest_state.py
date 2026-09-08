#!/usr/bin/env python3
"""State Engine 转移矩阵生成（Phase 2 产出，2026-08-27）。

用法：
    python3 backtest_state.py                     # 全池，默认 OOS 起点 2025-04-29
    python3 backtest_state.py --min-n 20          # 调低状态样本门槛

产出（output/）：
    state_transition_table.json   机器可读全表（含 train/oos 分段统计）
    state_transition_table.txt    人类可读矩阵

口径：状态 = 基金净值合成日K上的缠论结构（trend|pivot_pos[|recent_event]），
逐日前缀重放保证因果；数字为条件频率统计，非模型预测（与 forecast_engine 的
正态假设 Q10/Q90 断开，q10/q90 改用经验分位数）。
"""
import argparse
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from core import data_loader
from core import state_engine as se


def _fmt(v) -> str:
    return "  -  " if v is None else f"{v:+.3f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--oos-start", default="2025-04-29",
                    help="OOS 段起点日期（与 backtest_forecast 一致）")
    ap.add_argument("--min-n", type=int, default=30,
                    help="状态最少样本数（train+oos 合计）")
    ap.add_argument("--flat-margin", type=float, default=0.003,
                    help="P(up) 判定阈值")
    ap.add_argument("--fresh-days", type=int, default=10,
                    help="枢外连续天数 ≤ 此值 → fresh（2026-08-27 敏感性测试用）")
    ap.add_argument("--tag", default="",
                    help="输出文件名后缀（多组对照用，如 f5/f15）")
    args = ap.parse_args()

    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    funds = cfg["fund_pool"]

    t0 = time.time()
    per_fund_states: dict[str, list[dict]] = {}
    per_fund_navs: dict[str, list[tuple[str, float]]] = {}
    for code in funds:
        fund = data_loader.load_fund(code)
        navs = fund["navs"]
        per_fund_navs[code] = navs
        print(f"[{code}] navs={len(navs)}，逐日前缀重放缠论…")
        per_fund_states[code] = se.states_for_fund(navs, fresh_days=args.fresh_days)
        print(f"    → states={len(per_fund_states[code])}")
    print(f"状态计算耗时 {time.time()-t0:.1f}s\n")

    table = se.build_transition_table(per_fund_states, per_fund_navs,
                                      oos_start=args.oos_start,
                                      min_n=args.min_n,
                                      flat_margin=args.flat_margin)

    # 稳定性评分（train vs OOS 回看）：输出增强表，并随 json 落盘供筛选状态集
    stability = se.summarize_stability(table)

    out_dir = BASE_DIR / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"state_transition_table_{args.tag}" if args.tag else "state_transition_table"
    (out_dir / f"{base}.json").write_text(
        json.dumps({"meta": {"oos_start": args.oos_start, "min_n": args.min_n,
                             "flat_margin": args.flat_margin,
                             "fresh_days": args.fresh_days,
                             "state_version": se.STATE_VERSION},
                    "stability": stability, "table": table},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    suff = {k: v for k, v in table.items() if v["sufficient"]}
    lines = [
        "状态转移表（PIT 条件频率统计，非模型预测）",
        f"OOS 起点 {args.oos_start} · min_n={args.min_n} · flat_margin={args.flat_margin}",
        f"样本足够状态 {len(suff)}/{len(table)}",
        "",
        "状态 | n(train/oos) | T+1 p_up | T+1 mean | T+3 p_up | T+3 mean | T+5 p_up | T+5 mean",
        "---|---:|---:|---:|---:|---:|---:|---:",
    ]
    for state in sorted(table, key=lambda s: -table[s]["n_total"]):
        e = table[state]
        cells = [state, f"{e['n_total']}({e['n_train']}/{e['n_oos']})"]
        for h in (1, 3, 5):
            seg = e.get(f"h{h}", {})
            stats = seg.get("oos") or seg.get("train")
            cells.append(_fmt(stats["p_up"] if stats else None))
            cells.append(_fmt(stats["mean"] if stats else None))
        lines.append(" | ".join(cells))
    lines += ["", "状态稳定性（train vs OOS 回看：OOS n>=20 且 |Δp_up|<=0.15 且方向一致 → 稳定）",
              "状态 | h1 | h3 | h5 |",
              "---|---:|---:|---:|"]
    for state in sorted(table, key=lambda s: -table[s]["n_total"]):
        st = stability.get(state, {})
        flags = []
        for h in (1, 3, 5):
            s = st.get(h, {})
            if s.get("p_up_train") is None:
                flags.append("—")
            elif s.get("stable"):
                flags.append(f"✓ d={s['diff']:.2f}")
            else:
                flags.append(f"✗ {s.get('reason','')[:26]}")
        lines.append(" | ".join([state] + flags))
    # 修复空真 bug（2026-08-27）：样本不足的状态所有周期 p_up_train=None，会被
    # all(空迭代器)=True 误判为“稳定”而虚增计数。要求至少一个周期有真实评估，
    # 且所有有评估的周期都稳定，才计入。
    stable_states = [s for s in stability
                     if any(v.get("p_up_train") is not None for v in stability[s].values())
                     and all(v.get("stable") for v in stability[s].values()
                             if v.get("p_up_train") is not None)]
    lines += ["", f"全部周期均稳定状态数: {len(stable_states)}",
              "名单: " + (", ".join(stable_states) if stable_states else "(无)"),
              "（这些状态可作为后续 Forecast 接入的候选状态集）"]
    txt = "\n".join(lines) + "\n"
    (out_dir / f"{base}.txt").write_text(txt, encoding="utf-8")

    print(txt)
    print(f"[ok] 已落盘 output/{base}.json / .txt")
    print(f"运行时长 {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())