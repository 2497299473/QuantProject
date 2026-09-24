"""Validation Evidence Schema v2（B++-1，2026-09-23）contract tests。

fast 层纪律：纯函数 + 合成数据，零网络、零写入（契约文本只读）。
钉死六条：
1) 常量/键集冻结（schema_version=2、四基金、三周期、指标键集，'ace' 禁入）；
2) 三态不可计算与真实数值正式分离（{value, status} 配对纪律）；
3) CI 节点 [lo, hi] 形态与负值可表达；
4) fail-closed 结构校验（缺基金/缺周期/未知键/旧名/坏 decision 全拦）；
5) midpoint_calibration_error 与 backtest_forecast.calibration_curve 数值同源；
6) 表达性反例：pooled PASS + fund FAIL + 四态同存 + JSON 往返不坍缩，
   且 schema 层绝不推导 decision（裁决权在 B++-3）。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import validation_schema as S           # noqa: E402
from core import forecast_contract as F           # noqa: E402
from core import pit1455_contract as P            # noqa: E402
from backtest_forecast import calibration_curve   # noqa: E402

CONTRACT_B = (BASE_DIR / "evidence" / "contracts"
              / "forecast_promotion_contract_B_20260923.md")


class TestFrozenConstants(unittest.TestCase):
    def test_schema_version_is_2(self):
        self.assertEqual(S.VALIDATION_SCHEMA_VERSION, 2)

    def test_status_constants_exact(self):
        self.assertEqual(
            S.ALL_EVIDENCE_STATUSES,
            ("OK", "UNKNOWN", "INSUFFICIENT_POWER", "NOT_COMPUTABLE"))
        self.assertEqual(len(set(S.ALL_EVIDENCE_STATUSES)), 4)

    def test_production_funds_exact(self):
        """契约 B §2 四基金（F1..F4）首次钉成代码常量；缺一多一都非法。"""
        self.assertEqual(S.PRODUCTION_FUNDS, ("002112", "002207", "022853", "025687"))

    def test_horizons_exact_and_canonical(self):
        self.assertEqual(S.HORIZONS, ("1", "3", "5"))
        self.assertEqual(S.canonical_horizon(1), "1")
        self.assertEqual(S.canonical_horizon("5"), "5")
        self.assertIsNone(S.canonical_horizon("7"))
        self.assertIsNone(S.canonical_horizon("01"))
        self.assertIsNone(S.canonical_horizon(1.0))

    def test_historical_feature_mode_enum_is_authoritative(self):
        self.assertEqual(S.HISTORICAL_FEATURE_MODES,
                         ("EOD_PROXY", "PIT_1455_SNAPSHOT"))
        self.assertIn("PIT_1455_SNAPSHOT", S.HISTORICAL_FEATURE_MODES)

    def test_kfp_comparability_states_exact(self):
        self.assertEqual(S.KFP_COMPARABILITY_STATES,
                         ("SAME", "DRIFTED", "UNKNOWN"))
        self.assertIn("kfp_comparability", S.PROVENANCE_KEYS)

    def test_baseline_tied_to_contract_c3_c4(self):
        """C3/C4：基线名与量纲必须与 core.forecast_contract / pit1455 同源。"""
        self.assertEqual(S.BASELINE_NAME, "est_chg")
        self.assertEqual(S.BASELINE_UNIT, "fraction")
        self.assertEqual(S.BASELINE_UNIT, P.EST_CHG_UNIT)
        self.assertIn("est_chg", F.contract_provenance()["adjudication_baseline"])

    def test_ace_name_banned_in_schema(self):
        """GPT 四审 P1：桶中点口径必须具名，'ace' 泛称禁入 schema 键集。"""
        self.assertNotIn("ace", S.METRIC_KEYS)
        self.assertIn(S.MIDPOINT_CALIBRATION_ERROR_KEY, S.METRIC_KEYS)
        for token in ("桶中点", "5 桶", "calibration_curve"):
            self.assertIn(token, S.MIDPOINT_CALIBRATION_ERROR_DEF)


class TestContractTextBinding(unittest.TestCase):
    def test_contract_b_lists_four_funds_and_states(self):
        """schema 常量必须能在契约 B 原文中找到出处（防单侧漂移）。"""
        if not CONTRACT_B.exists():
            self.skipTest("契约 B 文本不在本地")
        text = CONTRACT_B.read_text(encoding="utf-8")
        for fund in S.PRODUCTION_FUNDS:
            self.assertIn(fund, text)
        for token in ("INSUFFICIENT_POWER", "BLOCKED_BASELINE_EDGE",
                      "decision_edge_CI_lower > 0", "cluster bootstrap by trading day"):
            self.assertIn(token, text)


class TestMidpointFormulaTie(unittest.TestCase):
    def test_schema_definition_matches_calibration_curve(self):
        """冻结公式与现有实现数值同源：手动桶中点公式 == calibration_curve 的 ace。"""
        p = np.array([0.05, 0.12, 0.25, 0.30, 0.45, 0.55,
                      0.60, 0.70, 0.85, 0.95, 0.99, 0.50, 0.20, 0.33])
        y = np.array([0, 0, 1, 0, 1, 1, 0, 1, 1, 1, 1, 0, 0, 1], dtype=float)
        got = calibration_curve(y, p, nbins=5)["ace"]
        edges = np.linspace(0.0, 1.0, 6)
        acc, total = 0.0, 0
        for i in range(5):
            lo, hi = edges[i], edges[i + 1]
            mask = (p >= lo) if i == 4 else (p >= lo) & (p < hi)
            if mask.sum() == 0:
                continue
            acc += abs((lo + hi) / 2 - y[mask].mean()) * mask.sum()
            total += int(mask.sum())
        self.assertEqual(total, len(p))          # 5 桶全覆盖（合成数据不跳桶）
        self.assertAlmostEqual(got, acc / len(p), places=12)


class TestBlankTemplate(unittest.TestCase):
    def test_blank_validates_clean(self):
        ev = S.blank_evidence()
        ok, errs = S.validate_evidence(ev)
        self.assertTrue(ok)
        self.assertEqual(errs, [])

    def test_blank_shape(self):
        ev = S.blank_evidence()
        self.assertEqual(set(ev.keys()), set(S.TOP_LEVEL_KEYS))
        self.assertEqual(set(ev["funds"].keys()), set(S.PRODUCTION_FUNDS))
        for fund in S.PRODUCTION_FUNDS:
            self.assertEqual(set(ev["funds"][fund].keys()), set(S.HORIZONS))
        for node in [ev["pooled"][h] for h in S.HORIZONS] + [
                ev["funds"][f][h] for f in S.PRODUCTION_FUNDS for h in S.HORIZONS]:
            self.assertEqual(set(node.keys()), set(S.METRIC_KEYS))
            for slot in node.values():
                self.assertEqual(slot["status"], S.STATUS_UNKNOWN)
                self.assertIsNone(slot["value"])
        self.assertEqual(ev["power"]["frozen"], {"value": False, "status": "OK"})
        self.assertEqual(ev["baseline"], {"name": "est_chg", "unit": "fraction"})

    def test_blank_json_round_trip(self):
        """JSON canonical：字符串键、无 NaN、往返后仍合法且不含旧名 'ace'。"""
        ev = S.blank_evidence()
        text = json.dumps(ev)
        self.assertNotIn('"ace"', text)
        ev2 = json.loads(text)
        ok, errs = S.validate_evidence(ev2)
        self.assertTrue(ok)
        self.assertEqual(errs, [])

    def test_validate_is_idempotent(self):
        ev = S.blank_evidence()
        self.assertEqual(S.validate_evidence(ev), S.validate_evidence(ev))


class TestStatusPairing(unittest.TestCase):
    def _errs(self, mutate) -> list:
        ev = S.blank_evidence()
        mutate(ev)
        ok, errs = S.validate_evidence(ev)
        self.assertFalse(ok, "预期校验报错，但通过了")
        return errs

    def test_ok_with_null_rejected(self):
        errs = self._errs(lambda ev: ev["pooled"]["1"].update(
            {"rank_ic": {"value": None, "status": S.STATUS_OK}}))
        self.assertIn("value=null", "；".join(errs))

    def test_ok_with_nan_rejected(self):
        errs = self._errs(lambda ev: ev["pooled"]["1"].update(
            {"rank_ic": {"value": float("nan"), "status": S.STATUS_OK}}))
        self.assertIn("有限数值", "；".join(errs))

    def test_ok_with_bool_rejected_for_scalar(self):
        errs = self._errs(lambda ev: ev["pooled"]["1"].update(
            {"rank_ic": {"value": True, "status": S.STATUS_OK}}))
        self.assertIn("bool", "；".join(errs))

    def test_ok_with_string_rejected_for_scalar(self):
        errs = self._errs(lambda ev: ev["pooled"]["1"].update(
            {"rank_ic": {"value": "0.7", "status": S.STATUS_OK}}))
        self.assertIn("有限数值", "；".join(errs))

    def test_non_ok_must_carry_null(self):
        """三态不可计算一律 value=null——0.0 混装在结构上被禁。"""
        for st in (S.STATUS_UNKNOWN, S.STATUS_INSUFFICIENT_POWER,
                   S.STATUS_NOT_COMPUTABLE):
            errs = self._errs(lambda ev, s=st: ev["pooled"]["1"].update(
                {"rank_ic": {"value": 0.0, "status": s}}))
            self.assertIn("禁带数值", "；".join(errs))

    def test_unknown_status_rejected(self):
        errs = self._errs(lambda ev: ev["pooled"]["1"].update(
            {"rank_ic": {"value": None, "status": "PASS"}}))
        self.assertIn("非法 status", "；".join(errs))

    def test_ci_shape_enforced(self):
        errs = self._errs(lambda ev: ev["pooled"]["1"].update(
            {"rank_ic_ci": {"value": [0.1], "status": S.STATUS_OK}}))
        self.assertIn("[lo, hi]", "；".join(errs))
        errs = self._errs(lambda ev: ev["pooled"]["1"].update(
            {"rank_ic_ci": {"value": [0.1, float("nan")], "status": S.STATUS_OK}}))
        self.assertIn("CI[1]", "；".join(errs))

    def test_negative_ci_representable(self):
        """decision_edge CI 跨零必须能原样入档（不得钳成 0 或丢符号）。"""
        ev = S.blank_evidence()
        ev["pooled"]["5"].update({
            "decision_edge": S.ev_ok(-0.02),
            "decision_edge_ci": S.ev_ok([-0.09, 0.05]),
        })
        ok, errs = S.validate_evidence(ev)
        self.assertTrue(ok)
        self.assertEqual(errs, [])

    def test_ev_na_rejects_ok(self):
        with self.assertRaises(ValueError):
            S.ev_na(S.STATUS_OK)
        with self.assertRaises(ValueError):
            S.ev_na("PASS")


class TestFailClosedStructure(unittest.TestCase):
    def _errs(self, mutate) -> list:
        ev = S.blank_evidence()
        mutate(ev)
        ok, errs = S.validate_evidence(ev)
        self.assertFalse(ok, "预期校验报错，但通过了")
        return errs

    def test_missing_top_key(self):
        errs = self._errs(lambda ev: ev.pop("provenance"))
        self.assertIn("缺少顶层键 provenance", "；".join(errs))

    def test_unknown_top_key(self):
        errs = self._errs(lambda ev: ev.update({"extra": 1}))
        self.assertIn("未知顶层键", "；".join(errs))

    def test_missing_fund(self):
        errs = self._errs(lambda ev: ev["funds"].pop("025687"))
        self.assertIn("缺少生产基金 025687", "；".join(errs))

    def test_extra_fund(self):
        errs = self._errs(lambda ev: ev["funds"].update({"000001": S.metric_node()}))
        self.assertIn("未知基金", "；".join(errs))

    def test_missing_horizon(self):
        errs = self._errs(lambda ev: ev["funds"]["002112"].pop("3"))
        self.assertIn("缺少周期 3", "；".join(errs))

    def test_extra_horizon(self):
        errs = self._errs(lambda ev: ev["funds"]["002112"].update(
            {"2": S.metric_node()}))
        self.assertIn("非法周期键", "；".join(errs))

    def test_missing_metric_key(self):
        errs = self._errs(lambda ev: ev["pooled"]["5"].pop(
            S.MIDPOINT_CALIBRATION_ERROR_KEY))
        self.assertIn("缺少指标键", "；".join(errs))

    def test_legacy_ace_key_blocked(self):
        """旧名 'ace' 必须被键集冻结拦下（防静默回退泛称）。"""
        errs = self._errs(lambda ev: ev["pooled"]["5"].update({"ace": S.ev_ok(0.1)}))
        self.assertIn("ace", "；".join(errs))

    def test_bad_decision(self):
        errs = self._errs(lambda ev: ev.update({"decision": "bogus"}))
        self.assertIn("decision 非法", "；".join(errs))

    def test_baseline_tamper(self):
        errs = self._errs(lambda ev: ev.update(
            {"baseline": {"name": "est_chg", "unit": "pct"}}))
        self.assertIn("unit", "；".join(errs))
        errs = self._errs(lambda ev: ev.update(
            {"baseline": {"name": "mom10", "unit": "fraction"}}))
        self.assertIn("禁止临时换基线", "；".join(errs))

    def test_wrong_schema_version(self):
        errs = self._errs(lambda ev: ev.update({"schema_version": 1}))
        self.assertIn("schema_version", "；".join(errs))

    def test_protocol_rules(self):
        errs = self._errs(lambda ev: ev.update(
            {"protocol": {"version": 0, "feature_dim": 14}}))
        self.assertIn("正整数", "；".join(errs))
        errs = self._errs(lambda ev: ev.update(
            {"protocol": {"version": 1, "feature_dim": 14, "bogus": 1}}))
        self.assertIn("未知键", "；".join(errs))
        ev = S.blank_evidence()
        ev["protocol"] = {"version": 1, "feature_dim": 14}
        ok, errs = S.validate_evidence(ev)
        self.assertTrue(ok)
        self.assertEqual(errs, [])

    def test_power_frozen_must_be_bool(self):
        errs = self._errs(lambda ev: ev["power"].update({"frozen": S.ev_ok(1)}))
        self.assertIn("bool", "；".join(errs))


class TestAntiExampleExpressibility(unittest.TestCase):
    """契约 B 最怕的四类反例，schema 层必须能同帧表达而不坍缩：

    pooled PASS + fund edge FAIL、pooled PASS + power insufficient、
    edge CI 跨零、四态同存。裁决是 B++-3 的事，schema 只保证「装得下、分得清」。
    """

    def _full_pass_pooled(self, ev):
        ev["pooled"]["5"].update({
            "n": S.ev_ok(923),
            "rank_ic": S.ev_ok(0.08),
            "rank_ic_ci": S.ev_ok([0.017, 0.142]),
            "base_ic": S.ev_ok(0.02),
            "decision_edge": S.ev_ok(0.0615),
            "decision_edge_ci": S.ev_ok([-0.042, 0.169]),
            "brier": S.ev_ok(0.61),
            "brier_ci": S.ev_ok([0.58, 0.64]),
            "b_majority": S.ev_ok(0.63),
            S.MIDPOINT_CALIBRATION_ERROR_KEY: S.ev_ok(0.12),
        })

    def test_pooled_pass_fund_fail_three_states_coexist(self):
        ev = S.blank_evidence()
        self._full_pass_pooled(ev)
        # 025687（本地冻结件 n=35）：样本不足 → INSUFFICIENT_POWER，不是 0 分
        for k in ("n", "decision_edge", "decision_edge_ci"):
            ev["funds"]["025687"]["5"][k] = S.ev_na(S.STATUS_INSUFFICIENT_POWER)
        # 002112：edge 点估计为负且 CI 跨零（fund edge FAIL），rank_ic 缺输入不可算
        ev["funds"]["002112"]["5"]["decision_edge"] = S.ev_ok(-0.02)
        ev["funds"]["002112"]["5"]["decision_edge_ci"] = S.ev_ok([-0.09, 0.05])
        ev["funds"]["002112"]["5"]["rank_ic"] = S.ev_na(S.STATUS_NOT_COMPUTABLE)
        # 002207 保持模板 UNKNOWN——「还没算」也是一等公民

        ok, errs = S.validate_evidence(ev)
        self.assertTrue(ok)
        self.assertEqual(errs, [])
        statuses = {
            ev["pooled"]["5"]["decision_edge"]["status"],           # OK
            ev["funds"]["025687"]["5"]["decision_edge"]["status"],  # INSUFFICIENT_POWER
            ev["funds"]["002112"]["5"]["rank_ic"]["status"],        # NOT_COMPUTABLE
            ev["funds"]["002207"]["5"]["decision_edge"]["status"],  # UNKNOWN
        }
        self.assertEqual(statuses, {"OK", "UNKNOWN", "INSUFFICIENT_POWER",
                                    "NOT_COMPUTABLE"})
        # JSON 往返不坍缩（跨进程/落盘后状态逐位保留）
        ev2 = json.loads(json.dumps(ev))
        self.assertEqual(ev2, ev)
        ok2, errs2 = S.validate_evidence(ev2)
        self.assertTrue(ok2)
        self.assertEqual(errs2, [])

    def test_schema_never_derives_decision(self):
        """即便全部槽位 OK 且「看起来该过」，decision 仍须由 B++-3 裁决，
        schema 层既不推导也不拒绝 null——同形态证据只此一份事实源。"""
        ev = S.blank_evidence()
        self._full_pass_pooled(ev)
        ok, errs = S.validate_evidence(ev)
        self.assertTrue(ok)
        self.assertEqual(errs, [])
        self.assertIsNone(ev["decision"])


if __name__ == "__main__":
    unittest.main()
