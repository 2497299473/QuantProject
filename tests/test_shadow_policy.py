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

from shadow_policy import (append_records, load_existing_keys, policy_action,  # noqa: E402
                           POLICY_THR)


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
