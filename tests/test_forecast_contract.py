"""Forecast Contract（重定）contract test 骨架（V4.4，2026-09-23）。

fast 层纪律：纯函数 + 合成行，不碰真实 data/、零网络。
钉死契约五条核心纪律（条文见 core/forecast_contract.py）：
C1 裁决 label = T 日最终 NAV 分母，且 est_chg 不进 label；
C2 1455 降级为「决策输入 + 盈亏核算」（委托等价，不重写公式）；
C3 裁决基线 = est_chg 单因子（old +est_chg / 1455 −est_chg）；
C4 量纲 = fraction；
C5 机械耦合恒等式作数值不变量钉死（防「再把 est_chg 塞进裁决 label 分母」）。
"""
from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import forecast_contract as F   # noqa: E402
from core import pit1455_contract as C    # noqa: E402

# 合成 NAV（fraction 域）：T-1 = 5.00，T = 5.05（a_T = +1%），T+1 = 5.15
NAV_T_MINUS_1, NAV_T, NAV_T1 = 5.00, 5.05, 5.15
A_T = NAV_T / NAV_T_MINUS_1 - 1          # T 日真实涨跌 +0.01


class TestAdjudicationLabel(unittest.TestCase):
    def test_golden_matches_spread_formula(self):
        """C1 黄金：必须与 backtest_spread 的 navs[i+h]/navs[i]−1 公式逐位一致（生产口径）。"""
        self.assertAlmostEqual(
            F.adjudication_label(NAV_T, NAV_T1), NAV_T1 / NAV_T - 1, places=12)

    def test_est_chg_cannot_enter_label(self):
        """C1：est_chg 不是 adjudication_label 的参数——耦合在定义上拆掉。"""
        params = inspect.signature(F.adjudication_label).parameters
        self.assertNotIn("est_chg", params)
        self.assertNotIn("est", params)

    def test_invalid_inputs(self):
        self.assertIsNone(F.adjudication_label(None, NAV_T1))
        self.assertIsNone(F.adjudication_label(NAV_T, None))
        self.assertIsNone(F.adjudication_label(0.0, NAV_T1))
        self.assertIsNone(F.adjudication_label(-1.0, NAV_T1))


class TestCaliberRoles(unittest.TestCase):
    def test_1455_role_is_accounting_only(self):
        """C2：provenance 钉死 1455 角色 = 决策输入 + 盈亏核算（不作裁决目标）。"""
        p = F.contract_provenance()
        self.assertEqual(p["contract_version"], F.CONTRACT_VERSION)
        self.assertEqual(p["pit1455_role"], "decision_input_and_pnl_accounting_only")
        self.assertEqual(p["adjudication_baseline"],
                         "est_chg 单因子（decision_edge = RankIC(模型) - RankIC(est_chg)）")
        # 1455 式子权威仍是 pit1455-v1（两契约并存、各司其职）
        self.assertEqual(p["pit1455_contract_version"], C.CONTRACT_VERSION)

    def test_baseline_score_directions(self):
        """C3：old 口径基线 = +est_chg；1455 口径（须预注册）= −est_chg；未知口径 fail-closed。"""
        self.assertAlmostEqual(F.adjudication_baseline_score(0.0123), 0.0123, places=12)
        self.assertAlmostEqual(F.adjudication_baseline_score(0.0123, F.CALIBER_ADJUDICATION),
                               0.0123, places=12)
        self.assertAlmostEqual(F.adjudication_baseline_score(0.0123, F.CALIBER_ACCOUNTING),
                               -0.0123, places=12)
        self.assertIsNone(F.adjudication_baseline_score(None))
        with self.assertRaises(ValueError):
            F.adjudication_baseline_score(0.01, "bogus")

    def test_unit_is_fraction(self):
        """C4：est_chg 量纲 = fraction；唯一换算桥仍是 pit1455_contract。"""
        self.assertEqual(C.EST_CHG_UNIT, "fraction")
        self.assertAlmostEqual(C.est_chg_from_pct(1.23), 0.0123, places=12)


class TestAccountingAndCoupling(unittest.TestCase):
    def test_accounting_delegates_to_pit1455(self):
        """C2：核算口径 = pit1455-v1 公式的委托（不重写）；None 透传。"""
        hat = C.estimate_nav_at_1455(NAV_T_MINUS_1, 0.008)
        want = C.fwd_from_1455(hat, NAV_T1)
        self.assertAlmostEqual(F.accounting_label(NAV_T_MINUS_1, 0.008, NAV_T1), want, places=12)
        self.assertIsNone(F.accounting_label(None, 0.008, NAV_T1))
        self.assertIsNone(F.accounting_label(NAV_T_MINUS_1, None, NAV_T1))

    def test_coupling_identity_pinned(self):
        """C5：机械耦合恒等式是钉死的数值不变量——
        1 + fwd1455 = (1 + fwd_old) · (1 + a_T) / (1 + est_chg)
        任何未来「把 est_chg 塞进裁决 label 分母」的改动必须先撞穿这条测例。
        """
        est = 0.008
        fwd_old = F.adjudication_label(NAV_T, NAV_T1)
        fwd_1455 = F.accounting_label(NAV_T_MINUS_1, est, NAV_T1)
        lhs = 1.0 + fwd_1455
        rhs = (1.0 + fwd_old) * (1.0 + A_T) / (1.0 + est)
        self.assertAlmostEqual(lhs, rhs, places=12)

    def test_degenerates_to_adjudication_when_estimate_is_true(self):
        """黄金：est_chg 恰等于 T 日真实涨跌时，核算 label 必须 == 裁决 label
        （nav_hat = navs[T]，两口径重合）——钉死两契约之间的一致性。"""
        fwd_old = F.adjudication_label(NAV_T, NAV_T1)
        fwd_1455 = F.accounting_label(NAV_T_MINUS_1, A_T, NAV_T1)
        self.assertAlmostEqual(fwd_1455, fwd_old, places=12)


if __name__ == "__main__":
    unittest.main()
