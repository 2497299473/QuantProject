"""B++-2（2026-09-23）：backtest_forecast 产出 validation evidence schema v2
的纯函数层 contract tests。

合成数据、零网络、不跑 main()（不触发真实训练/数据加载）。
钉死四条：
1) 不足/不可算不落 0 分：n=0/恒值 → NOT_COMPUTABLE，0<n<30 → INSUFFICIENT_POWER；
2) fund×horizon 与 pooled 共用同一构建器 evidence_node_from_rows，数值可复算；
3) pooled_node_from_results 与 stdout 报告字典逐位同源（CI None → NOT_COMPUTABLE）；
4) assemble_validation_evidence 组表通过 validate_evidence（fail-closed 自检），
   decision 只映射验证器 overall_ok，power.frozen=OK(False)，provenance 透传。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from backtest_forecast import (           # noqa: E402
    assemble_validation_evidence, evidence_node_from_rows,
    insufficient_power_node, pooled_node_from_results, rank_ic)
from core import validation_schema as S   # noqa: E402


def _rows(n: int, seed: int = 7):
    rng = np.random.default_rng(seed)
    score = rng.uniform(0.0, 1.0, n)
    base = rng.uniform(-0.02, 0.02, n)
    yr = rng.uniform(-0.03, 0.03, n)
    dates = [f"2026-01-{i:02d}" for i in range(1, n + 1)]
    return score, base, yr, dates


def _p_train() -> np.ndarray:
    return np.array([0.3, 0.3, 0.4])


def _pooled_results() -> dict:
    """模拟 results[h] 常规分支（stdout 报告字典，已 round）。"""
    return {"ok": True, "cv_brier": 0.6, "oos_brier": 0.58, "rank_ic": 0.05,
            "ric_ci": [0.01, 0.09], "brier_ci": [0.55, 0.61], "base_ic": 0.01,
            "decision_edge": {"edge": 0.04, "edge_ci": [-0.02, 0.1]},
            "b_majority": 0.6, "ace": 0.11, "n_train": 800, "n_oos": 923}


class TestInsufficientPowerNode(unittest.TestCase):
    def test_zero_rows_not_computable(self):
        node = insufficient_power_node(0)
        self.assertEqual(node["n"], {"value": 0, "status": "OK"})
        for k in node:
            if k != "n":
                self.assertEqual(node[k]["status"], S.STATUS_NOT_COMPUTABLE)
                self.assertIsNone(node[k]["value"])

    def test_small_n_insufficient_power(self):
        node = insufficient_power_node(29)
        st = {node[k]["status"] for k in node if k != "n"}
        self.assertEqual(st, {S.STATUS_INSUFFICIENT_POWER})

    def test_node_is_schema_valid(self):
        ev = S.blank_evidence()
        ev["pooled"]["5"] = insufficient_power_node(12)
        ev["funds"]["025687"]["5"] = insufficient_power_node(7)
        ok, errs = S.validate_evidence(ev)
        self.assertTrue(ok)
        self.assertEqual(errs, [])


class TestEvidenceNodeFromRows(unittest.TestCase):
    def test_full_ok_node_recomputable(self):
        score, base, yr, dates = _rows(40)
        po = np.zeros((40, 3))
        po[:, 2] = score
        yo = np.array([2 if v > 0 else 0 for v in yr])
        node = evidence_node_from_rows(score, base, yr, dates, _p_train(), po, yo)
        self.assertEqual(node["n"], {"value": 40, "status": "OK"})
        self.assertEqual(node["rank_ic"]["status"], S.STATUS_OK)
        self.assertAlmostEqual(
            node["rank_ic"]["value"], round(rank_ic(score.tolist(), yr.tolist()), 3))
        self.assertAlmostEqual(
            node["decision_edge"]["value"],
            round(rank_ic(score.tolist(), yr.tolist()) - rank_ic(base.tolist(), yr.tolist()), 3))
        self.assertEqual(node["rank_ic_ci"]["status"], S.STATUS_OK)
        self.assertEqual(node["brier"]["status"], S.STATUS_OK)
        self.assertEqual(node["b_majority"]["status"], S.STATUS_OK)
        self.assertEqual(node[S.MIDPOINT_CALIBRATION_ERROR_KEY]["status"], S.STATUS_OK)
        self.assertNotIn("ace", node)   # 旧名禁入
        ev = S.blank_evidence()
        ev["pooled"]["1"] = node
        ok, errs = S.validate_evidence(ev)
        self.assertTrue(ok)
        self.assertEqual(errs, [])

    def test_constant_target_not_zero(self):
        """恒值目标：秩相关族 NOT_COMPUTABLE（value=null），不得伪装 0.0。"""
        score, base, yr, dates = _rows(40)
        node = evidence_node_from_rows(score, base, np.full(40, 0.01), dates, _p_train())
        self.assertEqual(node["rank_ic"]["status"], S.STATUS_NOT_COMPUTABLE)
        self.assertEqual(node["decision_edge"]["status"], S.STATUS_NOT_COMPUTABLE)
        self.assertIsNone(node["rank_ic"]["value"])

    def test_ci_needs_ten_days(self):
        score, base, yr, _ = _rows(40)
        dates8 = [f"2026-day{i // 5}" for i in range(40)]   # 仅 8 个不同交易日
        node = evidence_node_from_rows(score, base, yr, dates8, _p_train())
        self.assertEqual(node["rank_ic"]["status"], S.STATUS_OK)
        self.assertEqual(node["rank_ic_ci"]["status"], S.STATUS_NOT_COMPUTABLE)

    def test_small_slice_insufficient_power(self):
        score, base, yr, dates = _rows(20)
        node = evidence_node_from_rows(score, base, yr, dates, _p_train())
        st = {node[k]["status"] for k in node if k != "n"}
        self.assertEqual(st, {S.STATUS_INSUFFICIENT_POWER})


class TestPooledNodeFromResults(unittest.TestCase):
    def test_same_source_as_stdout_dict(self):
        node = pooled_node_from_results(_pooled_results())
        self.assertEqual(node["n"]["value"], 923)
        self.assertEqual(node["rank_ic"]["value"], 0.05)
        self.assertEqual(node["rank_ic_ci"]["value"], [0.01, 0.09])
        self.assertEqual(node["decision_edge"]["value"], 0.04)
        self.assertEqual(node["decision_edge_ci"]["value"], [-0.02, 0.1])
        self.assertEqual(node["brier"]["value"], 0.58)
        self.assertEqual(node[S.MIDPOINT_CALIBRATION_ERROR_KEY]["value"], 0.11)
        ev = S.blank_evidence()
        ev["pooled"]["3"] = node
        ok, errs = S.validate_evidence(ev)
        self.assertTrue(ok)
        self.assertEqual(errs, [])

    def test_missing_ci_is_not_zero(self):
        r = _pooled_results()
        r["ric_ci"] = None
        r["brier_ci"] = None
        node = pooled_node_from_results(r)
        self.assertEqual(node["rank_ic_ci"]["status"], S.STATUS_NOT_COMPUTABLE)
        self.assertEqual(node["brier_ci"]["status"], S.STATUS_NOT_COMPUTABLE)
        self.assertIsNone(node["rank_ic_ci"]["value"])

    def test_reason_branch_delegates(self):
        node = pooled_node_from_results(
            {"ok": False, "reason": "insufficient_samples", "n_oos": 12})
        st = {node[k]["status"] for k in node if k != "n"}
        self.assertEqual(st, {S.STATUS_INSUFFICIENT_POWER})
        self.assertEqual(node["n"]["value"], 12)


class TestAssembleValidationEvidence(unittest.TestCase):
    def _snap_info(self) -> dict:
        return {"mode": "FROZEN", "file": "samples_frozen_20260910.jsonl",
                "snapshot_provenance": {
                    "snapshot_file": "samples_frozen_20260910.jsonl",
                    "samples_sha256_lf": "a" * 64,
                    "kfp_recorded_sha256": None, "kfp_current_sha256": None,
                    "kfp_comparability": "SAME"}}

    def test_full_assembly_validates_and_maps(self):
        score, base, yr, dates = _rows(40)
        po = np.zeros((40, 3))
        po[:, 2] = score
        yo = np.array([2 if v > 0 else 0 for v in yr])
        fund_nodes = {code: evidence_node_from_rows(
            score, base, yr, dates, _p_train(), po, yo)
            for code in S.PRODUCTION_FUNDS}
        ev = assemble_validation_evidence(
            {"1": _pooled_results()},
            {"3": insufficient_power_node(5)},
            {"1": fund_nodes,
             "3": {c: insufficient_power_node(3) for c in S.PRODUCTION_FUNDS}},
            [1, 3, 5], self._snap_info(),
            overall_ok=False, git_head="b" * 40,
            produced_at="2026-09-23T23:59:00")
        ok, errs = S.validate_evidence(ev)
        self.assertTrue(ok)
        self.assertEqual(errs, [])
        self.assertEqual(ev["decision"], "rejected")
        self.assertEqual(ev["pooled"]["1"]["rank_ic"]["value"], 0.05)
        self.assertEqual(ev["pooled"]["3"]["rank_ic"]["status"], S.STATUS_INSUFFICIENT_POWER)
        # 配置未含的周期保持 UNKNOWN——「没跑」如实表达，不造 0
        self.assertEqual(ev["pooled"]["5"]["rank_ic"]["status"], S.STATUS_UNKNOWN)
        self.assertEqual(ev["funds"]["025687"]["1"]["rank_ic"]["status"], S.STATUS_OK)
        self.assertEqual(ev["power"]["frozen"], {"value": False, "status": "OK"})
        self.assertEqual(ev["provenance"]["dataset_sha256"]["value"], "a" * 64)
        self.assertEqual(ev["provenance"]["git_commit"]["value"], "b" * 40)
        self.assertEqual(ev["provenance"]["historical_feature_mode"]["value"], "EOD_PROXY")
        self.assertEqual(ev["protocol"]["version"], 1)
        self.assertEqual(ev["baseline"], {"name": "est_chg", "unit": "fraction"})

    def test_fresh_mode_sha_unknown(self):
        ev = assemble_validation_evidence(
            {"1": _pooled_results()}, {}, {}, [1],
            {"mode": "FRESH", "snapshot_provenance": {}},
            overall_ok=True, git_head=None, produced_at="t")
        ok, errs = S.validate_evidence(ev)
        self.assertTrue(ok)
        self.assertEqual(errs, [])
        self.assertEqual(ev["decision"], "approved")
        self.assertEqual(ev["provenance"]["frozen_dataset"]["value"], "FRESH")
        self.assertEqual(ev["provenance"]["dataset_sha256"]["status"], S.STATUS_UNKNOWN)
        self.assertEqual(ev["provenance"]["git_commit"]["status"], S.STATUS_UNKNOWN)

    def test_main_wiring_exists(self):
        """接线护栏：main() 必须组表 + 自检 + 落盘（缺失即测试红，防静默断线）。"""
        src = (BASE_DIR / "backtest_forecast.py").read_text(encoding="utf-8")
        self.assertIn("assemble_validation_evidence(", src)
        self.assertIn('"output" / "validation_evidence"', src)
        self.assertIn("validation_schema.validate_evidence(ev)", src)
        self.assertIn("freeze_verify_tool.git_commit()", src)


if __name__ == "__main__":
    unittest.main()
