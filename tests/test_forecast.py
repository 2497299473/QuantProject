"""v5 多周期预测引擎测试（GPT-5.6Luna 评审落地）。

- 契约：ForecastEngine.predict 输出 T1/T3/T5 三周期（P_up/P_flat/P_down + 收益区间 + 置信度）
- 数据纪律：样本不足时返回 model_ready=false 占位，不假装预测
- 隔离：account（仓位）不进市场预测特征维
"""
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import forecast_engine as fe


def _fake_samples(n=300):
    """构造合成样本：est_chg 越正 → fwd 越正（让模型有信号可学）。"""
    import random
    rng = random.Random(0)
    rows = []
    for i in range(n):
        est_chg = (i % 7) / 10.0 - 0.3          # 有规律
        fwd = est_chg * 2.0 + rng.uniform(-0.02, 0.02)
        breadth = est_chg * 3.0
        rows.append({
            "fund": "TEST", "date": f"2024-01-{(i % 28) + 1:02d}",
            "est_chg": est_chg, "est_sign": 1 if est_chg > 0 else (-1 if est_chg < 0 else 0),
            "breadth": breadth, "concentration": 0.5, "covered_pct": 70.0,
            "composite": 1 if est_chg > 0 else -1, "score": 1 if est_chg > 0 else -1,
            "fwd1": fwd, "fwd3": fwd * 1.5, "fwd5": fwd * 2.0,
            "fwd10": fwd * 3.0, "fwd20": fwd * 4.0,
        })
    return rows


class TestForecastEngine(unittest.TestCase):
    def test_predict_contract_fields(self):
        """predict 输出必须含 T1/T3/T5 三周期 + 每个周期核心字段。

        v8（2026-08-30）：真分位数回归恢复 q50；max_dd 仍未建模。
        """
        samples = _fake_samples()
        eng = fe.ForecastEngine()
        eng.fit(samples)
        f = eng.predict(samples[0], freshness=1.0)
        for hf_name, hf in (("T1", f.t1), ("T3", f.t3), ("T5", f.t5)):
            self.assertIn(hf_name, fe.forecast_to_cn(f))
            for attr in ("p_up", "p_flat", "p_down", "e_return", "q10", "q50", "q90",
                         "confidence", "model_ready"):
                self.assertTrue(hasattr(hf, attr), f"{hf_name} 缺 {attr}")
            for gone in ("max_dd",):
                self.assertFalse(hasattr(hf, gone), f"{hf_name} 不应再有 {gone}")

    def test_quantile_monotonicity(self):
        """v8 新增：同一预测的 q10 ≤ q50 ≤ q90（分位数单调性，回归器交叉时已重排）。"""
        samples = _fake_samples()
        eng = fe.ForecastEngine()
        eng.fit(samples)
        f = eng.predict(samples[0], freshness=1.0)
        for hf in (f.t1, f.t3, f.t5):
            self.assertLessEqual(hf.q10, hf.q50 + 1e-12)
            self.assertLessEqual(hf.q50, hf.q90 + 1e-12)


class TestInferPath(unittest.TestCase):
    """v8 P0-⑤：路径推断由方向概率主导（不再看 E[return] 符号）。

    回归场景（GPT 四审⑰）：T+5 P(down)=57% 时不得输出「持续上行」。
    """

    @staticmethod
    def _hf(h: int, p_up: float, p_flat: float, p_down: float,
            e: float = 0.0) -> fe.HorizonForecast:
        return fe.HorizonForecast(horizon=h, p_up=p_up, p_flat=p_flat,
                                  p_down=p_down, e_return=e,
                                  q10=e - 0.02, q50=e, q90=e + 0.02,
                                  confidence=0.5, model_ready=True)

    def test_down_prob_overrides_increasing_e(self):
        """方向概率全偏下 + E[return] 单调递增 → 必须判「持续走弱」（旧版会判上行）。"""
        eng = fe.ForecastEngine()
        hf_list = [self._hf(h, p_up=0.35, p_flat=0.08, p_down=0.57, e=0.01 * i)
                   for i, h in enumerate((1, 3, 5), start=1)]
        path = eng._infer_path(hf_list)
        self.assertNotIn("上行", path)
        self.assertIn("走弱", path)

    def test_all_up_dominant(self):
        eng = fe.ForecastEngine()
        hf_list = [self._hf(h, p_up=0.65, p_flat=0.05, p_down=0.30)
                   for h in (1, 3, 5)]
        self.assertEqual(eng._infer_path(hf_list), "短期→中期持续上行")

    def test_v_shape(self):
        """T+1 偏下 → T+3/T+5 偏上 = V 型修复（旧版按 e 符号无法稳定表达）。"""
        eng = fe.ForecastEngine()
        hf_list = [self._hf(1, p_up=0.30, p_flat=0.10, p_down=0.60),
                   self._hf(3, p_up=0.60, p_flat=0.10, p_down=0.30),
                   self._hf(5, p_up=0.65, p_flat=0.10, p_down=0.25)]
        self.assertIn("V 型", eng._infer_path(hf_list))

    def test_peak_then_fade(self):
        eng = fe.ForecastEngine()
        hf_list = [self._hf(1, p_up=0.60, p_flat=0.10, p_down=0.30),
                   self._hf(3, p_up=0.25, p_flat=0.10, p_down=0.65),
                   self._hf(5, p_up=0.20, p_flat=0.10, p_down=0.70)]
        self.assertIn("冲高后回落", eng._infer_path(hf_list))

    def test_weak_signal_falls_to_mixed_path(self):
        """主导概率 <0.5 → mixed，不假装有明确方向。"""
        eng = fe.ForecastEngine()
        hf_list = [self._hf(1, p_up=0.40, p_flat=0.35, p_down=0.25),
                   self._hf(3, p_up=0.38, p_flat=0.34, p_down=0.28),
                   self._hf(5, p_up=0.37, p_flat=0.33, p_down=0.30)]
        self.assertIn("无明确单一路径", eng._infer_path(hf_list))

    def test_not_ready_returns_insufficient(self):
        eng = fe.ForecastEngine()
        hf = self._hf(1, 0.9, 0.05, 0.05)
        hf.model_ready = False
        self.assertEqual(eng._infer_path([hf]), "数据不足以判断路径")


class TestReturnQuantileModel(unittest.TestCase):
    """v8：真分位数回归（quantile loss HGBR）单元测试（GPT 四审③④⑤）。"""

    @staticmethod
    def _samples(n=400):
        import random
        rng = random.Random(11)
        rows = []
        for i in range(n):
            x = rng.uniform(-1, 1)
            fwd = 0.05 * x + rng.gauss(0, 0.01)   # 状态依赖 + 对称噪声
            rows.append({
                "fund": "T", "date": f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}",
                "est_chg": x, "est_sign": 1 if x > 0 else (-1 if x < 0 else 0),
                "breadth": x, "concentration": 0.5, "covered_pct": 70.0,
                "composite": 0, "score": x,
                **{f"fwd{h}": fwd for h in (1, 3, 5)},
            })
        return rows

    @staticmethod
    def _vec(x: float) -> list[float]:
        # 与 FEATURE_KEYS 对齐：est_chg, est_sign, breadth, concentration,
        # covered_pct, composite, score
        # B1（2026-08-31）：模型协议改为「值 + missing_mask」双列（14 维），
        # 测试特征需同步——每个值后跟 mask=0（无缺失）。
        vals = [x, 1 if x > 0 else (-1 if x < 0 else 0), x, 0.5, 70.0, 0, x]
        out = []
        for v in vals:
            out.append(float(v))
            out.append(0.0)
        return out

    def test_fit_predict_monotone_and_state_dependent(self):
        if not fe._SKLEARN:
            self.skipTest("sklearn 不可用")
        qm = fe.ReturnQuantileModel(1)
        self.assertTrue(qm.fit(self._samples()))
        lo = qm.predict_dist(self._vec(-1.0))
        hi = qm.predict_dist(self._vec(1.0))
        for d in (lo, hi):
            self.assertLessEqual(d["q10"], d["q50"] + 1e-12)
            self.assertLessEqual(d["q50"], d["q90"] + 1e-12)
        # 状态依赖：x=+1 的条件分布应整体高于 x=-1（正态近似同宽区间做不到的）
        self.assertLess(lo["q90"], hi["q50"])
        self.assertLess(lo["q50"], hi["q10"])

    def test_untrained_returns_zeros(self):
        qm = fe.ReturnQuantileModel(1)
        d = qm.predict_dist(self._vec(0.0))
        self.assertEqual(d["e"], 0.0)
        self.assertEqual(d["q50"], 0.0)
        self.assertEqual(d["q10"], 0.0)

    def test_fit_requires_min_samples(self):
        qm = fe.ReturnQuantileModel(1)
        self.assertFalse(qm.fit(self._samples(30)))

    def test_probabilities_sum_to_one(self):
        """P(up)+P(flat)+P(down) 应约等于 1（softmax/兼容性）。"""
        samples = _fake_samples()
        eng = fe.ForecastEngine()
        eng.fit(samples)
        f = eng.predict(samples[0], freshness=1.0)
        for hf in (f.t1, f.t3, f.t5):
            total = hf.p_up + hf.p_flat + hf.p_down
            self.assertAlmostEqual(total, 1.0, delta=0.02)

    def test_zero_samples_returns_placeholder(self):
        """零/极少数样本 → model_ready=false 占位，不崩溃。"""
        eng = fe.ForecastEngine()
        eng.fit([])
        f = eng.predict({}, freshness=0.5)
        self.assertFalse(f.t1.model_ready)
        self.assertEqual(f.state, "UNKNOWN")

    def test_fit_requires_min_samples(self):
        """不足 min 样本 → 不训练，model_ready 为 false。"""
        eng = fe.ForecastEngine()
        ok = eng.fit(_fake_samples(10))
        self.assertFalse(ok)
        self.assertFalse(eng._fit_ok)

    def test_confidence_bound(self):
        """置信度 ∈ [0,1]。"""
        samples = _fake_samples()
        eng = fe.ForecastEngine()
        eng.fit(samples)
        f = eng.predict(samples[0], freshness=0.9)
        for hf in (f.t1, f.t3, f.t5):
            self.assertGreaterEqual(hf.confidence, 0.0)
            self.assertLessEqual(hf.confidence, 1.0)


if __name__ == "__main__":
    unittest.main()
