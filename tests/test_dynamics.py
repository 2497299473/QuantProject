"""动力学指标（MACD 背驰近似 + 吻结构 + 防狼术）单元测试。

运行：python3 -m unittest tests.test_dynamics -v
"""
import datetime
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.lookthrough import MacdDynamics, StockSignals

CFG = {"dynamics": {"div_area_ratio": 0.8, "div_valid_bars": 15, "zero_pull_ratio": 0.15,
                    "ma": [5, 20, 60], "dd_window": 60}}


def _klines_from_closes(closes, start="2024-01-01"):
    d0 = datetime.date.fromisoformat(start)
    out, prev = [], None
    for i, c in enumerate(closes):
        out.append(((d0 + datetime.timedelta(days=i)).isoformat(), 0.0, c, max(c, prev or c), 0.0))
        prev = c
    return out


def _feed(ss, klines):
    for date, _o, close, high, _l in klines:
        ss.update(date, close, high)


class TestDivergence(unittest.TestCase):
    def test_top_divergence_with_zero_pull(self):
        """强拉 → 深回调（DIF 穿 0 轴）→ 减速新高 → 翻号确认：应检出顶背驰(-1)。"""
        seg1 = [10 + 0.45 * i for i in range(30)]           # 高点 23.05
        seg2 = [seg1[-1] - 0.35 * i for i in range(1, 15)]  # 深回调至 18.15
        base = seg2[-1]
        seg3 = [base + 0.16 * i for i in range(1, 36)]      # 缓慢新高 23.75
        seg4 = [seg3[-1] - 0.3 * i for i in range(1, 9)]    # 翻号关闭正柱簇
        dyn = MacdDynamics()
        signals = [dyn.update(c) for c in seg1 + seg2 + seg3 + seg4]
        self.assertIn(-1, signals, "深回调后的减速新高应产生顶背驰")

    def test_bottom_divergence_with_zero_pull(self):
        """急跌 → 温和长反弹（DIF 回拉穿 0 轴）→ 缓速新低 → 翻号确认：应检出底背驰(+1)。"""
        seg1 = [60 - 0.5 * i for i in range(24)]            # 急跌 60 → 48.5
        seg2 = [seg1[-1] + 0.15 * i for i in range(1, 61)]  # 温和长反弹至 57.5（黄白线穿 0 轴）
        base = seg2[-1]
        seg3 = [base - 0.2 * i for i in range(1, 56)]       # 更缓速的创新低 46.5
        seg4 = [seg3[-1] + 0.3 * i for i in range(1, 9)]    # 翻号关闭负柱簇
        dyn = MacdDynamics()
        signals = [dyn.update(c) for c in seg1 + seg2 + seg3 + seg4]
        self.assertIn(1, signals, "长反弹穿0轴后的缓速新低应产生底背驰")

    def test_no_divergence_without_zero_pull(self):
        """课24 前提：B 段黄白线未回拉 0 轴（仅小幅喘息）→ 不允许比较，不报背驰。"""
        seg1 = [10 + 0.5 * i for i in range(35)]            # 强趋势
        seg2 = [seg1[-1] + 0.05 * i * (1 if i % 2 else -0.8) for i in range(1, 8)]  # 浅幅喘息
        base = seg1[-1]
        seg3 = [base + 0.2 * i for i in range(1, 30)]       # 再上行（喘息期 DIF 远离 0 轴）
        seg4 = [seg3[-1] - 0.4 * i for i in range(1, 10)]
        dyn = MacdDynamics()
        signals = [dyn.update(c) for c in seg1 + seg2 + seg3 + seg4]
        self.assertNotIn(-1, signals, "黄白线未回 0 轴的两段不应判背驰（skill 课24 前提）")

    def test_steady_trend_no_divergence(self):
        closes = [10 + 0.5 * i for i in range(80)] + [10 + 40 - 0.4 * i for i in range(1, 20)] \
                 + [30 + 0.5 * i for i in range(1, 40)]
        dyn = MacdDynamics()
        signals = [dyn.update(c) for c in closes]
        self.assertNotIn(-1, signals[:60], "匀速趋势前段不应有顶背驰")


class TestFanglang(unittest.TestCase):
    def test_fanglang_after_prolonged_decline(self):
        """课103 防狼术：持续下跌 → DIF/DEA 双双 0 轴下 → 警报=1。"""
        dyn = MacdDynamics()
        for c in [100 - 0.4 * i for i in range(60)]:
            dyn.update(c)
        self.assertEqual(dyn.fanglang, 1, "长期下跌后黄白线应在 0 轴下方")

    def test_no_fanglang_in_uptrend(self):
        dyn = MacdDynamics()
        for c in [10 + 0.4 * i for i in range(60)]:
            dyn.update(c)
        self.assertEqual(dyn.fanglang, 0, "上涨趋势不应触发防狼术")


class TestCausality(unittest.TestCase):
    def test_prefix_invariance(self):
        """因果性：全量计算的第 k 个状态 == 只用前 k 根重算的第 k 个状态。"""
        closes = [10 + 3 * math.sin(i / 9) + 0.05 * i + (0.3 if i % 37 == 0 else 0)
                  for i in range(400)]
        klines = _klines_from_closes(closes)
        full = StockSignals(CFG)
        _feed(full, klines)
        for k in (120, 233, 399):
            part = StockSignals(CFG)
            _feed(part, klines[:k + 1])
            d = klines[k][0]
            self.assertEqual(part.series[d], full.series[d],
                             f"k={k} 的信号必须与全量计算的同一日一致（因果性）")

    def test_ma_alignment_direction(self):
        up = _klines_from_closes([10 + 0.3 * i for i in range(100)])
        s = StockSignals(CFG)
        _feed(s, up)
        self.assertEqual(s.series[up[-1][0]]["ma"], 1, "持续上涨应为完全多头排列")
        down = _klines_from_closes([100 - 0.3 * i for i in range(100)])
        s2 = StockSignals(CFG)
        _feed(s2, down)
        self.assertEqual(s2.series[down[-1][0]]["ma"], -1, "持续下跌应为完全空头排列")


if __name__ == "__main__":
    unittest.main(verbosity=2)
