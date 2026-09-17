"""V4-A（2026-09-17）：飞书发布资格门禁 publish_gate（方案 A 整批裁决）。

钉死五条契约（全程 tempdir，零网络，不碰真实 holdings.json / output/）：
1. 整批语义：任一只论域基金被污染 ⇒ ok=False，全部不推；
2. 防自锁：feishu_push_failed / notification 节是门禁的**输出**，永不作输入；
3. 归因诚实：清单带结构化 codes 时逐基金归因；缺结构化（旧清单）⇒ 污染全部，
   而非悄悄假定干净；
4. fail-closed：无当日证据 ⇒ 不推；--no-publish-gate 必记 publish_gate_bypassed；
5. 词汇漂移守护：GATE_FUND_KEYED 前缀必须仍出现在 run.py，防改名后静默漏网。
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import audit_project as ap                                 # noqa: E402
import run                                                 # noqa: E402

NOW = datetime(2026, 9, 17, 14, 56, 0)


class GateHarness(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.man_dir = self.root / "run_manifest"
        self.man_dir.mkdir()
        self._orig = (ap.RUN_MANIFEST_DIR, ap.HOLDINGS)
        ap.RUN_MANIFEST_DIR = self.man_dir
        ap.HOLDINGS = self.root / "holdings.json"

    def tearDown(self):
        ap.RUN_MANIFEST_DIR, ap.HOLDINGS = self._orig
        self._td.cleanup()

    def set_holdings(self, **shares):
        ap.HOLDINGS.write_text(
            json.dumps({"funds": {c: {"shares": s} for c, s in shares.items()}}),
            encoding="utf-8")

    def manifest(self, run_id, degraded=(), structured=None, notification=None):
        payload = {"run_id": run_id, "slot": "post",
                   "degraded_reasons": list(degraded),
                   "notification": notification or {"ok": True, "reason": None}}
        payload.update(structured or {})
        (self.man_dir / f"run_manifest_{run_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def gate(self, universe=(), extra=()):
        return ap.publish_gate(NOW, universe=universe, extra=extra)


class TestBatchSemantics(GateHarness):
    def setUp(self):
        super().setUp()
        self.set_holdings(**{"000001": 100.0, "512890": 200.0})

    def test_all_clean_is_ok(self):
        self.manifest("20260917_113000_mid", [], {"codes": ["000001", "512890"]})
        g = self.gate()
        self.assertTrue(g["ok"], g["detail"])
        self.assertEqual(sorted(g["eligible"]), ["000001", "512890"])
        self.assertEqual(g["contaminated"], {})

    def test_one_fund_down_blocks_the_whole_batch(self):
        self.manifest("20260917_113000_mid", ["fund_data_partial:512890"],
                      {"codes": ["000001", "512890"],
                       "data": {"failed": ["512890"]}})
        g = self.gate()
        self.assertFalse(g["ok"], "A 方案：一只不干净 ⇒ 整批不推")
        self.assertEqual(g["eligible"], ["000001"])
        self.assertEqual(g["contaminated"], {"512890": ["fund_data_partial:512890"]})

    def test_run_level_reason_contaminates_everyone(self):
        self.manifest("20260917_113000_mid", ["market_context_unavailable"],
                      {"codes": ["000001", "512890"]})
        g = self.gate()
        self.assertEqual(g["eligible"], [])
        self.assertEqual(g["run_level"], ["market_context_unavailable"])

    def test_unattributed_code_escalates_to_run_level(self):
        """后缀代码不在论域（漂移/拼错）⇒ 保守升运行级，不静默放行。"""
        self.manifest("20260917_113000_mid", ["fund_data_partial:999999"],
                      {"codes": ["000001", "512890"], "data": {"failed": []}})
        g = self.gate()
        self.assertEqual(g["run_level"], ["fund_data_partial:999999"])
        self.assertEqual(g["eligible"], [])

    def test_legacy_manifest_attributes_via_suffix_and_blocks_batch(self):
        """旧清单（09-16 格式）无结构化字段：后缀带码仍可归因到该基金；
        但整批裁决下任一只被污染 ⇒ 仍不推。"""
        self.manifest("20260917_113000_mid", ["fund_data_partial:512890"])
        g = self.gate()
        self.assertEqual(g["contaminated"]["512890"], ["fund_data_partial:512890"])
        self.assertFalse(g["ok"])

    def test_union_across_day_manifests(self):
        self.manifest("20260917_113000_mid", ["fund_data_partial:512890"],
                      {"codes": ["000001", "512890"],
                       "data": {"failed": ["512890"]}})
        self.manifest("20260917_145500_post", [], {"codes": ["000001"]})
        g = self.gate()
        self.assertFalse(g["ok"])
        self.assertIn("512890", g["contaminated"])


class TestSelfLockExclusions(GateHarness):
    """push_ok 是被门禁控制的对象——它坏了绝不能反过来锁死明天的门禁。"""

    def setUp(self):
        super().setUp()
        self.set_holdings(**{"000001": 100.0})

    def test_push_failed_yesterday_does_not_block_today(self):
        self.manifest("20260917_113000_mid", ["feishu_push_failed"],
                      {"codes": ["000001"]},
                      notification={"ok": False, "reason": "webhook_403"})
        g = self.gate()
        self.assertTrue(g["ok"], "推送失败记录被排除后，证据链干净 ⇒ 仍可推")
        self.assertNotIn("feishu_push_failed", g["run_level"])

    def test_exclusion_list_is_explicit_and_in_result(self):
        g = self.gate()
        self.assertIn("feishu_push_failed", g["excluded"])
        self.assertIn("manifest.notification", g["excluded"])


class TestEvidencePresence(GateHarness):
    def test_no_manifest_today_is_not_ok(self):
        self.set_holdings(**{"000001": 100.0})
        self.manifest("20260916_183000_post", [])       # 昨天的，不算今天证据
        g = self.gate()
        self.assertFalse(g["ok"])
        self.assertEqual(g["reason"], "no_evidence_today")

    def test_empty_universe_is_not_ok(self):
        """没有持仓 ⇒ 没有论域；fail-closed，门禁不该绿着。"""
        self.set_holdings()
        self.manifest("20260917_113000_mid", [], {"codes": []})
        g = self.gate()
        self.assertFalse(g["ok"])
        self.assertEqual(g["reason"], "empty_universe")

    def test_universe_comes_from_holdings_only(self):
        """论域 = shares>0 的持仓；清单里写了代码也不扩论域（防持仓泄露）。"""
        self.set_holdings(**{"000001": 100.0, "022853": 0})
        self.manifest("20260917_113000_mid", [], {"codes": ["000001", "022853"]})
        g = self.gate()
        self.assertEqual(g["universe"], ["000001"],
                         "shares=0 不得进论域；清单 codes 字段也不得扩论域")
        self.assertTrue(g["ok"], g["detail"])

    def test_in_memory_extra_counts_as_evidence(self):
        self.set_holdings(**{"000001": 100.0})
        g = self.gate(universe=("000001",),
                      extra=[(["lookthrough_missing:000001"],
                              {"codes": ["000001"],
                               "lookthrough": {"missing": ["000001"]}})])
        self.assertFalse(g["ok"], "本次内存证据即使无落盘清单也必须被裁决")
        self.assertEqual(g["n_runs"], 1)


class TestRunWiring(unittest.TestCase):
    """源码级守护（与 TestExitCodeSemantics 同风格）：接线与留痕不漂移。"""

    def setUp(self):
        self.src = (BASE_DIR / "run.py").read_text(encoding="utf-8")

    def test_push_is_gated_before_notify(self):
        self.assertIn('reason": "blocked_publish_gate', self.src)
        self.assertIn("publish_gate_blocked:", self.src)
        self.assertLess(self.src.index("blocked_publish_gate"),
                        self.src.index("notify.push_feishu"),
                        "门禁必须在真正推送之前判定")

    def test_bypass_is_logged_not_silent(self):
        self.assertIn("publish_gate_bypassed", self.src)
        self.assertIn("--no-publish-gate", self.src)

    def test_manifest_records_gate_reason(self):
        self.assertIn('"gate"', self.src, "push_status 应留 gate 字段供复盘")

    def test_blocked_gate_segment_is_count_only(self):
        """run_manifest 随仓库跟踪：拦截详情只能写计数，代码只进本地日志。"""
        seg = self.src[self.src.index('"reason": "blocked_publish_gate"'):]
        seg = seg[:seg.index("else:")]
        self.assertIn("n_contaminated", seg)
        self.assertIn("n_universe", seg)
        self.assertNotIn('sorted(gate["contaminated"])', seg,
                         "持仓代码不得写进通知节→继而进清单（P0-2）")

    def test_fund_keyed_vocabulary_has_not_drifted(self):
        for prefix in ap.GATE_FUND_KEYED:
            self.assertIn(f'"{prefix}:', self.src,
                          f"run.py 已不再产生 {prefix}，GATE_FUND_KEYED 需同步")
        self.assertIn("extend(realtime_degraded_reasons", self.src)
        for p in ("realtime_failed", "realtime_degraded"):
            self.assertIn(p, ap.GATE_FUND_KEYED)

    def test_excluded_vocabulary_still_exists(self):
        self.assertIn('"feishu_push_failed"', self.src)


class TestAuditIntegration(GateHarness):
    def test_check_publish_gate_is_warn_not_fail(self):
        """污染时 WARN：审计 FAIL 数继续只度量静态审计（防 audit→gate 自锁）。"""
        self.set_holdings(**{"000001": 100.0})
        self.manifest("20260917_113000_mid", ["market_context_unavailable"],
                      {"codes": ["000001"]})
        a = ap.Audit()
        ap.check_publish_gate(a)
        self.assertEqual(len(a.checks), 1)
        self.assertEqual(a.checks[0].status, ap.WARN)
        self.assertEqual(a.checks[0].cid, "P1-9")

    def test_clean_day_yields_pass(self):
        self.set_holdings(**{"000001": 100.0})
        self.manifest("20260917_113000_mid", [], {"codes": ["000001"]})
        a = ap.Audit()
        ap.check_publish_gate(a)
        self.assertEqual(a.checks[0].status, ap.PASS)


class TestAuditPrivacy(unittest.TestCase):
    """入库侧防泄：audit_current.json 进版本库，不得携带持仓代码。"""

    def setUp(self):
        self._orig = ap.HOLDINGS
        ap.HOLDINGS = Path(self._td) / "holdings.json" if hasattr(self, "_td") \
            else None

    def tearDown(self):
        ap.HOLDINGS = self._orig

    def test_held_codes_absent_from_persisted_gate(self):
        held = ["002112", "025687", "022853", "002207"]
        src = (BASE_DIR / "audit_project.py").read_text(encoding="utf-8")
        # 入库字段必须是计数/掩码，不得直接内嵌 eligible/contaminated 代码列表
        seg = src[src.index('def check_publish_gate'):]
        seg = seg[:seg.index('def ', 10)]
        self.assertIn('mask_fund_codes', seg)
        self.assertIn('"n_universe"', seg)
        self.assertNotIn('"eligible": g["eligible"]', seg,
                         "真实代码列表不得写入入库 payload")
        self.assertNotIn('"universe": g["universe"]', seg)
        # 反向自验：掩码函数确实产出别名
        alias = ap.mask_fund_codes(held)
        self.assertEqual(set(alias.values()), {"F1", "F2", "F3", "F4"})
        self.assertEqual(ap._mask_detail("002112←fund_data_partial:002112", alias),
                         "F1←fund_data_partial:F1")


if __name__ == "__main__":
    unittest.main()
