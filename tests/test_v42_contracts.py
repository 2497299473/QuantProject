"""V4.2（2026-09-18）：四项 🟠 契约收紧。

1. **P1-7 审「当前运行的证据」而不是「曾经跑过」**：`check_run_manifest` 按自洽 / 齐备 /
   新鲜度 / 掩码四问核验最新清单——旧实现只要目录里存在任意 JSON 就 PASS，于是
   「今天没跑、昨天留了一份 SUCCESS」照样过关；
2. **current 指针自证时效**：payload 带 `evidence_as_of` / `evidence_stale`，post 收尾
   调 `refresh_current()` 让指针真正 current（README 写「所有人只看 audit_current.json」，
   指针停在上一交易日就是自相矛盾）；
3. **`_lsjz` 未知状态进动作层硬门禁**：状态未知 ⇒ 动作恒 HOLD，并记进清单
   `data.status_unknown`（动作层打开之前必须解决的前置条件）；
4. **Signal Journal 幂等**：同一交易日只留一行（就地更新，不追加第二行）。

全程 tempdir + 内存结构，零网络，不碰真实 data/ / output/ / Obsidian 笔记。
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
from core import data_loader                             # noqa: E402
from core import decision_engine as de                   # noqa: E402

NOW = datetime(2026, 9, 18, 15, 0, 0)          # 周五 15:00，当日 14:55 post 之后


def manifest(run_id="20260918_145500_post", **over):
    """一份「齐备」的清单（掩码后格式，可直接落盘）。"""
    payload = {"run_id": run_id, "slot": "post", "status": "SUCCESS",
               "data": {"ok": True, "failed": [], "fallback": [],
                        "status_unknown": [], "n_funds": 4},
               "lookthrough": {"ok": True, "missing": []},
               "realtime": {"ok": True, "failed": [], "degraded": []},
               "degraded_reasons": [],
               "notification": {"ok": True, "reason": None}}
    payload.update(over)
    return payload


class ManifestHarness(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.man = Path(self._td.name) / "run_manifest"
        self.man.mkdir()
        self._orig = ap.RUN_MANIFEST_DIR
        ap.RUN_MANIFEST_DIR = self.man

    def tearDown(self):
        ap.RUN_MANIFEST_DIR = self._orig
        self._td.cleanup()

    def write(self, payload, name=None):
        rid = payload.get("run_id", "x")
        (self.man / f"run_manifest_{name or rid}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def status(self, now=NOW):
        a = ap.Audit()
        ap.check_run_manifest(a, now=now)
        return a.checks[0].status, a.checks[0].detail, a.meta["run_manifest"]


class TestP1_7CurrentRunEvidence(ManifestHarness):
    """P1-7：清单存在 ≠ 当前运行的证据完整。"""

    def test_no_manifest_is_warn_when_tri_implemented(self):
        status, detail, meta = self.status()
        self.assertEqual(status, ap.WARN)
        self.assertIn("无产物", detail)
        self.assertTrue(meta["stale"], "没有清单 ⇒ 证据必然过期，meta 必须自证")

    def test_fresh_complete_manifest_passes(self):
        self.write(manifest())
        status, detail, meta = self.status()
        self.assertEqual(status, ap.PASS, detail)
        self.assertEqual(meta["as_of"], "2026-09-18")
        self.assertFalse(meta["stale"])

    def test_yesterdays_manifest_alone_is_stale_not_pass(self):
        """核心回归：9-18 没跑、只剩 9-17 的 SUCCESS —— 旧实现会 PASS。"""
        self.write(manifest(run_id="20260917_145500_post"))
        status, detail, meta = self.status()
        self.assertEqual(status, ap.WARN, detail)
        self.assertTrue(meta["stale"])
        self.assertEqual(meta["expected_as_of"], "2026-09-18")

    def test_run_id_mismatch_fails(self):
        self.write(manifest(run_id="20260918_145500_post"), name="20260918_999999_post")
        status, detail, _ = self.status()
        self.assertEqual(status, ap.FAIL)
        self.assertIn("run_id", detail)

    def test_missing_sections_fail(self):
        payload = manifest()
        del payload["realtime"]
        self.write(payload)
        status, detail, _ = self.status()
        self.assertEqual(status, ap.FAIL)
        self.assertIn("缺关键字段", detail)

    def test_illegal_status_fails(self):
        self.write(manifest(status="OK"))
        status, detail, _ = self.status()
        self.assertEqual(status, ap.FAIL)
        self.assertIn("三态词表", detail)

    def test_failed_status_is_in_vocabulary_but_still_fails_audit(self):
        """V4.5 P0（2026-09-23）：FAILED 是合法三态值，但它如实报告「崩了」。

        两个断言缺一不可——① 不得被判「非三态词表」（词表须与 README 三态退出码
        对齐）；② 也不得被当 SUCCESS 放行（否则兜底清单反而洗白了失败）。
        """
        self.write(manifest(status="FAILED",
                            **{"failure_reason": "exception:TypeError",
                               "failure_stage": "[repo]", "failure_detail": "TypeError"}))
        status, detail, meta = self.status()
        self.assertEqual(status, ap.FAIL, detail)
        self.assertNotIn("三态词表", detail)
        self.assertIn("status=FAILED", detail)
        self.assertIn("exception:TypeError", detail)
        self.assertEqual(meta["status"], "FAILED")

    def test_unmasked_code_in_manifest_fails(self):
        """V4.1 ④ 的契约由审计常驻复核：清单里残留 6 位码 ⇒ FAIL。"""
        self.write(manifest(**{"data": {"ok": False, "failed": ["002112"],
                                        "fallback": [], "status_unknown": [],
                                        "n_funds": 4}}))
        status, detail, _ = self.status()
        self.assertEqual(status, ap.FAIL)
        self.assertIn("残留基金代码", detail)


class TestExpectedRunDate(unittest.TestCase):
    """新鲜度判定的时点语义：周末/假期/盘中不得误报。"""

    def test_monday_morning_expects_friday(self):
        self.assertEqual(
            ap.last_expected_run_date(datetime(2026, 9, 21, 9, 0)),
            "2026-09-18")

    def test_monday_after_mid_expects_today(self):
        self.assertEqual(
            ap.last_expected_run_date(datetime(2026, 9, 21, 12, 0)),
            "2026-09-21")

    def test_saturday_expects_friday(self):
        self.assertEqual(
            ap.last_expected_run_date(datetime(2026, 9, 19, 12, 0)),
            "2026-09-18")

    def test_holiday_is_skipped(self):
        orig = ap._holidays
        ap._holidays = lambda: {"2026-09-18"}
        try:
            self.assertEqual(
                ap.last_expected_run_date(datetime(2026, 9, 21, 9, 0)),
                "2026-09-17")
        finally:
            ap._holidays = orig

    def test_trading_day_helper(self):
        self.assertFalse(ap.is_trading_day(datetime(2026, 9, 19, 12, 0)))
        self.assertTrue(ap.is_trading_day(datetime(2026, 9, 18, 12, 0)))


class TestCurrentPointerFreshness(ManifestHarness):
    def test_payload_carries_evidence_as_of_and_stale(self):
        self.write(manifest(run_id="20260917_145500_post"))
        a = ap.Audit()
        ap.check_run_manifest(a, now=NOW)
        payload = ap.audit_payload(a, a.counts(), ap.derive_states(a))
        self.assertEqual(payload["evidence_as_of"], "2026-09-17")
        self.assertEqual(payload["evidence_expected_as_of"], "2026-09-18")
        self.assertTrue(payload["evidence_stale"])
        self.assertIn("run_manifest", payload["evidence_latest_run"])

    def test_fresh_evidence_is_not_stale(self):
        self.write(manifest())
        a = ap.Audit()
        ap.check_run_manifest(a, now=NOW)
        payload = ap.audit_payload(a, a.counts(), ap.derive_states(a))
        self.assertFalse(payload["evidence_stale"])

    def test_refresh_current_writes_current_and_history(self):
        """post 收尾的刷新动作：current 指针 + 历史快照两份都要落。"""
        self.write(manifest())
        out = Path(self._td.name) / "audit_current.json"
        orig_cur, orig_hist = ap.AUDIT_CURRENT, ap.AUDIT_HISTORY
        ap.AUDIT_CURRENT = out
        ap.AUDIT_HISTORY = out.parent / "audit_history"
        try:
            cur, snap = ap.refresh_current()
        finally:
            ap.AUDIT_CURRENT, ap.AUDIT_HISTORY = orig_cur, orig_hist
        self.assertTrue(cur.is_file())
        self.assertTrue(snap.is_file())
        payload = json.loads(cur.read_text(encoding="utf-8"))
        self.assertEqual(payload["evidence_as_of"], "2026-09-18")
        self.assertIn("production_status", payload)


class TestFundStatusGate(unittest.TestCase):
    """V4.2 ③：申购/赎回状态未知 ⇒ 动作层硬门禁强制 HOLD。"""

    def _cfg(self, history_validated=True):
        return {"decision": {"weights": {"intraday_trend": 0.35, "breadth": 0.25,
                                        "relative_pool": 0.15, "mid_trend": 0.15,
                                        "account": 0.10},
                             "thresholds": {"add": -100, "reduce": -100},
                             "gates": {"min_coverage": 60, "max_snapshot_age_days": 120,
                                       "max_position_pct": 0.8,
                                       "history_validated": history_validated}}}

    def setUp(self):
        self._orig = de._load_cfg
        de._load_cfg = lambda: self._cfg()

    def tearDown(self):
        de._load_cfg = self._orig

    @staticmethod
    def _inp(status_known=True):
        return de.DecisionInput(
            code="002112", name="测试基金", slot="post",
            feat_1455={"est_return": 1.0, "breadth": 0.6, "covered_pct": 90.0,
                       "holdings_age_days": 5},
            pool_est={"002112": 1.0},
            account_state={"current_weight": 0.2},
            fund_status_known=status_known)

    def test_known_status_can_produce_action(self):
        d = de.evaluate(self._inp(status_known=True))
        self.assertEqual(d.invalid_conditions, [])
        self.assertEqual(d.action, "ADD", "门禁全过时动作层应能输出候选动作")

    def test_unknown_status_forces_hold(self):
        d = de.evaluate(self._inp(status_known=False))
        self.assertEqual(d.action, "HOLD")
        self.assertTrue(any("状态未知" in c for c in d.invalid_conditions),
                        d.invalid_conditions)

    def test_default_is_backward_compatible(self):
        """不传该字段的既有调用方（回测/脚本）语义不变：默认视为可得。"""
        self.assertTrue(de.DecisionInput(code="x", name="y", slot="mid").fund_status_known)

    def test_predicate_single_source(self):
        self.assertTrue(data_loader.is_fund_status_known(
            {"purchase_status": "开放申购", "redeem_status": "开放赎回"}))
        for bad in ("未知", "", None):
            self.assertFalse(data_loader.is_fund_status_known(
                {"purchase_status": bad, "redeem_status": "开放赎回"}), bad)
        self.assertFalse(data_loader.is_fund_status_known({}))
        self.assertFalse(data_loader.is_fund_status_known(None))

    def test_gate_reason_is_registered_not_a_degraded_item(self):
        """状态未知不是发布门禁的输入（它只锁动作层），故不得进 GATE_FUND_KEYED。"""
        self.assertNotIn("fund_status_unknown", ap.GATE_FUND_KEYED)
        self.assertIn(("data", "status_unknown"), ap.MANIFEST_FUND_FIELDS)


class TestSignalJournalIdempotency(unittest.TestCase):
    """V4.2 ④：同一交易日只留一行；其余内容逐字节不动。"""

    SIGNALS = {"002112": {"stance": "中性", "score": 1},
               "025687": {"stance": "偏空", "score": -2}}

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name) / "journal.md"
        run._LOG.clear()

    def tearDown(self):
        run._LOG.clear()
        self._td.cleanup()

    def _append(self, when, signals=None, account=None, slot="post"):
        return run.append_obsidian_log(slot, signals or self.SIGNALS,
                                       account or {"positions": []}, when, self.path)

    def _rows(self):
        return [ln for ln in self.path.read_text(encoding="utf-8").splitlines()
                if ln.startswith("| ") and " 盘后 |" in ln]

    def test_first_write_creates_header_and_row(self):
        self.assertTrue(self._append(datetime(2026, 9, 18, 14, 56)))
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# 基金日频参谋 · 盘后信号流水（2026）", text)
        self.assertEqual(len(self._rows()), 1)

    def test_same_day_rerun_updates_in_place(self):
        self._append(datetime(2026, 9, 18, 14, 56))
        changed = {"002112": {"stance": "偏多", "score": 3},
                   "025687": {"stance": "偏空", "score": -2}}
        self.assertTrue(self._append(datetime(2026, 9, 18, 15, 40), signals=changed))
        rows = self._rows()
        self.assertEqual(len(rows), 1, "同日重跑不得追加第二行")
        self.assertIn("🔴偏多+3", rows[0], "当日行应为最后一次运行的结果（last-run-wins）")

    def test_other_days_append_and_stay_untouched(self):
        self._append(datetime(2026, 9, 18, 14, 56))
        before = self.path.read_text(encoding="utf-8")
        self._append(datetime(2026, 9, 21, 14, 56))
        after = self.path.read_text(encoding="utf-8")
        self.assertEqual(len(self._rows()), 2)
        self.assertTrue(after.startswith(before.rstrip("\n")),
                        "追加不得改动既有行（历史小节必须逐字节保持）")

    def test_legacy_duplicate_rows_are_merged(self):
        self.path.write_text(
            "# 流水\n\n"
            "| 日期 | 002112 | 025687 | 账户面 |\n|:---|---|---:|\n"
            "| 09-18 盘后 | ⚪中性+0 | ⚪中性+0 | 无持仓 |\n"
            "| 09-18 盘后 | ⚪中性+1 | ⚪中性+1 | 无持仓 |\n", encoding="utf-8")
        self._append(datetime(2026, 9, 18, 15, 40))
        rows = self._rows()
        self.assertEqual(len(rows), 1, "历史重复行应被归并")
        self.assertIn("⚪中性+1", rows[0])
        self.assertTrue(self.path.read_text(encoding="utf-8").startswith("# 流水"))

    def test_non_post_or_non_trading_day_writes_nothing(self):
        self.assertFalse(self._append(datetime(2026, 9, 18, 11, 30), slot="mid"))
        self.assertFalse(self._append(datetime(2026, 9, 19, 14, 56)))   # 周六
        self.assertFalse(self.path.exists())

    def test_no_tmp_left_behind(self):
        self._append(datetime(2026, 9, 18, 14, 56))
        self._append(datetime(2026, 9, 18, 15, 40))
        self.assertEqual(list(Path(self._td.name).glob("*.tmp")), [])


class TestObsidianLogConfigured(unittest.TestCase):
    """P0-3（2026-09-23）：Obsidian 流水路径配置化（notify.obsidian_signal_log）。

    单一事实来源 = config.json；留空/删键 = 禁用（静默跳过不算 degraded）；
    config 缺失/损坏 → 回退旧默认路径（保持原有行为）。
    """

    SIGNALS = {"002112": {"stance": "中性", "score": 1}}

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self._old_base = run.BASE_DIR
        run.BASE_DIR = self.root          # _configured_obsidian_log 读模块级 BASE_DIR
        run._LOG.clear()

    def tearDown(self):
        run.BASE_DIR = self._old_base
        run._LOG.clear()
        self._td.cleanup()

    def _write_cfg(self, notify):
        (self.root / "config.json").write_text(
            json.dumps({"notify": notify}), encoding="utf-8")

    def test_absolute_path_from_config(self):
        self._write_cfg({"obsidian_signal_log": r"C:\tmp\journal.md"})
        self.assertEqual(run._configured_obsidian_log(), Path(r"C:\tmp\journal.md"))

    def test_relative_path_resolves_against_root(self):
        self._write_cfg({"obsidian_signal_log": "notes/journal.md"})
        self.assertEqual(run._configured_obsidian_log(),
                         self.root / "notes" / "journal.md")

    def test_empty_or_missing_key_disables(self):
        self._write_cfg({"obsidian_signal_log": "   "})
        self.assertIsNone(run._configured_obsidian_log())
        self._write_cfg({})
        self.assertIsNone(run._configured_obsidian_log())

    def test_corrupted_config_falls_back_to_default(self):
        (self.root / "config.json").write_text("{ not json", encoding="utf-8")
        self.assertEqual(run._configured_obsidian_log(), run.OBSIDIAN_LOG)

    def test_append_with_none_path_uses_config(self):
        target = self.root / "journal.md"
        self._write_cfg({"obsidian_signal_log": str(target)})
        ok = run.append_obsidian_log("post", self.SIGNALS, {"positions": []},
                                     NOW, path=None)
        self.assertTrue(ok)
        self.assertTrue(target.exists())

    def test_append_disabled_when_config_empty(self):
        self._write_cfg({"obsidian_signal_log": ""})
        ok = run.append_obsidian_log("post", self.SIGNALS, {"positions": []},
                                     NOW, path=None)
        self.assertFalse(ok)
        self.assertEqual(list(self.root.glob("*.md")), [])


if __name__ == "__main__":
    unittest.main()
