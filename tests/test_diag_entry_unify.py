"""B++-6（2026-09-23）：统一剩余诊断入口 contract tests。

fast 层纪律：源码断言 + 合成数据纯函数，零网络、不跑各脚本 main()。钉死四条：
1) 四个诊断入口（rolling_oos / walk_forward / quantile_calib / lofo）的样本装载
   统一 require_fwds=()——sample construction ≠ target availability（B3 纪律推广）；
2) backtest_forecast 自身的 require_fwds=() 不回退（B3 锚点守护）；
3) policy 不再保留第二套手搓 bootstrap（default_rng/rng.choice 清零），CI 引擎
   统一复用 cluster_bootstrap_ci（契约 B §8）；
4) eval_policy 迁移后数值逐位不变：与旧手搓实现同参数复算（seed=42、999 次、
   2.5/97.5 分位）逐位相等；样本不足分支诚实缺省（不造 CI）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from backtest_forecast_policy import eval_policy   # noqa: E402

REQUIRE_FWDS_FILES = (
    "backtest_rolling_oos.py", "backtest_walk_forward.py",
    "backtest_quantile_calib.py", "backtest_lofo.py")


class TestSourceWiring(unittest.TestCase):
    def _src(self, name: str) -> str:
        return (BASE_DIR / name).read_text(encoding="utf-8")

    def test_four_diag_entries_use_require_fwds_empty(self):
        for name in REQUIRE_FWDS_FILES:
            self.assertIn("lambda: load_samples(require_fwds=())",
                          self._src(name), name)

    def test_forecast_entry_not_regressed(self):
        """B3 锚点：backtest_forecast 自身的 require_fwds=() 不得回退。"""
        self.assertIn("require_fwds=()", self._src("backtest_forecast.py"))

    def test_policy_no_second_bootstrap_impl(self):
        src = self._src("backtest_forecast_policy.py")
        self.assertNotIn("rng.choice", src)
        self.assertNotIn("default_rng", src)
        self.assertIn("cluster_bootstrap_ci(", src)


class TestEvalPolicyBehavior(unittest.TestCase):
    @staticmethod
    def _synthetic(n_days: int, with_add: bool = True):
        """每日 2 加 + 2 不动 + 1 减；日期递增使日块差异充分。"""
        dates, act, fwd1 = [], [], []
        for i in range(1, n_days + 1):
            d = f"2026-03-{i:02d}"
            base = 0.001 * i
            rows = ([2, 2, 1, 1, 0] if with_add else [1, 1, 1, 1, 0])
            vals = {"2": [base + 0.010, base + 0.020],
                    "1": [base + 0.004, base + 0.006,
                          base + 0.005, base + 0.007],   # 4 个：with_add=False 时四行不动全要 pop
                    "0": [base - 0.003]}
            for a in rows:
                dates.append(d)
                act.append(a)
                fwd1.append(vals[str(a)].pop(0))
        return dates, np.array(act), np.array(fwd1, dtype=float)

    def test_ci_bitwise_identical_to_legacy_reference(self):
        """迁移必须逐位保真：与旧手搓实现同参数复算（seed=42、999 次、
        2.5/97.5 分位、round 5）结果完全一致。"""
        dates, act, fwd1 = self._synthetic(12)
        out = eval_policy("ml", act, fwd1, dates)
        self.assertGreater(out["n_add"], 0)
        # —— 旧实现参考复算（迁移前算法逐字）——
        uniq = sorted(set(dates))
        daily = {}
        for d, a, r in zip(dates, act, fwd1):
            v = daily.setdefault(d, {"a": [], "h": []})
            if a == 2:
                v["a"].append(r)
            elif a == 1:
                v["h"].append(r)
        blocks = []
        for d in uniq:
            v = daily.get(d)
            if v and v["a"] and v["h"]:
                blocks.append(float(np.mean(v["a"]) - np.mean(v["h"])))
        rng = np.random.default_rng(42)
        boots = []
        for _ in range(999):
            samp = rng.choice(blocks, size=len(blocks), replace=True)
            boots.append(float(np.mean(samp)))
        want = [round(float(np.percentile(boots, 2.5)), 5),
                round(float(np.percentile(boots, 97.5)), 5)]
        self.assertEqual(out["excess_add_ci"], want)

    def test_too_few_pair_days_no_ci(self):
        """10 日但仅 8 日有加+不动配对 → blocks<10 → 不造 CI（诚实缺省）。"""
        dates, act, fwd1 = self._synthetic(10)
        # 末两日的加行改为不动行 → 这两日无 add
        for i, (d, a) in enumerate(zip(dates, act)):
            if d in ("2026-03-09", "2026-03-10") and a == 2:
                act[i] = 1
        out = eval_policy("ml", act, fwd1, dates)
        self.assertNotIn("excess_add_ci", out)

    def test_no_add_rows_no_ci(self):
        dates, act, fwd1 = self._synthetic(12, with_add=False)
        out = eval_policy("ml", act, fwd1, dates)
        self.assertEqual(out["n_add"], 0)
        self.assertNotIn("excess_add_ci", out)


if __name__ == "__main__":
    unittest.main()
