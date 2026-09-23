"""V4-A（2026-09-16）：run.py 运行清单落盘 + 三态退出码语义。

覆盖两件事：
1. `_write_run_manifest` 把逐环状态写成机器可读 JSON，且不污染真实 output/；
2. DEGRADED 判定与退出码映射的语义（降级 ⇒ 2，干净 ⇒ 0）。

不覆盖完整 `_run()` 链路（需网络与真实模型），那部分由每日实际运行留档验证。
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import run                                              # noqa: E402

POOL = ["002112", "002207", "022853", "025687"]      # V4.1 ④ 别名基准表（== config.fund_pool）


class TestRunManifestWrite(unittest.TestCase):
    def setUp(self):
        self._orig = run.BASE_DIR
        self._td = tempfile.TemporaryDirectory()
        run.BASE_DIR = Path(self._td.name)
        run._LOG.clear()

    def tearDown(self):
        run.BASE_DIR = self._orig
        run._LOG.clear()
        self._td.cleanup()

    def _written(self):
        files = sorted((Path(self._td.name) / "output" / "run_manifest").glob("*.json"))
        self.assertEqual(len(files), 1, f"应恰好落盘 1 份清单，实际 {files}")
        return files[0], json.loads(files[0].read_text(encoding="utf-8"))

    def test_success_manifest_roundtrip(self):
        now = datetime(2026, 9, 16, 18, 30, 0)
        run._write_run_manifest(now, {
            "slot": "post", "status": "SUCCESS", "degraded_reasons": [],
            "data": {"ok": True, "failed": [], "n_funds": 4},
            "lookthrough": {"ok": True, "missing": []},
            "notification": {"ok": True, "reason": None},
            "shadow": {"ok": True, "reason": None},
        }, POOL)
        path, payload = self._written()
        self.assertEqual(path.name, "run_manifest_20260916_183000_post.json")
        self.assertEqual(payload["run_id"], "20260916_183000_post")
        self.assertEqual(payload["status"], "SUCCESS")
        self.assertEqual(payload["degraded_reasons"], [])
        self.assertTrue(payload["shadow"]["ok"])
        # 逐环状态齐备（缺失即监控瞎眼）
        for key in ("data", "lookthrough", "notification", "shadow", "slot", "ts"):
            self.assertIn(key, payload)

    def test_degraded_manifest_keeps_reasons(self):
        run._write_run_manifest(datetime(2026, 9, 16, 14, 55, 0), {
            "slot": "post", "status": "DEGRADED",
            "degraded_reasons": ["feishu_push_failed", "shadow_failed:exit_1"],
            "notification": {"ok": False, "reason": "webhook_403"},
        }, POOL)
        _, payload = self._written()
        self.assertEqual(payload["status"], "DEGRADED")
        self.assertEqual(payload["degraded_reasons"],
                         ["feishu_push_failed", "shadow_failed:exit_1"])
        self.assertFalse(payload["notification"]["ok"])

    def test_write_failure_does_not_raise(self):
        # 落盘失败必须静默降级（不得让证据写入拖垮主流程）
        run.BASE_DIR = Path(self._td.name) / "nul" / ("x" * 300)
        try:
            run._write_run_manifest(datetime(2026, 9, 16, 18, 0, 0),
                                    {"slot": "mid", "status": "SUCCESS"}, POOL)
        finally:
            run.BASE_DIR = self._orig
        self.assertTrue(any("运行清单写入失败" in ln for ln in run._LOG))


class TestExitCodeSemantics(unittest.TestCase):
    """退出码契约：0=SUCCESS / 2=DEGRADED / 1=FAILED（源码级守护）。"""

    def test_labels_documented(self):
        src = (BASE_DIR / "run.py").read_text(encoding="utf-8")
        for marker in ("return 2", "return 1", "degraded_reasons"):
            self.assertIn(marker, src)
        # 无数据 ⇒ FAILED（1），降级 ⇒ 2，干净 ⇒ 0
        self.assertIn("return 1", src)
        self.assertIn("log(f\"[exit] DEGRADED", src)


class TestRealtimeDegradedContract(unittest.TestCase):
    """V4-A（2026-09-17）：实时行情部分缺位必须进入 DEGRADED。

    case A 请求异常（行情没拿到）/ case B est_change_pct=None（估值不可用）
    / case C 全部正常（不得误报）。用 stub 替代真实抓取，零网络可测。
    """

    def setUp(self):
        self._orig_fetch = run.realtime_mod.fetch_realtime
        self._orig_est = run.realtime_mod.weighted_estimate
        run._LOG.clear()

    def tearDown(self):
        run.realtime_mod.fetch_realtime = self._orig_fetch
        run.realtime_mod.weighted_estimate = self._orig_est
        run._LOG.clear()

    @staticmethod
    def _agg(code):
        return {"rows": [{"code": code}], "snapshot_date": "2026-06-30"}

    def _stub(self, behavior):
        """behavior: code -> ("ok", pct) | ("none", None) | ("raise", None)"""

        def fetch(rows):
            if behavior[rows[0]["code"]][0] == "raise":
                raise RuntimeError("network down")
            return {"quotes": {}}

        def est(rows, rt):
            return {"est_change_pct": behavior[rows[0]["code"]][1],
                    "covered_pct": 80.0}

        run.realtime_mod.fetch_realtime = fetch
        run.realtime_mod.weighted_estimate = est

    def test_case_a_fetch_exception_is_degraded(self):
        self._stub({"510300": ("raise", None), "159915": ("ok", 0.5)})
        rt, failures, degraded_funds = run._collect_realtime(
            {"510300": self._agg("510300"), "159915": self._agg("159915")})
        self.assertEqual(failures, ["510300"])
        self.assertEqual(degraded_funds, [])
        self.assertEqual(list(rt), ["159915"], "失败基金不得混入 realtime")
        self.assertEqual(run.realtime_degraded_reasons(failures, degraded_funds),
                         ["realtime_failed:510300"])

    def test_case_b_none_estimate_is_degraded(self):
        self._stub({"159915": ("none", None)})
        rt, failures, degraded_funds = run._collect_realtime({"159915": self._agg("159915")})
        self.assertEqual(rt, {})
        self.assertEqual(failures, [])
        self.assertEqual(degraded_funds, ["159915"])
        self.assertEqual(run.realtime_degraded_reasons(failures, degraded_funds),
                         ["realtime_degraded:159915"])

    def test_case_b_logs_warning(self):
        self._stub({"159915": ("none", None)})
        run._collect_realtime({"159915": self._agg("159915")})
        self.assertTrue(any("估值不可用" in ln for ln in run._LOG),
                        "est_change_pct=None 必须留下可判读的告警")

    def test_case_c_clean_run_no_false_degradation(self):
        self._stub({"510300": ("ok", 1.2), "159915": ("ok", -0.4)})
        rt, failures, degraded_funds = run._collect_realtime(
            {"510300": self._agg("510300"), "159915": self._agg("159915")})
        self.assertEqual(len(rt), 2)
        self.assertEqual(failures, [])
        self.assertEqual(degraded_funds, [])
        self.assertEqual(run.realtime_degraded_reasons(failures, degraded_funds), [],
                         "全部正常时不得产生降级项")

    def test_empty_lookthrough_is_not_a_failure(self):
        rt, failures, degraded_funds = run._collect_realtime(None)
        self.assertEqual((rt, failures, degraded_funds), ({}, [], []))


class TestManifestFailureContract(unittest.TestCase):
    """V4-A（2026-09-17）：清单写失败 ⇒ 不得声称「证据链完整」（exit=2）。"""

    def setUp(self):
        self._orig = run.BASE_DIR
        self._td = tempfile.TemporaryDirectory()
        run.BASE_DIR = Path(self._td.name)
        run._LOG.clear()

    def tearDown(self):
        run.BASE_DIR = self._orig
        run._LOG.clear()
        self._td.cleanup()

    def _out(self):
        return Path(self._td.name) / "output" / "run_manifest"

    def test_success_returns_true_and_leaves_no_tmp(self):
        ok = run._write_run_manifest(datetime(2026, 9, 17, 11, 30, 3),
                                     {"slot": "mid", "status": "SUCCESS"}, POOL)
        self.assertTrue(ok)
        self.assertEqual(list(self._out().glob("*.tmp")), [], "原子写不得残留 tmp")
        self.assertEqual(len(list(self._out().glob("*.json"))), 1)

    def test_failure_returns_false(self):
        run.BASE_DIR = Path(self._td.name) / "nul" / ("x" * 300)
        ok = run._write_run_manifest(datetime(2026, 9, 17, 11, 30, 3),
                                     {"slot": "mid", "status": "SUCCESS"}, POOL)
        self.assertFalse(ok, "落盘失败必须返回 False 供调用方降级")

    def test_finalize_maps_manifest_failure_to_exit_2(self):
        run.BASE_DIR = Path(self._td.name) / "nul" / ("x" * 300)
        degraded = []
        code = run._finalize_run(datetime(2026, 9, 17, 11, 30, 3),
                                 {"slot": "mid", "status": "SUCCESS"}, degraded, POOL)
        self.assertEqual(code, 2)
        self.assertIn("run_manifest_write_failed", degraded)

    def test_finalize_clean_run_is_exit_0(self):
        code = run._finalize_run(datetime(2026, 9, 17, 11, 30, 3),
                                 {"slot": "mid", "status": "SUCCESS"}, [], POOL)
        self.assertEqual(code, 0)


class TestFailedManifestFallback(unittest.TestCase):
    """V4.5 P0（2026-09-23）：异常必须留下 FAILED 清单，而非「无任何证据」。

    实况驱动：09-23 14:55 post 轮在报告生成前抛异常 ⇒ 无报告、无清单、审计指针
    不刷新，监控只看到「没有清单」。以下测例钉住三件事：兜底清单**存在**、**可被
    审计按三态词表识别**、且**不泄露基金代码**（走同一掩码路径）。
    """

    def setUp(self):
        self._orig = run.BASE_DIR
        self._td = tempfile.TemporaryDirectory()
        run.BASE_DIR = Path(self._td.name)
        run._LOG.clear()
        run._MANIFEST_WRITTEN = False
        # 基准表：真实代码只在内存，落盘必须变 F1/F2
        (Path(self._td.name) / "config.json").write_text(
            json.dumps({"fund_pool": POOL}), encoding="utf-8")

    def tearDown(self):
        run.BASE_DIR = self._orig
        run._MANIFEST_WRITTEN = False
        run._LOG.clear()
        self._td.cleanup()

    def _manifests(self):
        d = Path(self._td.name) / "output" / "run_manifest"
        return sorted(d.glob("*.json")) if d.is_dir() else []

    def test_exception_writes_failed_manifest_and_exit_1(self):
        code = run._finalize_failed(datetime(2026, 9, 23, 14, 55, 4), "post",
                                    "exception:TypeError", exc=TypeError("boom"))
        self.assertEqual(code, 1, "失败必须映射 exit=1")
        files = self._manifests()
        self.assertEqual(len(files), 1, "异常后必须恰好留一份兜底清单")
        self.assertEqual(files[0].name, "run_manifest_20260923_145504_post.json")
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "FAILED")
        self.assertEqual(payload["failure_reason"], "exception:TypeError")
        self.assertEqual(payload["failure_detail"], "TypeError")
        self.assertEqual(payload["slot"], "post")
        self.assertEqual(payload["run_id"], "20260923_145504_post")
        # 齐备性：审计 MANIFEST_REQUIRED_KEYS 全在，否则监控判「瞎眼」
        for key in ("run_id", "slot", "status", "data", "lookthrough",
                    "realtime", "degraded_reasons"):
            self.assertIn(key, payload)

    def test_failed_manifest_passes_audit_three_state_vocabulary(self):
        """兜底清单必须能被审计按三态词表**识别**（而不是判「非三态词表」）。"""
        import audit_project as ap
        run._finalize_failed(datetime(2026, 9, 23, 14, 55, 4), "post",
                             "exception:TypeError", exc=TypeError("boom"))
        orig = ap.RUN_MANIFEST_DIR
        ap.RUN_MANIFEST_DIR = Path(self._td.name) / "output" / "run_manifest"
        try:
            a = ap.Audit()
            ap.check_run_manifest(a, now=datetime(2026, 9, 23, 15, 0, 0))
        finally:
            ap.RUN_MANIFEST_DIR = orig
        self.assertEqual(a.checks[0].status, ap.FAIL, a.checks[0].detail)
        self.assertNotIn("三态词表", a.checks[0].detail)
        self.assertIn("FAILED", a.checks[0].detail)

    def test_exception_message_not_written_to_manifest(self):
        """异常原文（可能含真实代码）只进本地日志；清单只留类名。"""
        run._finalize_failed(datetime(2026, 9, 23, 14, 55, 4), "post",
                             "exception:ValueError",
                             exc=ValueError(f"bad fund {POOL[0]}"))
        text = self._manifests()[0].read_text(encoding="utf-8")
        for code in POOL:
            self.assertNotIn(code, text, "清单不得写真实基金代码（P0-2）")
        self.assertIn("ValueError", text)

    def test_second_call_does_not_overwrite_richer_evidence(self):
        """正常路径已落清单后若再抛异常，不得用 FAILED 覆盖那份更完整的证据。"""
        run._write_run_manifest(datetime(2026, 9, 23, 14, 55, 4),
                                {"slot": "post", "status": "SUCCESS",
                                 "degraded_reasons": []}, POOL)
        self.assertTrue(run._MANIFEST_WRITTEN)
        code = run._finalize_failed(datetime(2026, 9, 23, 14, 55, 4), "post",
                                    "exception:TypeError", exc=TypeError("x"))
        self.assertEqual(code, 1, "退出码仍为失败")
        files = self._manifests()
        self.assertEqual(len(files), 1, "不得写出第二份清单")
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "SUCCESS", "已落盘的证据不得被降级覆盖")

    def test_manifest_write_failure_still_returns_exit_1(self):
        """兜底清单自身也写不出时，不得抛异常、不得改判成功。"""
        run.BASE_DIR = Path(self._td.name) / "nul" / ("x" * 300)
        code = run._finalize_failed(datetime(2026, 9, 23, 14, 55, 4), "post",
                                    "exception:TypeError", exc=TypeError("x"))
        self.assertEqual(code, 1)
        self.assertTrue(any("兜底清单亦未落盘" in ln for ln in run._LOG), run._LOG)

    def test_stage_from_log_extracts_tag_without_fund_codes(self):
        run._LOG[:] = ["14:55:36 [stat] 002207 结构态 up|above~stale"]
        self.assertEqual(run._stage_from_log(), "[stat]")
        run._LOG[:] = []
        self.assertEqual(run._stage_from_log(), "unknown")

    def test_fund_pool_safe_survives_missing_config(self):
        (Path(self._td.name) / "config.json").unlink()
        self.assertEqual(run._fund_pool_safe(), [])


if __name__ == "__main__":
    unittest.main()
