"""B 契约 §15-B3（2026-09-23）：样本构建与 target 可用性分离。

load_samples 新增仅关键字参数 require_fwds（默认 FWD_LIST，旧行为零变更）：
Forecast 短周期可声明自己需要的标签集合，不再被 fwd20 连坐截掉时间末端
样本。本文件只做离线验证：签名契约、保留判定纯函数、excess10 安全补齐、
源码级入口门（沿用仓库 AST/源码断言先例）。
"""
import inspect
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from backtest_spread import (   # noqa: E402
    FWD_LIST, _attach_excess10, _sample_row_complete, load_samples)


def _row(**fwds):
    row = {"fund": "002112", "date": "2025-01-06"}
    row.update(fwds)
    return row


class TestRequireFwdsContract(unittest.TestCase):
    def test_signature_keyword_only_default_fwd_list(self):
        sig = inspect.signature(load_samples)
        p = sig.parameters["require_fwds"]
        self.assertIs(p.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(p.default, FWD_LIST)
        self.assertEqual(FWD_LIST, (5, 10, 20))     # 默认口径零变更

    def test_default_predicate_requires_full_fwd_list(self):
        full = _row(fwd5=0.01, fwd10=0.02, fwd20=0.03)
        short_only = _row(fwd1=0.01, fwd3=0.02, fwd5=0.03)   # 缺 fwd20
        self.assertTrue(_sample_row_complete(full, FWD_LIST))
        self.assertFalse(_sample_row_complete(short_only, FWD_LIST))  # 旧行为

    def test_forecast_horizons_decoupled_from_fwd20(self):
        tail = _row(fwd1=0.01)                       # 时间末端：只有 fwd1
        self.assertFalse(_sample_row_complete(tail, (1, 3, 5)))
        self.assertTrue(_sample_row_complete(tail, (1,)))
        self.assertTrue(_sample_row_complete(tail, ()))   # 入口全保留，build_xy 逐 horizon 过滤

    def test_excess10_skips_missing_fwd10_without_error(self):
        samples = [
            _row(fwd10=0.10),
            _row(fwd10=0.20),
            _row(fund="025687"),                     # 无 fwd10（B3 后合法行）
        ]
        _attach_excess10(samples, ["002112", "025687"])
        self.assertAlmostEqual(samples[0]["excess10"], -0.05)
        self.assertAlmostEqual(samples[1]["excess10"], 0.05)
        self.assertNotIn("excess10", samples[2])     # 安全跳过，不报错不造数

    def test_excess10_values_unchanged_for_complete_rows(self):
        """既有完整样本的 excess10 与旧实现同算式同结果（B3 红线）。"""
        samples = [_row(fwd10=a) for a in (0.10, 0.20, 0.30)]
        _attach_excess10(samples, ["002112"])
        base = (0.10 + 0.20 + 0.30) / 3
        for s, a in zip(samples, (0.10, 0.20, 0.30)):
            self.assertAlmostEqual(s["excess10"], a - base, places=12)

    def test_entry_gate_wired_in_source(self):
        src = (BASE_DIR / "backtest_spread.py").read_text(encoding="utf-8")
        self.assertNotIn('all(f"fwd{f}" in row for f in FWD_LIST)', src)
        self.assertIn("_sample_row_complete(row, require_fwds)", src)
        fc = (BASE_DIR / "backtest_forecast.py").read_text(encoding="utf-8")
        self.assertIn("require_fwds=()", fc)         # Forecast 入口解耦
        tr = (BASE_DIR / "train_forecast_model.py").read_text(encoding="utf-8")
        self.assertIn("require_fwds=()", tr)         # 训练侧同口径


if __name__ == "__main__":
    unittest.main()
