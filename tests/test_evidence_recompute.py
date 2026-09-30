# -*- coding: utf-8 -*-
"""D3-01 ②（2026-09-29 面 3 审查）：validation evidence **独立复算门**管线测例。

真实冻结件 samples_frozen_20260910.jsonl（3371 行，只读）+ 确定性 stub 模型跑通
复算管线（重依赖 scipy/sklearn + 真实 forecast_outputs/ → slow 层）。

纯函数比对判据与锚点在 tests/test_evidence_anchor.py（fast 层）。

真实 artifact（forecast_v3.pkl）端到端跑通证据**不入 CI**（加载 900KB pkl +
3 周期 × 999 次 cluster bootstrap ≈ 分钟级），落盘于
evidence/probes/d3_batch_selftest_20260929.log，供 A 独立核验。

stub 模型说明：predict_proba 按行序号产出确定性概率（不加载真实权重）——
本测例钉的是**管线与比对判据**（复算→组表→check→篡改必拒），不是模型数值本身；
真实 artifact 数值正确性由上方探针证据与 A 的独立复算负责。
"""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import evidence_recompute as erc          # noqa: E402
from core import validation_schema as S   # noqa: E402


class TestRecomputePipelineWithStub(unittest.TestCase):
    FROZEN = "samples_frozen_20260910.jsonl"
    HORIZONS = (1, 3, 5)
    FLAT = 0.003

    @classmethod
    def setUpClass(cls):
        import numpy as np
        cls.np = np
        cls.snap = BASE_DIR / "forecast_outputs" / cls.FROZEN
        if not cls.snap.is_file():
            raise unittest.SkipTest(f"真实冻结件不在档：{cls.snap}")
        cls.dataset_sha = hashlib.sha256(
            cls.snap.read_bytes().replace(b"\r\n", b"\n")).hexdigest()

    class _StubModel:
        """确定性 stub：按行序号产出三分类概率（可重复，无随机源）。"""

        def __init__(self, seed=0):
            self.seed = seed

        def predict_proba(self, X):
            import numpy as np
            n = X.shape[0]
            g = (np.arange(n) * 7 + self.seed) % 100 / 100.0
            p_up = 0.2 + 0.6 * g
            p_flat = 0.1 + 0.1 * (1 - g)
            p_down = np.clip(1.0 - p_up - p_flat, 1e-6, None)
            tot = p_up + p_flat + p_down
            return np.column_stack([p_down / tot, p_flat / tot, p_up / tot])

    def _models(self):
        return {str(h): self._StubModel(seed=h) for h in self.HORIZONS}

    def _evidence_from(self, recomputed):
        ev = S.blank_evidence()
        ev["decision"] = "approved"
        for hs, rec in recomputed.items():
            node = ev["pooled"][hs]
            for slot in erc.SCALAR_SLOTS + erc.CI_SLOTS:
                v = rec.get(slot)
                node[slot] = (S.ev_ok(v) if v is not None
                              else S.ev_na(S.STATUS_NOT_COMPUTABLE))
        return ev

    def test_pipeline_roundtrip_zero_error(self):
        """验收「独立复算门对一份真实 frozen 件跑通（误差 0）」：
        复算 → 用复算真值组诚实 evidence → check 必过。"""
        samples, err = erc.load_frozen_samples(self.dataset_sha, self.FROZEN, BASE_DIR)
        self.assertIsNotNone(samples, err)
        self.assertEqual(len(samples), 3371)
        recomputed = erc.recompute_pooled(samples, self._models(),
                                          self.HORIZONS, self.FLAT)
        self.assertEqual(sorted(recomputed), ["1", "3", "5"])
        # 同一输入复算两次必须逐位相同（纯确定性，防随机种子泄漏）
        again = erc.recompute_pooled(samples, self._models(),
                                     self.HORIZONS, self.FLAT)
        self.assertEqual(recomputed, again)
        ok, mis = erc.check_evidence_recompute(self._evidence_from(recomputed),
                                               recomputed)
        self.assertTrue(ok, mis)

    def test_tampered_metrics_rejected(self):
        """端到端：复算真值 → 篡改任一槽位 → check 必拒（含 CI 槽位）。"""
        samples, _ = erc.load_frozen_samples(self.dataset_sha, self.FROZEN, BASE_DIR)
        recomputed = erc.recompute_pooled(samples, self._models(),
                                          self.HORIZONS, self.FLAT)
        for slot in ("rank_ic", "decision_edge", "brier", "rank_ic_ci",
                     "decision_edge_ci", S.MIDPOINT_CALIBRATION_ERROR_KEY):
            with self.subTest(slot=slot):
                ev = self._evidence_from(recomputed)
                ev["pooled"]["1"][slot] = (
                    S.ev_ok([-0.987, -0.5]) if slot in erc.CI_SLOTS
                    else S.ev_ok(-0.987))
                ok, mis = erc.check_evidence_recompute(ev, recomputed)
                self.assertFalse(ok, f"{slot} 篡改未被抓")
                self.assertTrue(any(slot in m for m in mis), mis)

    def test_wrong_dataset_sha_rejected(self):
        """evidence 声称的 dataset_sha256 与冻结件实算不符 ⇒ 拒（复算的不是同一份数据）。"""
        samples, err = erc.load_frozen_samples("f" * 64, self.FROZEN, BASE_DIR)
        self.assertIsNone(samples)
        self.assertIn("不符", err)

    def test_missing_frozen_file_rejected(self):
        samples, err = erc.load_frozen_samples(self.dataset_sha,
                                               "samples_frozen_19990101.jsonl",
                                               BASE_DIR)
        self.assertIsNone(samples)
        self.assertIn("不存在", err)

    def test_missing_artifact_sha_rejected(self):
        models, err = erc.load_artifact_models("", self.HORIZONS, self.FLAT, BASE_DIR)
        self.assertIsNone(models)
        self.assertIn("artifact_sha256", err)

    def test_unknown_artifact_sha_rejected(self):
        models, err = erc.load_artifact_models("0" * 64, self.HORIZONS, self.FLAT, BASE_DIR)
        self.assertIsNone(models)
        self.assertIn("找不到", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
