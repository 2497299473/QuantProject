"""V4.1 ④（2026-09-18）：运行清单的逐基金代码 → 位置别名掩码。

契约三问：

1. ``output/run_manifest/`` **随仓库跟踪**，而逐基金降级码 ⊆ ``config.fund_pool``；
   ``run.py`` 的注释写着「清单不得写代码」，旧实现却把真实 6 位码写进
   ``data.failed`` / ``data.fallback`` —— 契约与产物自相矛盾（P0-2 持仓隔离）。
2. 别名基准 = **基金池升序位**：同一只基金在所有字段、所有清单里都是同一个 F 号，
   门禁按同一张表反查（``resolve_fund_refs``）⇒ **逐基金归因能力不得退化**。
3. 掩码后仍残留 6 位码 ⇒ 拒绝落盘（run.py 转 DEGRADED，exit=2）：宁可少一份证据，
   也不写出一份泄露持仓的清单。

全程 tempdir + 内存结构，零网络，不碰真实 data/ 与 output/。
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import audit_project as ap                               # noqa: E402
import run                                               # noqa: E402

NOW = datetime(2026, 9, 18, 14, 56, 0)
POOL = ["002112", "025687", "022853", "002207"]
# 升序位次即别名：002112=F1、002207=F2、022853=F3、025687=F4


def full(failed=(), fallback=(), lt=(), rt_failed=(), rt_deg=()):
    """构造 fund_evidence_complete() 认账的完整逐基金评估结构。"""
    return {"data": {"ok": not failed, "failed": list(failed),
                     "fallback": list(fallback), "n_funds": len(POOL)},
            "lookthrough": {"ok": not lt, "missing": list(lt)},
            "realtime": {"ok": not (rt_failed or rt_deg),
                         "failed": list(rt_failed), "degraded": list(rt_deg)}}


class TestAliasBase(unittest.TestCase):
    """基准表语义：位置由「池内升序」决定，与出现顺序/出现集合无关。"""

    def test_alias_is_sorted_pool_position(self):
        alias = ap.fund_alias(["025687", "002112"])
        self.assertEqual(alias["002112"], "F1")
        self.assertEqual(alias["025687"], "F2")

    def test_same_code_same_alias_across_subsets(self):
        # 关键：子集掩码不得重排——否则同一只基金在两个字段里会是两个 F 号
        only = ap.mask_fund_codes(["022853"], base=POOL)
        both = ap.mask_fund_codes(["022853", "002112"], base=POOL)
        self.assertEqual(only["022853"], both["022853"])
        self.assertEqual(both["022853"], "F3")

    def test_legacy_mode_is_positional_within_given_codes(self):
        # audit_current.json 的单列表口径（base=None）保持旧行为
        self.assertEqual(ap.mask_fund_codes(["025687"]), {"025687": "F1"})

    def test_fingerprint_is_order_insensitive_and_pool_sensitive(self):
        self.assertEqual(ap.fund_pool_fingerprint(POOL),
                         ap.fund_pool_fingerprint(list(reversed(POOL))))
        self.assertNotEqual(ap.fund_pool_fingerprint(POOL), ap.fund_pool_fingerprint(POOL[:-1]))


class TestMaskManifestFunds(unittest.TestCase):
    def _payload(self):
        return {"run_id": "20260918_145500_post", "slot": "post",
                "status": "DEGRADED",
                "data": {"ok": False, "failed": ["002112"], "fallback": ["025687"],
                         "status_unknown": ["002207"], "n_funds": 4},
                "lookthrough": {"ok": False, "missing": ["022853"]},
                "realtime": {"ok": False, "failed": ["002207"],
                             "degraded": ["002112"], "n_funds": 3},
                "notification": {"ok": True, "reason": None},
                "degraded_reasons": [
                    "fund_data_partial:002112",
                    "fund_data_fallback:025687",
                    "lookthrough_missing:022853",
                    "realtime_failed:002207",
                    "realtime_degraded:002112",
                    "market_context_unavailable",
                    "shadow_failed:exit_1"],
                "status2": "SUCCESS"}

    def test_every_fund_keyed_field_is_masked(self):
        out = ap.mask_manifest_funds(self._payload(), POOL)
        self.assertEqual(out["data"]["failed"], ["F1"])
        self.assertEqual(out["data"]["fallback"], ["F4"])
        self.assertEqual(out["lookthrough"]["missing"], ["F3"])
        self.assertEqual(out["realtime"]["failed"], ["F2"])
        self.assertEqual(out["data"]["status_unknown"], ["F2"],
                         "V4.2 的状态未知清单同样是逐基金代码列表，必须掩码")
        self.assertEqual(out["realtime"]["degraded"], ["F1"])
        # 全部 GATE_FUND_KEYED 字段都覆盖到（新加字段若漏掩码，这条会红）
        for _prefix, (section, key) in ap.GATE_FUND_KEYED.items():
            self.assertIn(key, out[section], f"{section}.{key} 未被掩码逻辑覆盖")

    def test_manifest_fund_fields_cover_gate_vocabulary(self):
        """掩码清单必须覆盖门禁词表，且额外含 status_unknown（新字段漏登记就会裸奔）。"""
        for field in ap.GATE_FUND_KEYED.values():
            self.assertIn(tuple(field), ap.MANIFEST_FUND_FIELDS)
        self.assertIn(("data", "status_unknown"), ap.MANIFEST_FUND_FIELDS)

    def test_reasons_masked_but_prefix_kept(self):
        out = ap.mask_manifest_funds(self._payload(), POOL)
        self.assertIn("fund_data_partial:F1", out["degraded_reasons"])
        self.assertIn("fund_data_fallback:F4", out["degraded_reasons"])
        self.assertIn("realtime_degraded:F1", out["degraded_reasons"])
        # 非逐基金项（运行级/子对象）原样保留
        self.assertIn("market_context_unavailable", out["degraded_reasons"])
        self.assertIn("shadow_failed:exit_1", out["degraded_reasons"])

    def test_no_six_digit_code_survives_anywhere_in_file_text(self):
        out = ap.mask_manifest_funds(self._payload(), POOL)
        text = json.dumps(out, ensure_ascii=False)
        for code in POOL:
            self.assertNotIn(code, text, f"{code} 仍留在清单文本里")

    def test_records_alias_base_for_gate_reverse_lookup(self):
        out = ap.mask_manifest_funds(self._payload(), POOL)
        self.assertEqual(out["fund_refs"]["n_pool"], len(POOL))
        self.assertEqual(out["fund_refs"]["pool_sha256_8"],
                         ap.fund_pool_fingerprint(POOL))

    def test_source_payload_not_mutated(self):
        src = self._payload()
        ap.mask_manifest_funds(src, POOL)
        self.assertEqual(src["data"]["failed"], ["002112"],
                         "掩码必须在副本上做——内存态要给门禁真实代码归因")
        self.assertEqual(src["degraded_reasons"][0], "fund_data_partial:002112")


class TestLeakGuard(unittest.TestCase):
    """掩码后残留代码 ⇒ 硬报错（而不是安静写出去）。"""

    def test_residual_code_in_field_raises(self):
        with self.assertRaises(ValueError):
            ap.assert_no_fund_codes({"data": {"failed": ["002112"], "fallback": []}})

    def test_residual_code_in_reason_suffix_raises(self):
        with self.assertRaises(ValueError):
            ap.assert_no_fund_codes(
                {"data": {"failed": [], "fallback": []},
                 "degraded_reasons": ["fund_data_partial:002112"]})

    def test_unrelated_six_digit_numbers_are_not_flagged(self):
        # run_id / ts 里的 6 位片段（145500）不得误伤
        ap.assert_no_fund_codes({"run_id": "20260918_145500_post",
                                 "ts": "2026-09-18T14:55:00+0800",
                                 "data": {"failed": [], "fallback": []},
                                 "degraded_reasons": ["shadow_failed:exit_1"]})

    def test_masked_payload_passes(self):
        out = ap.mask_manifest_funds(
            {"data": {"failed": ["002112"], "fallback": []},
             "degraded_reasons": ["fund_data_partial:002112"]}, POOL)
        ap.assert_no_fund_codes(out)


class TestUnresolvableRefs(unittest.TestCase):
    """反查失败不得被当成「干净」：交给调用方的 fail-closed 分支。"""

    def test_raw_codes_pass_through(self):
        self.assertEqual(ap.resolve_fund_refs(["002112"], POOL), ["002112"])

    def test_aliases_resolve_by_pool_position(self):
        self.assertEqual(ap.resolve_fund_refs(["F1", "F4"], POOL), ["002112", "025687"])

    def test_fingerprint_mismatch_leaves_alias_unresolved(self):
        self.assertEqual(
            ap.resolve_fund_refs(["F1"], POOL, fingerprint="deadbeef"), ["F1"])

    def test_out_of_range_alias_leaves_unresolved(self):
        self.assertEqual(ap.resolve_fund_refs(["F9"], POOL), ["F9"])

    def test_missing_pool_leaves_alias_unresolved(self):
        self.assertEqual(ap.resolve_fund_refs(["F1"], []), ["F1"])

    def test_attribution_escalates_unresolvable_alias_to_run_level(self):
        run_level, per_fund = ap.attribute_degradations(
            ["fund_data_fallback:F9"], {"data": {"fallback": ["F9"]}},
            {"002112"}, pool=POOL)
        self.assertEqual(per_fund, {})
        self.assertEqual(run_level, ["fund_data_fallback:F9"])

    def test_attribution_resolves_alias_to_real_code(self):
        run_level, per_fund = ap.attribute_degradations(
            ["fund_data_fallback:F4"], {"data": {"fallback": ["F4"]}},
            {"002112", "025687"}, pool=POOL)
        self.assertEqual(run_level, [])
        self.assertEqual(list(per_fund), ["025687"])


class TestWriteMasking(unittest.TestCase):
    """落盘侧：写出去的清单不含代码；写不出就转 DEGRADED，不写坏证据。"""

    def setUp(self):
        self._orig = run.BASE_DIR
        self._td = tempfile.TemporaryDirectory()
        run.BASE_DIR = Path(self._td.name)
        run._LOG.clear()

    def tearDown(self):
        run.BASE_DIR = self._orig
        run._LOG.clear()
        self._td.cleanup()

    def _manifest(self, failed=(), fallback=()):
        return {"slot": "post", "status": "DEGRADED" if (failed or fallback) else "SUCCESS",
                "data": {"ok": not failed, "failed": list(failed),
                         "fallback": list(fallback), "n_funds": len(POOL)},
                "lookthrough": {"ok": True, "missing": []},
                "realtime": {"ok": True, "failed": [], "degraded": []},
                "degraded_reasons": [f"fund_data_partial:{c}" for c in failed]
                                    + [f"fund_data_fallback:{c}" for c in fallback],
                "notification": {"ok": None, "reason": "skipped_no_push"}}

    def _out_dir(self):
        return Path(self._td.name) / "output" / "run_manifest"

    def test_written_file_has_aliases_only(self):
        ok = run._write_run_manifest(NOW, self._manifest(failed=["002112"]), POOL)
        self.assertTrue(ok)
        files = list(self._out_dir().glob("*.json"))
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding="utf-8")
        payload = json.loads(text)
        self.assertEqual(payload["data"]["failed"], ["F1"])
        self.assertEqual(payload["degraded_reasons"], ["fund_data_partial:F1"])
        for code in POOL:
            self.assertNotIn(code, text)
        # 监控依赖的字段一个都不能少
        for key in ("run_id", "ts", "status", "fund_refs", "notification"):
            self.assertIn(key, payload)

    def test_code_outside_pool_refuses_to_write(self):
        """池外代码（基准表与本次运行不一致）⇒ 拒绝落盘，绝不写出代码。"""
        ok = run._write_run_manifest(NOW, self._manifest(failed=["999999"]), POOL)
        self.assertFalse(ok)
        self.assertEqual(list(self._out_dir().glob("*.json")), [])
        self.assertTrue(any("运行清单写入失败" in ln for ln in run._LOG), run._LOG)

    def test_refused_write_becomes_degraded_exit_2(self):
        degraded = []
        code = run._finalize_run(NOW, self._manifest(failed=["999999"]),
                                 degraded, POOL)
        self.assertEqual(code, 2)
        self.assertIn("run_manifest_write_failed", degraded)


class TestGateStillAttributes(unittest.TestCase):
    """掩码不得削弱门禁：别名清单在门禁侧仍按真实基金归因。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.man_dir = self.root / "run_manifest"
        self.man_dir.mkdir()
        self._orig = (ap.RUN_MANIFEST_DIR, ap.HOLDINGS, ap.fund_pool)
        ap.RUN_MANIFEST_DIR = self.man_dir
        ap.HOLDINGS = self.root / "holdings.json"
        ap.HOLDINGS.write_text(
            json.dumps({"funds": {"002112": {"shares": 100.0},
                                  "025687": {"shares": 50.0}}}), encoding="utf-8")
        ap.fund_pool = lambda: list(POOL)

    def tearDown(self):
        ap.RUN_MANIFEST_DIR, ap.HOLDINGS, ap.fund_pool = self._orig
        self._td.cleanup()

    def _write(self, run_id, payload):
        payload = dict(payload, run_id=run_id, slot="post")
        masked = ap.mask_manifest_funds(payload, POOL)
        ap.assert_no_fund_codes(masked)
        (self.man_dir / f"run_manifest_{run_id}.json").write_text(
            json.dumps(masked, ensure_ascii=False), encoding="utf-8")

    def test_masked_manifest_blocks_the_contaminated_fund_only(self):
        self._write("20260918_113003_mid",
                    dict(full(fallback=["025687"]),
                         degraded_reasons=["fund_data_fallback:025687"],
                         notification={"ok": True, "reason": None}))
        g = ap.publish_gate(NOW, universe=())
        self.assertFalse(g["ok"], g["detail"])
        self.assertEqual(sorted(g["contaminated"]), ["025687"],
                         "归因结果必须是真实代码（别名只在文件里）")
        self.assertEqual(sorted(g["eligible"]), ["002112"])

    def test_masked_clean_manifest_is_ok(self):
        self._write("20260918_113003_mid",
                    dict(full(), degraded_reasons=[],
                         notification={"ok": True, "reason": None}))
        g = ap.publish_gate(NOW, universe=())
        self.assertTrue(g["ok"], g["detail"])

    def test_alias_base_mismatch_is_fail_closed(self):
        """清单里的基准表指纹与当前基准确不符 ⇒ 别名不可解析 ⇒ 升运行级拦截。"""
        self._write("20260918_113003_mid",
                    dict(full(fallback=["025687"]),
                         degraded_reasons=["fund_data_fallback:025687"],
                         notification={"ok": True, "reason": None}))
        ap.fund_pool = lambda: ["009999", "008888"]        # 池变了 → 指纹变 → F2 无解
        g = ap.publish_gate(NOW, universe=())
        self.assertFalse(g["ok"])
        self.assertEqual(g["eligible"], [], "不可解析的别名不得被当成干净")
        self.assertTrue(any("fund_data_fallback" in r for r in g["run_level"]))

    def test_publish_gate_payload_keeps_codes_out_of_audit_json(self):
        """check_publish_gate 写进 audit_current.json 的措辞只能是掩码后的。"""
        self._write("20260918_113003_mid",
                    dict(full(fallback=["025687"]),
                         degraded_reasons=["fund_data_fallback:025687"],
                         notification={"ok": True, "reason": None}))
        a = ap.Audit()
        ap.check_publish_gate(a, now=NOW)   # 注入固定日：不随墙钟跨日漂移（同 test_publish_gate.py:281 口径）
        check = [c for c in a.checks if c.cid == "P1-9"][0]
        self.assertNotIn("025687", check.detail)
        self.assertIn("F2", check.detail)      # 审计侧别名基准 = 论域(002112,025687)升序位
        self.assertIn("025687", a.meta["publish_gate"]["detail_real"],
                      "控制台仍须看到真实代码")


if __name__ == "__main__":
    unittest.main()
