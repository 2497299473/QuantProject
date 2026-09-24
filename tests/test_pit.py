"""PIT 样本口径 + 模型持久化闭环测试（2026-08-27 落地配套）。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import forecast_engine as fe
from core import intraday_store as snap_mod


class TestNavStateAt(unittest.TestCase):
    """_nav_state_at：特征只能用 i-1 及更早的已公布净值。"""

    def _navs(self, n=80):
        # 线性递增净值，方便手算 r20/dd
        return [(f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", 1.0 + i * 0.01) for i in range(n)]

    def test_no_same_day_nav_in_features(self):
        from backtest_spread import _nav_state_at
        navs = self._navs()
        st = _nav_state_at(75, navs, r20_win=20, dd_win=60)
        # r20 分母必须是 navs[75-1-20]=navs[54]，前收=navs[74]（绝不含当日 navs[75]）
        self.assertAlmostEqual(st["r20"], navs[74][1] / navs[54][1] - 1)
        # dd 窗口最高点含 navs[74] 自身、不含 navs[75]
        hh = max(v for _, v in navs[15:75])
        self.assertAlmostEqual(st["dd"], navs[74][1] / hh - 1)
        # macd 下标应是 i-1=74（由调用方替换成 hist_full[k]，此处只回传下标）
        self.assertEqual(st["macd"], 74)

    def test_insufficient_history_returns_none(self):
        from backtest_spread import _nav_state_at
        navs = self._navs(30)
        st = _nav_state_at(25, navs, r20_win=20, dd_win=60)   # k=24 < dd_win
        self.assertIsNone(st["r20"])
        self.assertIsNone(st["dd"])


class TestModelPersistence(unittest.TestCase):
    """save → load 往返一致性 + 版本拒绝。"""

    def _fake_samples(self, n=300):
        import random
        rng = random.Random(7)
        rows = []
        for i in range(n):
            est_chg = (i % 9) / 10.0 - 0.4
            fwd = est_chg * 2.0 + rng.uniform(-0.02, 0.02)
            rows.append({
                "fund": "TEST", "date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}",
                "est_chg": est_chg,
                "est_sign": 1 if est_chg > 0 else (-1 if est_chg < 0 else 0),
                "breadth": est_chg * 2, "concentration": 0.4,
                "covered_pct": 65.0, "composite": 1 if est_chg > 0 else -1,
                "score": 1 if est_chg > 0 else -1,
                **{f"fwd{h}": fwd * (1.0 + 0.3 * h) for h in (1, 3, 5)},
            })
        return rows

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            from core import model_registry as mr
            old_dir = fe.MODELS_DIR
            old_mr_dir = mr.MODELS_DIR
            old_reg_path = mr.REGISTRY_PATH
            old_prereg = mr.PROMOTION_PREREG_PATH
            fe.MODELS_DIR = Path(td)          # 隔离真实 data/models/
            # v8 修（2026-08-30）：registry 路径必须一并隔离——此前只隔离 MODELS_DIR，
            # save_models 自动登记时把 /tmp 临时 pkl 写进真实 registry.json 并覆盖原条目。
            mr.MODELS_DIR = Path(td)
            mr.REGISTRY_PATH = Path(td) / "registry.json"
            # A批 A5（2026-09-24）：save_models 有 prereg 覆写闸门——本测试用
            # forecast_v3.pkl 这个名字，必须一并隔离真实钉住清单，否则闸门按生产
            # 语义正确拒绝（本测试只验往返一致性；闸门行为由
            # tests/test_batch_a_hardening.py 覆盖）。指向不存在路径 ⇒ 无钉住记录。
            mr.PROMOTION_PREREG_PATH = Path(td) / "prereg_absent.json"
            try:
                e1 = fe.ForecastEngine()
                self.assertTrue(e1.fit(self._fake_samples()))
                p = e1.save_models()
                self.assertIsNotNone(p)
                e2 = fe.ForecastEngine()
                self.assertTrue(e2.load_models())
                self.assertIsInstance(e2.loaded_at, str)   # trained_at 已恢复
                # 往返一致性：同一输入，两个引擎预测应完全相同
                f1 = e1.predict({"est_chg": 0.02, "est_sign": 1, "breadth": 0.5,
                                 "concentration": 0.4, "covered_pct": 65,
                                 "composite": 1, "score": 1})
                f2 = e2.predict({"est_chg": 0.02, "est_sign": 1, "breadth": 0.5,
                                 "concentration": 0.4, "covered_pct": 65,
                                 "composite": 1, "score": 1})
                for a, b in ((f1.t1, f2.t1), (f1.t3, f2.t3), (f1.t5, f2.t5)):
                    self.assertAlmostEqual(a.p_up, b.p_up, places=9)
                    self.assertAlmostEqual(a.e_return, b.e_return, places=9)
                    self.assertAlmostEqual(a.q10, b.q10, places=9)
            finally:
                fe.MODELS_DIR = old_dir
                mr.MODELS_DIR = old_mr_dir
                mr.REGISTRY_PATH = old_reg_path
                mr.PROMOTION_PREREG_PATH = old_prereg

    def test_load_missing_file_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            old_dir = fe.MODELS_DIR
            fe.MODELS_DIR = Path(td)
            try:
                eng = fe.ForecastEngine()
                self.assertFalse(eng.load_models())
            finally:
                fe.MODELS_DIR = old_dir

    def test_hash_failure_prevents_pickle_deserialization(self):
        with tempfile.TemporaryDirectory() as td:
            from core import model_registry as mr
            old_dir = fe.MODELS_DIR
            old_mr_dir = mr.MODELS_DIR
            old_reg_path = mr.REGISTRY_PATH
            fe.MODELS_DIR = Path(td)
            mr.MODELS_DIR = Path(td)
            mr.REGISTRY_PATH = Path(td) / "registry.json"
            try:
                path = Path(td) / f"forecast_v{fe.MODEL_VERSION}.pkl"
                path.write_bytes(b"registered")
                mr.register_model(path, meta={})
                mr.bind_feature_protocol(path.name, mr.make_feature_protocol(
                    fe.FEATURE_KEYS, masking=fe.MASKING_PROTOCOL))
                path.write_bytes(b"tampered-not-a-pickle")
                eng = fe.ForecastEngine()
                with mock.patch.object(fe.pickle, "loads") as loads:
                    self.assertFalse(eng.load_models())
                    loads.assert_not_called()
                self.assertEqual(eng.load_error, "hash_mismatch")
            finally:
                fe.MODELS_DIR = old_dir
                mr.MODELS_DIR = old_mr_dir
                mr.REGISTRY_PATH = old_reg_path


if __name__ == "__main__":
    unittest.main()
