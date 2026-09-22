"""P1-⑦：shadow_policy 纯函数测试（policy_action / jsonl 去重 / 追加 / 幂等）。

只测不依赖网络/模型的纯逻辑；完整链路由每日实际运行留档验证。
2026-09-13 D2 补：Shadow 双通道闸门 resolve_promotion_mode + 零网络样本源。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import shadow_policy                                   # noqa: E402
from shadow_policy import (CHANNELS, append_records, archive_legacy_records,  # noqa: E402
                           expert_a_shadow_block, load_existing_keys, load_expert_a,
                           load_frozen_samples, load_post_inputs,
                           load_records_by_channel, record_channel,
                           resolve_promotion_mode, policy_action, POLICY_THR)
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
                # V4.4 步 2：切换日（2026-09-23）前的旧行 est_chg 是百分数，
                # 读取端过唯一桥归一到契约 fraction（0.8% → 0.008）
                self.assertAlmostEqual(rows["A"]["features"]["est_chg"], 0.008)
                self.assertNotIn("fwd1", rows["A"]["features"])
            finally:
                feature_store.STORE_PATH = old_path

    def test_post_switch_rows_pass_through_unscaled(self):
        """切换日起落盘的行已是 fraction，读取端原样透传（不得二次 ÷100）。"""
        with tempfile.TemporaryDirectory() as td:
            old_path = feature_store.STORE_PATH
            feature_store.STORE_PATH = Path(td) / "intraday_features.jsonl"
            try:
                feature_store.append_features(
                    "2026-09-23", "post", "B", {"est_chg": 0.008}, model_version=3)
                d, rows = load_post_inputs("2026-09-23")
                self.assertEqual(d, "2026-09-23")
                self.assertAlmostEqual(rows["B"]["features"]["est_chg"], 0.008)
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


class _FakeEngine:
    """只带闸门用到的字段，不碰真实模型。"""

    def __init__(self, approved):
        self.model_approved = approved
        self.model_ready = False
        self.approval_error = "promotion_not_approved" if not approved else None


class TestPromotionGate(unittest.TestCase):
    """D2（2026-09-13 Summer 拍板）：完整批准 / 预注册降级 双通道。"""

    def setUp(self):
        self._orig_eval = shadow_policy.mr.evaluate_prereg_degradation

    def tearDown(self):
        shadow_policy.mr.evaluate_prereg_degradation = self._orig_eval

    def test_full_approval_short_circuits(self):
        # 完整批准时不得去读降级旁路（防止意外抬升未批准模型）
        def boom(*a, **k):
            raise AssertionError("完整批准路径不应调用降级评估")
        shadow_policy.mr.evaluate_prereg_degradation = boom
        mode, note = resolve_promotion_mode(_FakeEngine(True))
        self.assertEqual(mode, "approved_full")
        self.assertIn("完整批准", note)

    def test_degraded_when_prereg_passes(self):
        shadow_policy.mr.evaluate_prereg_degradation = lambda name: (True, "prereg_degraded")
        mode, note = resolve_promotion_mode(_FakeEngine(False))
        self.assertEqual(mode, "prereg_degraded")
        self.assertIn("仅纸面记录", note)

    def test_blocked_when_prereg_fails(self):
        shadow_policy.mr.evaluate_prereg_degradation = lambda name: (False, "prereg_expired")
        mode, note = resolve_promotion_mode(_FakeEngine(False))
        self.assertIsNone(mode)                 # 两通道都不过 → 调用方必拒记
        self.assertEqual(note, "prereg_expired")  # 原因透传，便于日志定位


class TestFrozenSampleSource(unittest.TestCase):
    """零网络硬纪律：路径 σ 只读冻结 JSONL，缺失即失败，不回退抓取。"""

    def test_reads_latest_frozen_file(self):
        rows, src = load_frozen_samples()
        self.assertIsNotNone(rows, f"冻结样本缺失：{src}")
        self.assertTrue(src.startswith("forecast_outputs/samples_frozen_"))
        self.assertGreater(len(rows), 1000)
        self.assertIn("fwd5", rows[0])

    def test_no_fallback_to_network_when_missing(self):
        orig = shadow_policy.BASE_DIR
        with tempfile.TemporaryDirectory() as td:
            shadow_policy.BASE_DIR = Path(td)
            try:
                rows, src = load_frozen_samples()
            finally:
                shadow_policy.BASE_DIR = orig
        self.assertIsNone(rows)
        self.assertEqual(src, "no_frozen_samples")


class TestExpertAShadowBlock(unittest.TestCase):
    """接法 ①（影子并列，2026-09-13 预注册 §三）：两臂都记/排名/分歧标注/失败不阻断。"""

    SCORES = {"ok": True, "artifact_sha256": "a" * 64, "samples_sha256": "b" * 64,
              "n_train": 3371, "horizon": 5, "n_pool": 2,
              "label_basis": "same_day_cross_section_excess",
              "usage_lock": "relative_pool_only_no_abs_display",
              "arms": ["a_mean", "a_med"],
              "scores": {"F1": {"a_mean": 0.02, "a_med": -0.01},
                         "F2": {"a_mean": -0.03, "a_med": 0.05}}}

    def test_ranks_and_lock_present(self):
        feats = {"F1": {"est_chg": 0.01}, "F2": {"est_chg": -0.01}}
        blk = expert_a_shadow_block("F1", self.SCORES, feats)
        self.assertEqual(blk["arms"]["a_mean"]["rank_in_pool"], 1)
        self.assertEqual(blk["arms"]["a_med"]["rank_in_pool"], 2)  # 两臂独立排名
        self.assertEqual(blk["usage_lock"], "relative_pool_only_no_abs_display")
        self.assertFalse(blk["contains_future_labels"])
        self.assertEqual(blk["role"], "shadow_only_not_executed")

    def test_divergence_flagged_not_reconciled(self):
        # A 看多 F1（a_mean>0）但镜像分看空（est_chg 低于池内均值）→ 标分歧
        # V4.4 步 2：post 特征 est_chg 现为 fraction；镜像分经逆向桥还原 % 域
        # 后喂 _score_relative。-0.01 fraction = -1.0% vs 池内其余 +3.0%
        feats = {"F1": {"est_chg": -0.01}, "F2": {"est_chg": 0.03}}
        blk = expert_a_shadow_block("F1", self.SCORES, feats)
        # F1：a_mean=+0.02（A 看多）；还原后 est=-1.0% vs 池内其余均值 +3.0%
        # → diff=-4.0（实时看空），预期精确判为 a_bullish_live_bearish
        self.assertEqual(blk["mirror"]["diff_vs_pool_mean"], -4.0)
        self.assertEqual(blk["divergence"], "a_bullish_live_bearish")
        # 不调和：A 分与镜像分各自保持原值，不得被均值/衰减改写
        self.assertEqual(blk["arms"]["a_mean"]["score"], 0.02)
        # mirror.est_chg 为还原后的百分数；fraction 原值保留在 est_chg_fraction
        self.assertEqual(blk["mirror"]["est_chg"], -1.0)
        self.assertEqual(blk["mirror"]["est_chg_fraction"], -0.01)

    def test_no_divergence_when_same_direction(self):
        # 同方向（A 看多 + 实时强势）→ none；不能误标分歧
        feats = {"F1": {"est_chg": 0.03}, "F2": {"est_chg": -0.01}}
        blk = expert_a_shadow_block("F1", self.SCORES, feats)
        self.assertEqual(blk["divergence"], "none")

    def test_mirror_none_passthrough(self):
        # est_chg 缺失 → 镜像分 error 块（est_chg_unavailable），不得抛异常
        feats = {"F1": {"est_chg": None}, "F2": {"est_chg": 0.03}}
        blk = expert_a_shadow_block("F1", self.SCORES, feats)
        self.assertEqual(blk["mirror"]["error"], "est_chg_unavailable")

    def test_scorer_error_degrades_to_field_not_exception(self):
        # 打分器不可用 → 块里只有 error，主记录流程不得抛异常
        blk = expert_a_shadow_block("F1", {"error": "lab_env_or_script_missing"}, {})
        self.assertEqual(blk["error"], "lab_env_or_script_missing")
        self.assertNotIn("arms", blk)

    def test_load_expert_a_missing_lab_returns_error(self):
        orig = shadow_policy.BASE_DIR
        with tempfile.TemporaryDirectory() as td:
            shadow_policy.BASE_DIR = Path(td)
            try:
                out = load_expert_a("2026-09-11")
            finally:
                shadow_policy.BASE_DIR = orig
        self.assertEqual(out, {"error": "lab_env_or_script_missing"})


class TestEvidenceChannels(unittest.TestCase):
    """V4-A（2026-09-16·P0-1 尾巴清扫）：三通道隔离——跨通道默认禁止聚合。"""

    @staticmethod
    def _legacy(fund="A", date="2026-08-05"):
        return {"date": date, "fund": fund, "status": "shadow",
                "evidence_validity": "legacy_invalid",
                "contract": {"model_version": 3}}

    @staticmethod
    def _live(fund="A", date="2026-09-14", mode="prereg_degraded"):
        return {"date": date, "fund": fund, "status": "shadow",
                "contract": {"model_version": 3, "promotion_mode": mode}}

    def test_channel_precedence_and_fallback(self):
        # legacy 标记优先级最高；无契约字段时才落 unclassified
        self.assertEqual(record_channel(self._legacy()), "legacy_invalid")
        self.assertEqual(record_channel(self._live(mode="approved_full")),
                         "approved_full")
        self.assertEqual(record_channel(self._live()), "prereg_degraded")
        self.assertEqual(record_channel({"date": "2026-09-14", "fund": "A"}),
                         "unclassified")
        # 老格式（有 future-label 但缺 legacy 标记）不得被误归为前瞻通道
        self.assertEqual(record_channel({"contract": {"model_version": 3},
                                         "features": {"fwd1": 0.1}}),
                         "unclassified")

    def test_load_buckets_never_merge_channels(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.jsonl"
            p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in
                                   [self._legacy(), self._legacy("B"),
                                    self._live(), self._live("B", mode="approved_full"),
                                    {"date": "2026-09-14", "fund": "C"}]) + "\n",
                         encoding="utf-8")
            b = load_records_by_channel(p)
            self.assertEqual(len(b["legacy_invalid"]), 2)
            self.assertEqual(len(b["prereg_degraded"]), 1)
            self.assertEqual(len(b["approved_full"]), 1)
            self.assertEqual(len(b["unclassified"]), 1)
            # 前瞻可聚合集合必须显式拼装，且不得漏入 legacy
            prospective = b["approved_full"] + b["prereg_degraded"]
            self.assertEqual(len(prospective), 2)
            self.assertFalse(any(r.get("evidence_validity") == "legacy_invalid"
                                 for r in prospective))

    def test_missing_file_returns_empty_buckets(self):
        b = load_records_by_channel(Path(tempfile.gettempdir()) / "no_such.jsonl")
        self.assertEqual(set(b), set(CHANNELS) | {"unclassified"})
        self.assertTrue(all(v == [] for v in b.values()))

    def test_archive_moves_legacy_out_of_active_stream(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.jsonl"
            arch = Path(td) / "legacy.jsonl"
            p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in
                                   [self._legacy(), self._live(), self._legacy("B")]) + "\n",
                         encoding="utf-8")
            n_arch, n_keep = archive_legacy_records(p, arch)
            self.assertEqual((n_arch, n_keep), (2, 1))
            self.assertEqual(len(arch.read_text(encoding="utf-8").splitlines()), 2)
            remaining = load_records_by_channel(p)
            self.assertEqual(len(remaining["legacy_invalid"]), 0)
            self.assertEqual(len(remaining["prereg_degraded"]), 1)
            # 幂等：再跑一次无 legacy 可搬，活跃流不变
            self.assertEqual(archive_legacy_records(p, arch), (0, 1))
            self.assertEqual(len(arch.read_text(encoding="utf-8").splitlines()), 2)

    def test_archive_missing_file_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(
                archive_legacy_records(Path(td) / "none.jsonl",
                                       Path(td) / "arch.jsonl"),
                (0, 0))


if __name__ == "__main__":
    unittest.main()
