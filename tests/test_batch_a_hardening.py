"""A 批加固测试（2026-09-24）：血统解耦 + load fail-closed + 覆写闸门。

对应 A 批验收条款：
- ⑤ 旧 artifact 能被不同版本的 verifier 读取（训练 commit ≠ 验证器 commit 不再拦截）
- ⑥ artifact 模式 provenance.git_commit = 训练血统；verifier 版本留痕 produced_by，
  不新增 provenance 键（schema v2 键集冻结，契约 B §10）
- ⑦ partial/畸形 artifact 在 load_models 完整性门 fail-closed
- A5 prereg 钉住的 artifact 禁止普通 save_models 静默覆盖（判定与接线护栏）
"""
import json
import pickle
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import backtest_forecast as B                        # noqa: E402
from core import forecast_engine                     # noqa: E402
from core import model_registry                      # noqa: E402
from core import validation_schema as S              # noqa: E402


class _StubDirModel:
    """带 predict_proba 的最小方向模型替身（完整性门只验可用性，不验精度）。"""

    def predict_proba(self, X):
        return [[0.2, 0.5, 0.3] for _ in X]


def _quant_payload():
    return {"reg_e": "reg", "reg_q": {0.10: "a", 0.50: "b", 0.90: "c"},
            "resid_std": 0.01}


# ---------- A3：payload 完整性门 ----------


class TestPayloadCompleteness(unittest.TestCase):
    def _payload(self, drop_dir_h=None, drop_quant_h=None, drop_quantile=None,
                 drop_resid=False, dead_dir=False, boom_dir=False):
        dirmodels = {h: _StubDirModel() for h in (1, 3, 5)}
        quantmodels = {h: _quant_payload() for h in (1, 3, 5)}
        if drop_dir_h is not None:
            dirmodels.pop(drop_dir_h)
        if drop_quant_h is not None:
            quantmodels.pop(drop_quant_h)
        if drop_quantile is not None:
            quantmodels[5]["reg_q"].pop(drop_quantile)
        if drop_resid:
            quantmodels[5]["resid_std"] = None
        if dead_dir:
            class _Dead:
                pass
            dirmodels[3] = _Dead()
        if boom_dir:
            class _Boom:
                def predict_proba(self, X):
                    raise RuntimeError("not fitted")
            dirmodels[3] = _Boom()
        return {"dirmodels": dirmodels, "quantmodels": quantmodels}

    def _err(self, **kw):
        return forecast_engine.payload_completeness_error(
            self._payload(**kw), [1, 3, 5], feature_dim=2 * len(forecast_engine.FEATURE_KEYS))

    def test_full_payload_passes(self):
        self.assertIsNone(self._err())

    def test_missing_dir_model_rejected(self):
        self.assertEqual(self._err(drop_dir_h=3), "incomplete_dir_model:T+3")

    def test_dead_dir_model_rejected(self):
        err = self._err(dead_dir=True)
        self.assertEqual(err, "incomplete_dir_model:T+3")

    def test_exploding_dir_model_rejected(self):
        self.assertEqual(self._err(boom_dir=True), "dir_model_unusable:T+3:RuntimeError")

    def test_missing_quant_model_rejected(self):
        self.assertEqual(self._err(drop_quant_h=5), "incomplete_quant_model:T+5")

    def test_missing_quantile_rejected(self):
        self.assertEqual(self._err(drop_quantile=0.50), "incomplete_quant_quantile:T+5:q50")

    def test_missing_resid_std_rejected(self):
        self.assertEqual(self._err(drop_resid=True), "incomplete_quant_resid:T+5")


# ---------- A2：provenance 训练血统 + verifier 留痕 ----------


class TestTrainingGitProvenance(unittest.TestCase):
    def test_artifact_mode_uses_training_git(self):
        ev = B.assemble_validation_evidence(
            {}, {}, {}, [1],
            {"mode": "FROZEN", "file": "samples_frozen_x.jsonl",
             "snapshot_provenance": {"snapshot_file": "samples_frozen_x.jsonl"}},
            overall_ok=False, git_head="b" * 40, produced_at="t",
            model_sha256="c" * 64, training_git="a" * 40)
        # ⑥ git_commit = 训练血统（非 verifier head）
        self.assertEqual(ev["provenance"]["git_commit"]["value"], "a" * 40)
        # verifier 版本留痕在 produced_by，不新增 provenance 键
        self.assertIn("verifier_git=" + "b" * 40,
                      ev["provenance"]["produced_by"]["value"])
        ok, errs = S.validate_evidence(ev)
        self.assertTrue(ok)
        self.assertEqual(errs, [])

    def test_fresh_mode_git_unchanged(self):
        """fresh 研究模式不传 training_git：git_commit 仍 = 本验证器 head（同源）。"""
        ev = B.assemble_validation_evidence(
            {}, {}, {}, [1], {"mode": "FRESH", "snapshot_provenance": {}},
            overall_ok=True, git_head="b" * 40, produced_at="t")
        self.assertEqual(ev["provenance"]["git_commit"]["value"], "b" * 40)
        ok, errs = S.validate_evidence(ev)
        self.assertTrue(ok)


# ---------- A1：旧 artifact 可被不同版本 verifier 复核 ----------


class TestArtifactVerifierDecoupling(unittest.TestCase):
    def setUp(self):
        model_registry.PKL_DIR.mkdir(parents=True, exist_ok=True)
        self._orig_registry = (
            model_registry.REGISTRY_PATH.read_text(encoding="utf-8")
            if model_registry.REGISTRY_PATH.exists() else None)
        self.pkl = model_registry.PKL_DIR / "_test_a1_artifact.pkl"
        cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
        fc = cfg.get("forecast", {})
        self.horizons = fc.get("horizons", [1, 3, 5])
        self.flat_margin = fc.get("prob_flat_margin", 0.003)
        self._cleanup()

    def tearDown(self):
        self._cleanup()
        if self._orig_registry is not None:
            model_registry.REGISTRY_PATH.write_text(self._orig_registry, encoding="utf-8")
        else:
            model_registry.REGISTRY_PATH.unlink(missing_ok=True)

    def _cleanup(self):
        self.pkl.unlink(missing_ok=True)
        reg = model_registry.load_registry()
        reg["models"].pop(self.pkl.name, None)
        model_registry._save_registry(reg)

    def _register_with_lineage(self, train_commit):
        payload = {
            "model_version": forecast_engine.MODEL_VERSION,
            "feature_keys": list(forecast_engine.FEATURE_KEYS),
            "horizons": list(self.horizons),
            "flat_margin": self.flat_margin,
            "dirmodels": {h: _StubDirModel() for h in self.horizons},
        }
        self.pkl.write_bytes(pickle.dumps(payload))
        with mock.patch("core.model_registry.capture_provenance",
                        return_value={"data_manifest_sha256": "test-manifest",
                                      "git_commit": train_commit}):
            digest = model_registry.register_model(
                self.pkl, meta={},
                snapshot_provenance={"snapshot_file": "samples_frozen_x.jsonl",
                                     "samples_sha256_lf": "a" * 64,
                                     "kfp_recorded_sha256": "b" * 64,
                                     "kfp_current_sha256": "b" * 64,
                                     "kfp_comparability": "SAME"})
        self.assertIsNotNone(digest)
        model_registry.bind_feature_protocol(
            self.pkl.name,
            model_registry.make_feature_protocol(
                forecast_engine.FEATURE_KEYS,
                masking=forecast_engine.MASKING_PROTOCOL))

    def test_old_artifact_verifiable_by_newer_verifier(self):
        """⑤ 训练 commit ≠ 验证器 commit：不再拦截，血统与 verifier 分别留痕。"""
        self._register_with_lineage("d" * 40)          # 训练血统=旧 commit
        with mock.patch("backtest_forecast.freeze_verify_tool.git_commit",
                        return_value="e" * 40):         # 验证器=更新的 commit
            ctx, err = B.load_persisted_direction_artifact(
                str(self.pkl), self.horizons, self.flat_margin)
        self.assertIsNone(err)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["git_commit"], "d" * 40)
        self.assertEqual(ctx["verifier_git_commit"], "e" * 40)

    def test_incomplete_lineage_still_rejected(self):
        """血统不完整（registry 缺训练 commit）仍 fail-closed——A1 只解耦，不放松。"""
        self._register_with_lineage("d" * 40)
        reg = model_registry.load_registry()
        reg["models"][self.pkl.name]["git_commit"] = ""
        model_registry._save_registry(reg)
        ctx, err = B.load_persisted_direction_artifact(
            str(self.pkl), self.horizons, self.flat_margin)
        self.assertIsNone(ctx)
        self.assertEqual(err, "registry git_commit 缺失")


# ---------- A5：prereg 覆写闸门 ----------


class TestPreregPinGate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.prereg = Path(self._tmp.name) / "prereg.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, doc):
        self.prereg.write_text(json.dumps(doc), encoding="utf-8")

    def test_pinned_name_returns_sha(self):
        self._write({"enabled": True,
                     "grants": {"forecast_v3.pkl": {"model_sha256": "ab" * 32}}})
        self.assertEqual(
            model_registry.prereg_pinned_sha256("forecast_v3.pkl", self.prereg),
            "ab" * 32)

    def test_pinned_even_when_disabled(self):
        """钉住的是 artifact 身份——降级授权停用/expired 不解除覆盖保护。"""
        self._write({"enabled": False,
                     "grants": {"forecast_v3.pkl": {"model_sha256": "ab" * 32}}})
        self.assertEqual(
            model_registry.prereg_pinned_sha256("forecast_v3.pkl", self.prereg),
            "ab" * 32)

    def test_absent_name_returns_none(self):
        self._write({"enabled": True,
                     "grants": {"forecast_v9.pkl": {"model_sha256": "ab" * 32}}})
        self.assertIsNone(
            model_registry.prereg_pinned_sha256("forecast_v3.pkl", self.prereg))

    def test_missing_file_returns_none(self):
        self.assertIsNone(
            model_registry.prereg_pinned_sha256("forecast_v3.pkl",
                                                Path(self._tmp.name) / "nope.json"))

    def test_malformed_grant_pinned_without_sha(self):
        self._write({"enabled": True, "grants": {"forecast_v3.pkl": {}}})
        self.assertEqual(
            model_registry.prereg_pinned_sha256("forecast_v3.pkl", self.prereg),
            "pinned_without_sha")

    def test_save_models_wiring(self):
        """接线护栏：save_models 必须经 prereg 钉住闸门（缺失即测试红，防静默断线）。"""
        src = (BASE_DIR / "core" / "forecast_engine.py").read_text(encoding="utf-8")
        self.assertIn("prereg_pinned_sha256", src)
        self.assertIn("拒绝普通重训静默覆盖", src)


if __name__ == "__main__":
    unittest.main()
