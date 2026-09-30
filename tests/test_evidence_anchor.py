# -*- coding: utf-8 -*-
"""D3-01 门 A（2026-09-29 面 3 审查）：report↔evidence **配对锚点** + 复算比对
判据的纯函数测例（零重依赖、零真实数据 → fast 层）。

A 单面 3 核心结论：bind 全链只验 Identity proof，Computation proof 为零——
report 与 evidence 是两个**无锚点**的独立文件，可各自伪造、任意搭配。本模块
钉住两道纯函数防线：
  ① 配对锚点（validation_schema.canonical_evidence_*）：report 的
     PROVENANCE_JSON 声明 evidence 内容哈希，与现场重算逐位比对；
  ② 复算比对判据（evidence_recompute.check_evidence_recompute）：evidence 声明值
     vs 复算值逐位比对（含 CI——门 2/门 3 的实际判据）。

真实冻结件 + artifact 的重算管线在 tests/test_evidence_recompute.py（slow 层）。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import evidence_recompute as erc          # noqa: E402
from core import validation_schema as S   # noqa: E402


def _node(n=923, rank_ic=0.05, ric_ci=(0.01, 0.09), base_ic=0.01,
          edge=0.04, edge_ci=(0.01, 0.07), brier=0.58, brier_ci=(0.55, 0.61),
          bmaj=0.60, mce=0.11) -> dict:
    return {
        "n": S.ev_ok(n), "rank_ic": S.ev_ok(rank_ic),
        "rank_ic_ci": S.ev_ok(list(ric_ci)), "base_ic": S.ev_ok(base_ic),
        "decision_edge": S.ev_ok(edge), "decision_edge_ci": S.ev_ok(list(edge_ci)),
        "brier": S.ev_ok(brier), "brier_ci": S.ev_ok(list(brier_ci)),
        "b_majority": S.ev_ok(bmaj), S.MIDPOINT_CALIBRATION_ERROR_KEY: S.ev_ok(mce),
    }


def _recomputed(n=923, rank_ic=0.05, ric_ci=(0.01, 0.09), base_ic=0.01,
                edge=0.04, edge_ci=(0.01, 0.07), brier=0.58,
                brier_ci=(0.55, 0.61), bmaj=0.60, mce=0.11) -> dict:
    return {"n": n, "rank_ic": rank_ic, "rank_ic_ci": list(ric_ci),
            "base_ic": base_ic, "decision_edge": edge,
            "decision_edge_ci": list(edge_ci), "brier": brier,
            "brier_ci": list(brier_ci), "b_majority": bmaj,
            S.MIDPOINT_CALIBRATION_ERROR_KEY: mce}


def _evidence(pooled: dict) -> dict:
    ev = S.blank_evidence()
    ev["decision"] = "approved"
    ev["pooled"] = pooled
    return ev


def _full_provenance_ev(rank_ic=0.05) -> dict:
    ev = _evidence({h: _node(rank_ic=rank_ic) for h in ("1", "3", "5")})
    for k in ev["provenance"]:
        ev["provenance"][k] = S.ev_ok(f"{k}-ok")
    ev["provenance"]["historical_feature_mode"] = S.ev_ok("PIT_1455_SNAPSHOT")
    ev["provenance"]["kfp_comparability"] = S.ev_ok("SAME")
    return ev


class TestCheckEvidenceRecompute(unittest.TestCase):
    """复算比对判据（纯函数，零重依赖）。"""

    def test_identical_values_pass_with_zero_error(self):
        rec = _recomputed()
        ev = _evidence({h: _node() for h in ("1", "3", "5")})
        ok, mis = erc.check_evidence_recompute(ev, {h: dict(rec) for h in ("1", "3", "5")})
        self.assertTrue(ok, mis)
        self.assertEqual(mis, [])

    def test_forged_rank_ic_rejected(self):
        ev = _evidence({h: _node() for h in ("1", "3", "5")})
        ev["pooled"]["1"]["rank_ic"] = S.ev_ok(0.99)
        ok, mis = erc.check_evidence_recompute(ev, {h: _recomputed() for h in ("1", "3", "5")})
        self.assertFalse(ok)
        self.assertTrue(any("rank_ic" in m for m in mis), mis)

    def test_forged_ci_lower_bound_rejected(self):
        """CI 下界是门 2/门 3 的**实际判据**（ric_ci[0]<=0 / edge_ci[0]<=0）——
        只复算点估计的话伪造者改 CI 即可过门，故 CI 必须一并复算比对。"""
        ev = _evidence({h: _node() for h in ("1", "3", "5")})
        ev["pooled"]["5"]["rank_ic_ci"] = S.ev_ok([0.5, 0.9])
        ok, mis = erc.check_evidence_recompute(ev, {h: _recomputed() for h in ("1", "3", "5")})
        self.assertFalse(ok)
        self.assertTrue(any("rank_ic_ci" in m for m in mis), mis)

    def test_forged_decision_edge_ci_rejected(self):
        ev = _evidence({h: _node() for h in ("1", "3", "5")})
        ev["pooled"]["3"]["decision_edge_ci"] = S.ev_ok([0.02, 0.08])
        ok, mis = erc.check_evidence_recompute(ev, {h: _recomputed() for h in ("1", "3", "5")})
        self.assertFalse(ok)
        self.assertTrue(any("decision_edge_ci" in m for m in mis), mis)

    def test_fabricated_value_where_recompute_unavailable_rejected(self):
        """复算不可得（None）而 evidence 声明 OK 带值 ⇒ 凭空捏造，必拒。"""
        rec = _recomputed()
        rec["rank_ic"] = None
        ev = _evidence({h: _node() for h in ("1", "3", "5")})
        ok, mis = erc.check_evidence_recompute(ev, {h: dict(rec) for h in ("1", "3", "5")})
        self.assertFalse(ok)
        self.assertTrue(any("rank_ic" in m for m in mis), mis)

    def test_fabricated_ci_where_recompute_unavailable_rejected(self):
        rec = _recomputed()
        rec["decision_edge_ci"] = None
        ev = _evidence({h: _node() for h in ("1", "3", "5")})
        ok, mis = erc.check_evidence_recompute(ev, {h: dict(rec) for h in ("1", "3", "5")})
        self.assertFalse(ok)
        self.assertTrue(any("CI 是门 2/门 3" in m for m in mis), mis)

    def test_declared_not_computable_but_recomputable_rejected(self):
        """反向：把算得出的说成算不出（掩盖真实数值）同样不符。"""
        ev = _evidence({h: _node() for h in ("1", "3", "5")})
        ev["pooled"]["1"]["brier"] = S.ev_na(S.STATUS_NOT_COMPUTABLE)
        ok, mis = erc.check_evidence_recompute(ev, {h: _recomputed() for h in ("1", "3", "5")})
        self.assertFalse(ok)
        self.assertTrue(any("brier" in m for m in mis), mis)

    def test_missing_horizon_node_rejected(self):
        ev = _evidence({"1": _node(), "3": _node()})
        ok, mis = erc.check_evidence_recompute(ev, {h: _recomputed() for h in ("1", "3", "5")})
        self.assertFalse(ok)
        self.assertTrue(any("T+5" in m for m in mis), mis)

    def test_pooled_missing_entirely_rejected(self):
        ok, mis = erc.check_evidence_recompute({"schema_version": 2}, {"1": _recomputed()})
        self.assertFalse(ok)
        self.assertIn("pooled", mis[0])

    def test_n_mismatch_rejected(self):
        ev = _evidence({h: _node(n=500) for h in ("1", "3", "5")})
        ok, mis = erc.check_evidence_recompute(ev, {h: _recomputed(n=923) for h in ("1", "3", "5")})
        self.assertFalse(ok)
        self.assertTrue(any(".n:" in m for m in mis), mis)

    def test_float_noise_beyond_round_tolerance_rejected(self):
        ev = _evidence({h: _node(rank_ic=0.0500001) for h in ("1", "3", "5")})
        ok, mis = erc.check_evidence_recompute(ev, {h: _recomputed(rank_ic=0.05) for h in ("1", "3", "5")})
        self.assertFalse(ok)
        self.assertTrue(any("rank_ic" in m for m in mis), mis)


class TestAnchorPairing(unittest.TestCase):
    """D3-01 门 A：report↔evidence 配对锚点纯函数。"""

    def test_canonical_bytes_are_stable_and_key_order_insensitive(self):
        ev = _full_provenance_ev()
        b1 = S.canonical_evidence_bytes(ev)
        shuffled = json.loads(json.dumps(ev, ensure_ascii=False))
        shuffled["provenance"] = {k: shuffled["provenance"][k]
                                  for k in reversed(list(shuffled["provenance"]))}
        self.assertEqual(b1, S.canonical_evidence_bytes(shuffled))
        self.assertEqual(S.canonical_evidence_sha256(ev),
                         S.canonical_evidence_sha256(shuffled))

    def test_anchor_survives_json_roundtrip(self):
        """落盘/读回/经 registry JSON 往返后锚点不变（否则合法绑定会假失配）。"""
        ev = _full_provenance_ev()
        h0 = S.canonical_evidence_sha256(ev)
        h1 = S.canonical_evidence_sha256(json.loads(json.dumps(ev, ensure_ascii=False)))
        wrapper = json.loads(json.dumps({"validation": {"evidence": ev}},
                                        ensure_ascii=False))
        h2 = S.canonical_evidence_sha256(wrapper["validation"]["evidence"])
        self.assertEqual(h0, h1)
        self.assertEqual(h0, h2)

    def test_produced_at_excluded_so_report_bytes_reproducible(self):
        """**刻意排除 produced_at**：否则 report 字节每次重跑都变 ⇒ report_sha256
        漂移 ⇒ promotion_prereg D3 判据（钉住 report_sha256）与 stdout
        「同命令逐字节一致」纪律同时被破。锚点须跨轮稳定。"""
        ev_a = _full_provenance_ev()
        ev_a["provenance"]["produced_at"] = S.ev_ok("2026-09-23T21:00:58")
        ev_b = json.loads(json.dumps(ev_a, ensure_ascii=False))
        ev_b["provenance"]["produced_at"] = S.ev_ok("2026-12-31T09:00:00")
        self.assertEqual(S.canonical_evidence_sha256(ev_a),
                         S.canonical_evidence_sha256(ev_b),
                         "produced_at 是易变元数据，不得进锚点")

    def test_any_metric_tamper_changes_anchor(self):
        base = S.canonical_evidence_sha256(_full_provenance_ev())
        for slot, bad in (("rank_ic", 0.99), ("brier", 0.01),
                          (S.MIDPOINT_CALIBRATION_ERROR_KEY, 0.0)):
            ev = _full_provenance_ev()
            ev["pooled"]["1"][slot] = S.ev_ok(bad)
            self.assertNotEqual(base, S.canonical_evidence_sha256(ev), slot)
        ev = _full_provenance_ev()
        ev["pooled"]["1"]["rank_ic_ci"] = S.ev_ok([0.5, 0.9])
        self.assertNotEqual(base, S.canonical_evidence_sha256(ev), "CI 篡改须改锚点")

    def test_gate_provenance_tamper_changes_anchor(self):
        """门 5 判据字段（kfp_comparability）被改 ⇒ 锚点必变（不得只锚数值）。"""
        base = S.canonical_evidence_sha256(_full_provenance_ev())
        ev = _full_provenance_ev()
        ev["provenance"]["kfp_comparability"] = S.ev_ok("DRIFTED")
        self.assertNotEqual(base, S.canonical_evidence_sha256(ev))

    def test_non_dict_raises_not_silent(self):
        for bad in (None, "str", 42, []):
            with self.assertRaises(ValueError):
                S.canonical_evidence_bytes(bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
