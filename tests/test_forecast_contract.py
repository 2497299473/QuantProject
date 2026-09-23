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

    def test_spread_label_path_consumes_contract(self):
        """P0-2 护栏：backtest_spread 的 label 构造必须委托唯一真源，
        不得再出现自算 ``navs[i + fwd][1] / navs[i][1] - 1``（AST 级钉死，
        防未来回退成影子公式）。
        """
        import ast
        src = (BASE_DIR / "backtest_spread.py").read_text(encoding="utf-8")
        self.assertIn("from core.forecast_contract import adjudication_label", src)
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "adjudication_label"]
        self.assertGreaterEqual(len(calls), 2, "FWD_LIST/EXTRA_FWD 两处均须委托真源")
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
                    and n.value.id == "navs"):
                continue
            for parent in ast.walk(tree):
                if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div) \
                        and any(x is n for x in ast.walk(parent)) and n is not parent:
                    self.fail("backtest_spread 仍在用 navs 下标做除法——label 自算回退")


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


class TestHistoricalFeatureMode(unittest.TestCase):
    """EOD_PROXY 口径钉死（2026-09-23）：历史样本的个股特征是用当日收盘价
    近似的 14:55，不是真实 14:55 快照。此声明必须可机检，防被文档漂移冲掉。
    """

    def test_mode_constant_is_eod_proxy(self):
        import backtest_spread as B
        self.assertEqual(B.HISTORICAL_FEATURE_MODE, "EOD_PROXY")
        self.assertIn(B.HISTORICAL_FEATURE_MODE, B.HISTORICAL_FEATURE_MODES)

    def test_enum_contains_future_1455_snapshot(self):
        """969-23 起真实 14:55 快照攒够后，口径枚举须能表达 PIT_1455_SNAPSHOT。"""
        import backtest_spread as B
        self.assertEqual(
            set(B.HISTORICAL_FEATURE_MODES), {"EOD_PROXY", "PIT_1455_SNAPSHOT"})

    def test_source_still_uses_eod_close_for_stock_est(self):
        """实现层钉死：个股估涨仍取 `bisect_right(dates, d) - 1`（d 日收盘）。
        一旦真换成 14:55 快照，此测例会红 ⇒ 强制同步改口径常量，
        不许「代码改了、标注没改」的静默漂移。
        """
        src = (BASE_DIR / "backtest_spread.py").read_text(encoding="utf-8")
        self.assertIn("j = bisect_right(dates, d) - 1", src)

    def test_frozen_0910_meta_not_retroactively_rewritten(self):
        """冻结件诚实性：0910 meta 是冻结事实，不得追溯改写加新字段。"""
        p = BASE_DIR / "forecast_outputs" / "samples_frozen_20260910.meta.json"
        if not p.exists():
            self.skipTest("0910 冻结件不在本地")
        import json as _json
        meta = _json.loads(p.read_text(encoding="utf-8"))
        self.assertNotIn("historical_feature_mode", meta)
        self.assertEqual(meta["schema_version"], "1")


if __name__ == "__main__":
    unittest.main()
