"""B 契约 §15-B2（2026-09-23）：feature_protocol 逐字段 exact 校验。

旧实现只比 feature_keys / feature_dim / masking.enabled——布局翻转
（value_then_mask → mask_then_value）或填充值漂移在维度不变时漏检。
现 protocol_version / n_features / masking 整块（enabled+layout+
missing_value+mask_value）全部纳入 exact 比较。
"""
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import model_registry   # noqa: E402
from core.model_registry import B1_MASKING_PROTOCOL, make_feature_protocol   # noqa: E402

KEYS = ["est_chg", "est_sign", "breadth", "concentration", "covered_pct",
        "composite", "score"]


class TestFeatureProtocolStrictVerify(unittest.TestCase):
    def setUp(self):
        model_registry.MODELS_DIR.mkdir(parents=True, exist_ok=True)
        self.test_pkl = model_registry.MODELS_DIR / "_test_protocol_strict.pkl"
        self.test_pkl.write_bytes(b"protocol-strict-bytes")
        self._cleanup_registry()

    def _cleanup_registry(self):
        reg = model_registry.load_registry()
        reg["models"].pop(self.test_pkl.name, None)
        model_registry._save_registry(reg)

    def tearDown(self):
        self._cleanup_registry()
        self.test_pkl.unlink(missing_ok=True)

    def _bind(self, masking=B1_MASKING_PROTOCOL) -> dict:
        model_registry.register_model(self.test_pkl, meta={})
        proto = make_feature_protocol(KEYS, masking=masking)
        self.assertTrue(model_registry.bind_feature_protocol(self.test_pkl.name, proto))
        return proto

    @staticmethod
    def _expected(masking=B1_MASKING_PROTOCOL, **overrides) -> dict:
        proto = make_feature_protocol(KEYS, masking=masking)
        proto.update(overrides)
        return proto

    def test_exact_match_ok(self):
        self._bind()
        ok, reason = model_registry.verify_feature_protocol(
            self.test_pkl.name, self._expected())
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_plain_protocol_exact_match_ok(self):
        """v2 纯值路径（masking=None）回归：完全一致仍通过。"""
        self._bind(masking=None)
        ok, reason = model_registry.verify_feature_protocol(
            self.test_pkl.name, self._expected(masking=None))
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_layout_flip_rejected(self):
        self._bind()
        expected = self._expected()
        expected["masking"] = dict(expected["masking"], layout="mask_then_value")
        ok, reason = model_registry.verify_feature_protocol(self.test_pkl.name, expected)
        self.assertFalse(ok)
        self.assertEqual(reason, "masking_mismatch")

    def test_missing_value_drift_rejected(self):
        self._bind()
        expected = self._expected()
        expected["masking"] = dict(expected["masking"], missing_value=-1.0)
        ok, reason = model_registry.verify_feature_protocol(self.test_pkl.name, expected)
        self.assertFalse(ok)
        self.assertEqual(reason, "masking_mismatch")

    def test_mask_value_drift_rejected(self):
        self._bind()
        expected = self._expected()
        expected["masking"] = dict(expected["masking"], mask_value=2.0)
        ok, reason = model_registry.verify_feature_protocol(self.test_pkl.name, expected)
        self.assertFalse(ok)
        self.assertEqual(reason, "masking_mismatch")

    def test_masking_enabled_flip_rejected(self):
        """enabled 翻转（B1 → 纯值）在 feature_dim=14 伪装下也必须被拦。

        注：make_feature_protocol(masking=None) 会把 feature_dim 重算为 7，
        那样会先撞 feature_dim_mismatch。这里手工把维度伪装回 14，专测
        「维度不变、掩码语义漂移」这一真实攻击面 → 必须 masking_mismatch。
        """
        self._bind()
        expected = make_feature_protocol(KEYS, masking=None)
        expected["feature_dim"] = 14
        ok, reason = model_registry.verify_feature_protocol(self.test_pkl.name, expected)
        self.assertFalse(ok)
        self.assertEqual(reason, "masking_mismatch")

    def test_protocol_version_mismatch_rejected(self):
        self._bind()
        ok, reason = model_registry.verify_feature_protocol(
            self.test_pkl.name, self._expected(protocol_version=2))
        self.assertFalse(ok)
        self.assertEqual(reason, "protocol_version_mismatch")

    def test_n_features_mismatch_rejected(self):
        self._bind()
        ok, reason = model_registry.verify_feature_protocol(
            self.test_pkl.name, self._expected(n_features=6))
        self.assertFalse(ok)
        self.assertEqual(reason, "n_features_mismatch")

    def test_feature_dim_mismatch_rejected(self):
        self._bind()
        ok, reason = model_registry.verify_feature_protocol(
            self.test_pkl.name, self._expected(feature_dim=13))
        self.assertFalse(ok)
        self.assertEqual(reason, "feature_dim_mismatch")


if __name__ == "__main__":
    unittest.main()
