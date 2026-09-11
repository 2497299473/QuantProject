"""P1-⑦：shadow_policy 纯函数测试（policy_action / jsonl 去重 / 追加 / 幂等）。

只测不依赖网络/模型的纯逻辑；完整链路由每日实际运行留档验证。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from shadow_policy import (append_records, load_existing_keys, load_post_inputs,  # noqa: E402
                           policy_action, POLICY_THR)
from core import intraday_feature_store as feature_store  # noqa: E402


class TestPolicyAction(unittest.TestCase):
    def test_add_on_boundary(self):
        # 边界归 ADD（p_up ≥ θ，与 backtest_forecast_policy 一致）
        self.assertEqual(policy_action(POLICY_THR, 0.0), "ADD")

    def test_reduce_when_down_high(self):
        self.assertEqual(policy_action(0.4, 0.8), "REDUCE")

    def test_hold_default(self):
        self.assertEqual(policy_action(0.5, 0.3), "HOLD")

    def test_na_on_missing(self):
        self.assertEqual(policy_action(None, 0.5), "NA")

    def test_add_priority_over_reduce(self):
        # 双超阈值（罕见）→ ADD 优先（与 policy_actions 向量化赋值顺序一致）
        self.assertEqual(policy_action(0.7, 0.9), "ADD")


class TestPostInputs(unittest.TestCase):
    def test_latest_post_only_and_no_future_labels(self):
        with tempfile.TemporaryDirectory() as td:
            old_path = feature_store.STORE_PATH
            feature_store.STORE_PATH = Path(td) / "intraday_features.jsonl"
            try:
                base = {"est_chg": 0.1, "est_sign": 1, "breadth": 0.2,
                        "concentration": 0.3, "covered_pct": 70.0,
                        "composite": 1, "score": 1, "fwd1": 9.9}
                feature_store.append_features("2026-09-10", "post", "A", base,
                                              model_version=3)
                feature_store.append_features("2026-09-11", "mid", "A", base,
                                              model_version=3)
                latest = dict(base)
                latest["est_chg"] = 0.8
                feature_store.append_features("2026-09-11", "post", "A", latest,
                                              model_version=3)
                d, rows = load_post_inputs()
                self.assertEqual(d, "2026-09-11")
                self.assertEqual(rows["A"]["slot"], "post")
                self.assertEqual(rows["A"]["features"]["est_chg"], 0.8)
                self.assertNotIn("fwd1", rows["A"]["features"])
            finally:
                feature_store.STORE_PATH = old_path

    def test_model_version_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            old_path = feature_store.STORE_PATH
            feature_store.STORE_PATH = Path(td) / "intraday_features.jsonl"
            try:
                feature_store.append_features("2026-09-11", "post", "A",
                                              {"est_chg": 0.1}, model_version=999)
                d, rows = load_post_inputs("2026-09-11")
                self.assertEqual(d, "2026-09-11")
                self.assertEqual(rows, {})
            finally:
                feature_store.STORE_PATH = old_path


class TestJsonlRoundtrip(unittest.TestCase):
    def _rec(self, date, fund):
        return {"date": date, "fund": fund, "status": "shadow"}

    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(load_existing_keys(Path(td) / "nope.jsonl"), set())

    def test_dedup_and_append(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.jsonl"
            append_records(p, [self._rec("2026-09-01", "002112"),
                               self._rec("2026-09-01", "025687")])
            keys = load_existing_keys(p)
            self.assertEqual(keys, {("2026-09-01", "002112"),
                                    ("2026-09-01", "025687")})
            # 同日重算：两条全部去重
            recomputed = [self._rec("2026-09-01", "002112"),
                          self._rec("2026-09-01", "025687")]
            new = [r for r in recomputed if (r["date"], r["fund"]) not in keys]
            self.assertEqual(new, [])
            # 新日期一条 + 旧日期重复一条 → 只追加 1 条
            mixed = [self._rec("2026-09-02", "002112"),
                     self._rec("2026-09-01", "002112")]
            new2 = [r for r in mixed if (r["date"], r["fund"]) not in keys]
            self.assertEqual(len(new2), 1)
            append_records(p, new2)
            lines = p.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            self.assertEqual(json.loads(lines[2])["date"], "2026-09-02")
            self.assertEqual(len(load_existing_keys(p)), 3)

    def test_bad_lines_tolerated(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.jsonl"
            p.write_text("{not json}\n" +
                         json.dumps({"date": "2026-09-01", "fund": "A"}) + "\n",
                         encoding="utf-8")
            self.assertEqual(load_existing_keys(p), {("2026-09-01", "A")})


if __name__ == "__main__":
    unittest.main()
