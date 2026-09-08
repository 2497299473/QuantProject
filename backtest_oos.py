#!/usr/bin/env python3
"""Walk-forward out-of-sample 测试：加权权重能否外样本成立。

核心问题（2026-08-25 用户提出）：用 2026 年之前的数据训练加权权重，
看 2026 外样本是否还成立 —— 真正的 out-of-sample 检验。

方法：5 个滚动窗口，每个窗口用前段 OLS 拟合三信号权重，后段外样本验证。
- 信号：est_sign（实时估算方向）、composite（缠论）、score_sign（三因子方向）
- 拟合：OLS 回归 fwd10 ~ est + comp + score，得到系数 β
- 外样本：用 β 算 predicted，分桶（top 40% 加仓 / bottom 40% 减仓），看方向差
- 对照：全样本拟合 vs 外样本 —— 若外样本 << 全样本 = 过拟合

判定标准（事先写死）：
- I. ≥3/5 窗口外样本方向差 > 0
- J. 外样本方向差均值 > 0.3%（有经济意义的最小阈值）
- K. 外样本方向差 / 全样本方向差 > 0.5（过拟合不严重）
I+J+K 全过 → 加权组合可作为弱参考；否则不可上线。

用法：python3 backtest_oos.py
"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

# 直接复用 backtest_spread 的样本构建（含缓存，不重复拉数据）
from backtest_spread import load_samples as build_samples


def ols_fit(train: list[dict]) -> dict:
    """OLS: fwd10 ~ est_sign + composite + score_sign。返回 {b0, b1, b2, b3, r2}。

    手写最小二乘（3 自变量 + 截距），无外部依赖。
    """
    n = len(train)
    # 设计矩阵 [1, est, comp, score]
    X = [[1.0,
          float(s["est_sign"]),
          float(s["composite"]),
          (1.0 if s["score"] > 0 else (-1.0 if s["score"] < 0 else 0.0))]
         for s in train]
    y = [s["fwd10"] for s in train]
    p = 4  # 参数数

    # XtX
    XtX = [[0.0] * p for _ in range(p)]
    for i in range(p):
        for j in range(p):
            XtX[i][j] = sum(X[k][i] * X[k][j] for k in range(n))
    # Xty
    Xty = [sum(X[k][i] * y[k] for k in range(n)) for i in range(p)]

    # 高斯消元解 XtX * beta = Xty
    aug = [XtX[i][:] + [Xty[i]] for i in range(p)]
    for col in range(p):
        # 选主元
        piv = max(range(col, p), key=lambda r: abs(aug[r][col]))
        aug[col], aug[piv] = aug[piv], aug[col]
        if abs(aug[col][col]) < 1e-12:
            return {"b0": 0, "b1": 0, "b2": 0, "b3": 0, "r2": 0}
        for r in range(col + 1, p):
            f = aug[r][col] / aug[col][col]
            for c in range(col, p + 1):
                aug[r][c] -= f * aug[col][c]
    beta = [0.0] * p
    for i in range(p - 1, -1, -1):
        beta[i] = aug[i][p]
        for j in range(i + 1, p):
            beta[i] -= aug[i][j] * beta[j]
        beta[i] /= aug[i][i]

    # R²
    ymean = sum(y) / n
    ss_tot = sum((v - ymean) ** 2 for v in y)
    ss_res = sum((y[k] - sum(beta[j] * X[k][j] for j in range(p))) ** 2 for k in range(n))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

    return {"b0": beta[0], "b1": beta[1], "b2": beta[2], "b3": beta[3], "r2": r2}


def predict(beta: dict, s: dict) -> float:
    return (beta["b0"] + beta["b1"] * s["est_sign"]
            + beta["b2"] * s["composite"]
            + beta["b3"] * (1.0 if s["score"] > 0 else (-1.0 if s["score"] < 0 else 0.0)))


def spread(preds: list[tuple[float, float, str]]) -> dict:
    """preds: [(predicted, fwd10, fund)] → 方向差（top40% 均值 - bottom40% 均值）。"""
    if len(preds) < 10:
        return {"spread": None, "n_add": 0, "n_cut": 0}
    preds.sort(key=lambda x: x[0])
    n = len(preds)
    k = max(1, int(n * 0.4))
    bottom = preds[:k]
    top = preds[n - k:]
    mean_top = sum(p[1] for p in top) / len(top)
    mean_bot = sum(p[1] for p in bottom) / len(bottom)
    return {"spread": mean_top - mean_bot, "n_add": len(top), "n_cut": len(bottom),
            "mean_top": mean_top, "mean_bot": mean_bot}


def ic(signs: list[float], fwds: list[float]) -> float:
    n = len(signs)
    if n < 3:
        return 0.0
    mx, my = sum(signs) / n, sum(fwds) / n
    sx = sum((x - mx) ** 2 for x in signs) ** 0.5
    sy = sum((y - my) ** 2 for y in fwds) ** 0.5
    return sum((x - mx) * (y - my) for x, y in zip(signs, fwds)) / (sx * sy) if sx * sy else 0.0


def main() -> int:
    samples = build_samples()
    if not samples:
        print("无样本"); return 1

    # 去掉 025687（43 天样本太少，无法分窗）
    samples = [s for s in samples if s["fund"] != "025687"]
    samples.sort(key=lambda s: s["date"])
    dates = [s["date"] for s in samples]
    print(f"\n样本区间: {dates[0]} ~ {dates[-1]}，{len(samples)} 条（已剔除 025687）")

    # 5 个 walk-forward 窗口
    cutoffs = ["2023-01-01", "2024-01-01", "2025-01-01", "2026-01-01"]
    windows = [
        ("2020~2022 → 2023", "2020-01-01", "2023-01-01", "2024-01-01"),
        ("2020~2023 → 2024", "2020-01-01", "2024-01-01", "2025-01-01"),
        ("2020~2024 → 2025", "2020-01-01", "2025-01-01", "2026-01-01"),
        ("2020~2025 → 2026", "2020-01-01", "2026-01-01", "2027-01-01"),
        ("2023~2025 → 2026（仅后半段训练）", "2023-01-01", "2026-01-01", "2027-01-01"),
    ]

    print("\n== Walk-forward OOS ==")
    results = []
    for name, train_start, test_start, test_end in windows:
        train = [s for s in samples if train_start <= s["date"] < test_start]
        test = [s for s in samples if test_start <= s["date"] < test_end]
        if len(train) < 50 or len(test) < 20:
            print(f"  {name}: 样本不足 train={len(train)} test={len(test)}，跳过")
            continue

        beta = ols_fit(train)
        # 全样本拟合（对照）
        beta_full = ols_fit(train + test)
        sp_full = spread([(predict(beta_full, s), s["fwd10"], s["fund"]) for s in test])

        # 外样本
        preds = [(predict(beta, s), s["fwd10"], s["fund"]) for s in test]
        sp = spread(preds)

        # 各信号单独 IC（train）
        est_ic = ic([float(s["est_sign"]) for s in train], [s["fwd10"] for s in train])
        comp_ic = ic([float(s["composite"]) for s in train], [s["fwd10"] for s in train])
        score_ic = ic([(1.0 if s["score"] > 0 else (-1.0 if s["score"] < 0 else 0.0))
                       for s in train], [s["fwd10"] for s in train])

        ratio = (sp["spread"] / sp_full["spread"]) if sp_full["spread"] and abs(sp_full["spread"]) > 1e-8 else None
        results.append({"name": name, "spread": sp["spread"], "spread_full": sp_full["spread"],
                        "ratio": ratio, "n_test": len(test),
                        "beta": beta, "est_ic": est_ic, "comp_ic": comp_ic, "score_ic": score_ic})

        print(f"\n  【{name}】")
        print(f"    train={len(train)} test={len(test)}")
        print(f"    OLS β: b0={beta['b0']:+.5f} est={beta['b1']:+.4f} comp={beta['b2']:+.4f} score={beta['b3']:+.4f} R²={beta['r2']:.4f}")
        print(f"    train IC: est={est_ic:+.3f} comp={comp_ic:+.3f} score={score_ic:+.3f}")
        print(f"    外样本方向差: {sp['spread']*100:+.2f}% (加{sp['n_add']}/减{sp['n_cut']})")
        print(f"    全样本方向差: {sp_full['spread']*100:+.2f}%")
        print(f"    过拟合比: {f'{ratio:.2f}' if ratio is not None else 'N/A'}"
              f"{' ⚠️过拟合' if ratio is not None and ratio < 0.5 else ''}")

    # 判定
    valid = [r for r in results if r["spread"] is not None]
    n_pos = sum(1 for r in valid if r["spread"] > 0)
    mean_spread = sum(r["spread"] for r in valid) / len(valid) if valid else 0
    mean_ratio = sum(r["ratio"] for r in valid if r["ratio"] is not None) / \
                 sum(1 for r in valid if r["ratio"] is not None) if any(r["ratio"] is not None for r in valid) else 0

    i_pass = n_pos >= 3
    j_pass = mean_spread > 0.003
    k_pass = mean_ratio > 0.5
    verdict = i_pass and j_pass and k_pass

    verdict_line = ("✅ 通过 —— 加权组合可作为弱参考（措辞仍弱化，不构成指令）"
                    if verdict else
                    "❌ 未通过 —— 加权组合外样本不稳定，不可上线输出操作档位")

    print(f"\n{'='*60}")
    print(f"判定 I（≥3/5 窗口方向差>0）：{'✅' if i_pass else '❌'} ({n_pos}/{len(valid)})")
    print(f"判定 J（外样本均值>0.3%）：{'✅' if j_pass else '❌'} ({mean_spread*100:+.2f}%)")
    print(f"判定 K（过拟合比>0.5）：{'✅' if k_pass else '❌'} ({mean_ratio:.2f})")
    print(f"\n结论：{verdict_line}")

    # 写报告
    lines = ["# Walk-forward OOS 测试：加权权重外样本稳定性", "",
             f"> 生成：2026-08-25 · 样本 {len(samples)} 条（已剔除 025687）",
             f"> 区间 {dates[0]}~{dates[-1]} · 5 窗口滚动 OLS 拟合 → 外样本验证", "",
             "## 判定标准（事先写死）", "",
             "- **I** ≥3/5 窗口外样本方向差 > 0",
             "- **J** 外样本方向差均值 > 0.3%（最小经济意义阈值）",
             "- **K** 外样本/全样本方向差 > 0.5（过拟合不严重）",
             "", "## 逐窗口结果", "",
             "| 窗口 | 外样本方向差 | 全样本方向差 | 过拟合比 | train IC(est/comp/score) | β(est/comp/score) |",
             "|---|---:|---:|---:|---|---|"]
    for r in valid:
        ratio_str = f"{r['ratio']:.2f}" if r['ratio'] is not None else "N/A"
        lines.append(f"| {r['name']} | {r['spread']*100:+.2f}% | {r['spread_full']*100:+.2f}% | "
                     f"{ratio_str} | "
                     f"{r['est_ic']:+.3f}/{r['comp_ic']:+.3f}/{r['score_ic']:+.3f} | "
                     f"{r['beta']['b1']:+.4f}/{r['beta']['b2']:+.4f}/{r['beta']['b3']:+.4f} |")
    lines += ["", "## 判定汇总", "",
              f"- I（≥3/5>0）：{'✅' if i_pass else '❌'} ({n_pos}/{len(valid)})",
              f"- J（均值>0.3%）：{'✅' if j_pass else '❌'} ({mean_spread*100:+.2f}%)",
              f"- K（过拟合比>0.5）：{'✅' if k_pass else '❌'} ({mean_ratio:.2f})",
              "", "## 结论", "", f"**{verdict_line}**", "",
              "<sub>局限：①11:30 午盘未回测；②025687 因样本不足已剔除；③OLS 为线性模型，"
              "未捕捉非线性交互；④外样本窗口含不同市场环境（牛/熊/震荡），方向差波动大属正常。</sub>"]
    (BASE_DIR / "output" / "backtest_oos_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[done] 报告 → output/backtest_oos_report.md")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
