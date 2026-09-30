"""面 5 生产路径/编排修复测例（D5 批，2026-09-30）。

覆盖第五轮审计问题单落到生产侧的修复面：
- D5-A1 production_status 生产接线：run.production_eligibility 纯函数三态
  （READY/BLOCKED/UNKNOWN）+ 报告段/卡片行渲染 + run.py 接线序源码守护；
- D5-B1 _score_account(None) 幽灵 +4 死亡（直接调用路径）；
- D5-B2 _resolve_freshness 缺失 ≠ 最鲜（旧默认 1.0 路径死亡）；
- D5-B3 backtest_action 阈值与 config.decision.thresholds 同源（字面量死亡）；
- D5-D1 nav_freshness 净值日期过期判定（日历口径复用 audit 侧）；
- D5-D2 cache:fallback 告警上卡片（判定走 data_loader 谓词单一事实源）；
- D5-D3 Obsidian 流水行净值日期锚点；
- D5-E1 P1-15 drift 观察层时效（WARN 非 FAIL，不碰 registry/promotion）；
- D5-F1 build_panel_dlite main 守卫常开（无开关可关）；
- D5-F2 drift_monitor --fresh 双旗标 + 默认路径守卫 selftest；
- D5-F3 --no-net 关守卫必须警示 + 产物 meta 留痕。

纪律：全程注入（monkeypatch 模块属性 / tempdir 造文件），零网络、不碰真实
data/ 与 output/——fast 层惯例（同 test_audit_hygiene / test_v42_contracts）。
shadow_policy 相关（D5-C1）在 tests/test_shadow_policy.py（slow 层）另测。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import audit_project as ap                                   # noqa: E402
import run                                                   # noqa: E402
from core import decision_engine as de                       # noqa: E402
from core import intraday_features as ifx                    # noqa: E402
from core import notify, report_generator                    # noqa: E402

import backtest_action as ba                                 # noqa: E402
import drift_monitor as dm                                   # noqa: E402

SRC_RUN = (BASE_DIR / "run.py").read_text(encoding="utf-8")
SRC_ACTION = (BASE_DIR / "backtest_action.py").read_text(encoding="utf-8")
SRC_PANEL = (BASE_DIR / "experiments" / "forecast_lab" / "build_panel_dlite.py").read_text(encoding="utf-8")
SRC_ESAB = (BASE_DIR / "backtest_early_stopping_ab.py").read_text(encoding="utf-8")
SRC_PITMX = (BASE_DIR / "backtest_pit1455_matrix.py").read_text(encoding="utf-8")
SRC_DRIFT = (BASE_DIR / "drift_monitor.py").read_text(encoding="utf-8")
SRC_NOTIFY = (BASE_DIR / "core" / "notify.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------- D5-A1

class TestProductionEligibility(unittest.TestCase):
    """production_status → 生产侧标注（只标注不拦截；措辞单一真源）。"""

    BLOCKED_PAYLOAD = {
        "production_status": "BLOCKED",
        "audit_health": "PASS_WITH_WARNINGS",
        "generated_at": "2026-09-30T14:55:56",
        "inputs": {"model_ready": False, "history_validated": False,
                   "active_promotion": "blocked"},
    }

    def test_blocked_yields_note_with_reasons(self):
        p = run.production_eligibility(self.BLOCKED_PAYLOAD)
        self.assertEqual(p["status"], "BLOCKED")
        self.assertIsNotNone(p["note"])
        self.assertIn("BLOCKED", p["note"])
        # 验收：标注含原因（model_ready / promotion / history_validated 至少一项）
        self.assertIn("model_ready=false", p["note"])
        self.assertIn("promotion=blocked", p["note"])
        self.assertIn("history_validated=False", p["note"])

    def test_ready_yields_no_note(self):
        p = run.production_eligibility({
            "production_status": "READY",
            "inputs": {"model_ready": True, "history_validated": True,
                       "active_promotion": "approved"}})
        self.assertEqual(p["status"], "READY")
        self.assertIsNone(p["note"])
        self.assertEqual(p["reasons"], [])

    def test_missing_or_broken_pointer_is_unknown_fail_closed_note(self):
        for payload in (None, {}, {"foo": 1}, "not-a-dict"):
            p = run.production_eligibility(payload)
            self.assertEqual(p["status"], "UNKNOWN")
            self.assertIsNotNone(p["note"], f"查不到 ≠ 有资格：{payload!r}")

    def test_report_section_renders_note_only(self):
        sec = report_generator.production_section(
            run.production_eligibility(self.BLOCKED_PAYLOAD))
        self.assertIn("生产资格状态", sec)
        self.assertIn("BLOCKED", sec)
        self.assertEqual(report_generator.production_section(None), "")
        self.assertEqual(report_generator.production_section({"status": "READY", "note": None}), "")

    def test_card_note_line_present_when_blocked(self):
        prod = run.production_eligibility(self.BLOCKED_PAYLOAD)
        card = notify._build_card("post", {}, {"positions": [], "abnormal_alerts": [],
                                               "total_market_value": 0.0, "total_pnl_pct": 0.0},
                                  production=prod)
        texts = [e["elements"][0]["content"]
                 for e in card["card"]["elements"] if e["tag"] == "note"]
        self.assertTrue(any("生产资格" in t for t in texts), texts)
        # 免责脚注必须仍在最后
        self.assertIn("绝不自动下单", texts[-1])

    def test_card_without_production_unchanged(self):
        card = notify._build_card("post", {}, {"positions": [], "abnormal_alerts": [],
                                               "total_market_value": 0.0, "total_pnl_pct": 0.0})
        texts = [e["elements"][0]["content"]
                 for e in card["card"]["elements"] if e["tag"] == "note"]
        self.assertEqual(len(texts), 1, "旧调用方（不传 production）只留免责脚注")

    def test_run_wiring_order_and_kwargs(self):
        """接线守护：读取点在报告生成前；generate_report/push_feishu 都带 production=。"""
        i_read = SRC_RUN.index("prod = production_eligibility(")
        i_report = SRC_RUN.index("report = report_generator.generate_report(")
        self.assertLess(i_read, i_report, "资格读取必须在报告生成前（报告要能标注）")
        self.assertIn("production=prod", SRC_RUN)
        self.assertEqual(SRC_RUN.count("production=prod"), 2,
                         "generate_report 与 push_feishu 两处都要带")
        # 指针读取走 audit_project 单一入口（不另写路径/第二套 JSON 解析）
        self.assertIn("audit_project_mod.load_json(audit_project_mod.AUDIT_CURRENT)", SRC_RUN)


# ---------------------------------------------------------------- D5-B1

class TestScoreAccountNoneDeath(unittest.TestCase):
    """幽灵 +4 原发位置死亡：直接调用 _score_account(None) 不再当空仓。"""

    def test_none_returns_zero_unavailable(self):
        score, reasons = de._score_account(None)
        self.assertEqual(score, 0)
        self.assertTrue(any("不可用" in r for r in reasons), reasons)
        self.assertFalse(any("仓位较低" in r for r in reasons), "幽灵 +4 复活 = 旧路径未死")

    def test_explicit_empty_dict_keeps_plus4(self):
        score, reasons = de._score_account({})
        self.assertEqual(score, 4)
        self.assertTrue(any("仓位较低" in r for r in reasons))

    def test_old_defensive_line_dead(self):
        """幽灵 +4 原发行 ``acct = acct or {}`` 必须从可执行代码里消失（AST 判据）。

        判据收窄到「对 acct 自身的 or 重绑定」：函数内字段级默认
        （``acct.get("current_weight", 0.0) or 0.0``）是 A 单认定的无害类
        （计数/权重语义，{} 显式空仓语境），不在本判据打击面。
        也不误伤注释/文档里对旧 bug 的历史描述（_check_gates 注释里就有
        ``(acct or {})`` 字样，那是修复记录不是活代码）。
        """
        import ast
        src = (BASE_DIR / "core" / "decision_engine.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_score_account")
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.BoolOp)
                    and isinstance(node.value.op, ast.Or)):
                continue
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "acct":
                    self.fail(f"_score_account 内仍有 acct 重绑定 or 默认注入 "
                              f"L{node.lineno}（幽灵 +4 原发行未死）")
        # None 分支必须显式存在
        self.assertIn("if acct is None:", src)


# ---------------------------------------------------------------- D5-B2

class TestResolveFreshness(unittest.TestCase):
    """holdings_freshness 缺失 → 不再默认 1.0（未知 ≠ 最鲜）。"""

    def test_missing_everything_is_neutral(self):
        self.assertEqual(de._resolve_freshness(None), 0.5)
        self.assertEqual(de._resolve_freshness({}), 0.5)

    def test_age_fallback_uses_single_source_curve(self):
        self.assertEqual(de._resolve_freshness({"holdings_age_days": 10}), 1.0)
        self.assertEqual(de._resolve_freshness({"holdings_age_days": 200}), 0.0)
        self.assertAlmostEqual(de._resolve_freshness({"holdings_age_days": 100}),
                               ifx.freshness_from_age(100))
        self.assertAlmostEqual(ifx.freshness_from_age(100), 1.0 - 55 / 75.0)

    def test_explicit_key_wins(self):
        self.assertEqual(de._resolve_freshness(
            {"holdings_freshness": 0.2, "holdings_age_days": 10}), 0.2)

    def test_bool_and_garbage_are_not_numbers(self):
        self.assertEqual(de._resolve_freshness({"holdings_freshness": True}), 0.5)
        self.assertEqual(de._resolve_freshness({"holdings_age_days": "bad"}), 0.5)
        self.assertEqual(ifx.freshness_from_age(None), 0.5)

    def test_old_default_one_dead(self):
        """旧「缺失=最鲜」默认必须从可执行代码里消失（AST 判据，不误伤注释/文档）。

        仓内先例：test_audit_hygiene 的 AST 结构判据（注释里出现旧代码字面量
        不构成假 PASS/假 FAIL）。
        """
        import ast
        tree = ast.parse((BASE_DIR / "core" / "decision_engine.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "holdings_freshness"
                    and len(node.args) >= 2):
                self.fail(f"holdings_freshness 带默认值的 .get 仍在 L{node.lineno}（旧路径未死）")
        # 新解析单点必须在 evaluate_with_forecast 里接线
        self.assertIn("freshness = _resolve_freshness(inp.feat_1455)", 
                      (BASE_DIR / "core" / "decision_engine.py").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- D5-B3

class TestActionThresholdsSameSource(unittest.TestCase):
    """backtest_action 桶边界与 config.decision.thresholds 同源。"""

    def test_injected_config_drives_buckets(self):
        cfg = {"decision": {"thresholds": {"add": 70, "mid_band": [-10, 5]}}}
        self.assertEqual(ba.action_thresholds(cfg), (70.0, -10.0, 5.0))

    def test_defaults_match_engine_defaults(self):
        # config 缺键时的缺省必须与 decision_engine.evaluate 的缺省一致（60）
        self.assertEqual(ba.action_thresholds({}), (60.0, -20.0, 20.0))
        self.assertEqual(ba.action_thresholds(None), (60.0, -20.0, 20.0))

    def test_hardcoded_literals_dead(self):
        import ast
        tree = ast.parse(SRC_ACTION)
        # 可执行代码里不得再有「score 与字面量 60 / -20 / 20 比较」的桶边界
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                for op, comp in zip(node.ops, node.comparators):
                    if isinstance(comp, ast.Constant) and comp.value in (60, -20, 20) \
                            and isinstance(op, (ast.GtE, ast.LtE, ast.Gt, ast.Lt)):
                        self.fail(f"backtest_action 仍有字面量阈值比较 L{node.lineno}")
        self.assertIn('r["score"] >= thr_add', SRC_ACTION)
        self.assertIn("thr_mid_lo <= r[\"score\"] <= thr_mid_hi", SRC_ACTION)

    def test_report_wording_references_actual_values(self):
        self.assertIn("阈值=config.decision.thresholds 当前值", SRC_ACTION)
        self.assertIn("add={thr_add:g}", SRC_ACTION)


# ---------------------------------------------------------------- D5-D1

class HolidaysHarness(unittest.TestCase):
    """注入 ap.DATA（holidays.json）——fast 层不碰真实 data/。"""

    HOLIDAYS = {"years": {"2026": {
        "中秋": ["2026-09-25"],
        "国庆": ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"],
    }}}

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        (self.root / "holidays.json").write_text(
            json.dumps(self.HOLIDAYS), encoding="utf-8")
        self._orig = ap.DATA
        ap.DATA = self.root

    def tearDown(self):
        ap.DATA = self._orig
        self._td.cleanup()


class TestNavFreshness(HolidaysHarness):
    CFG = {"fund_pool": ["002112"],
           "nav_disclosure": {"default": "domestic_t1", "overrides": {}}}

    def test_expected_date_walks_trading_days(self):
        # 2026-09-30（周三）14:55 后 → 当日；lag=1 → 09-29（周二）
        self.assertEqual(run.expected_nav_date(datetime(2026, 9, 30, 14, 55), 1), "2026-09-29")
        # 周一早上（09-28 09:00）→ last_expected=09-25 是中秋 → 09-24；lag=1 → 09-23
        self.assertEqual(run.expected_nav_date(datetime(2026, 9, 28, 9, 0), 1), "2026-09-23")

    def test_fresh_nav_not_stale(self):
        r = run.nav_freshness("2026-09-29", datetime(2026, 9, 30, 14, 55), self.CFG)
        self.assertFalse(r["stale"])
        self.assertEqual(r["lag"], 1)

    def test_stale_nav_flagged(self):
        r = run.nav_freshness("2026-09-26", datetime(2026, 9, 30, 14, 55), self.CFG)
        self.assertTrue(r["stale"])
        self.assertIn("2026-09-29", r["detail"])

    def test_qdii_override_widens_lag(self):
        cfg = {"fund_pool": ["002112"],
               "nav_disclosure": {"default": "domestic_t1",
                                  "overrides": {"002112": "qdii_t2"}}}
        r = run.nav_freshness("2026-09-28", datetime(2026, 9, 30, 14, 55), cfg)
        self.assertEqual(r["lag"], 2)
        self.assertFalse(r["stale"], "QDII T-2 披露：09-28 在 09-30 是合法最新")

    def test_unknown_profile_skips_judgement(self):
        cfg = {"fund_pool": ["002112"],
               "nav_disclosure": {"default": "martian_t9", "overrides": {}}}
        r = run.nav_freshness("2026-09-26", datetime(2026, 9, 30, 14, 55), cfg)
        self.assertIsNone(r["stale"])
        self.assertIn("未知披露档位", r["detail"])

    def test_missing_or_bad_date_skips_judgement(self):
        r = run.nav_freshness("", datetime(2026, 9, 30, 14, 55), self.CFG)
        self.assertIsNone(r["stale"])
        r2 = run.nav_freshness("garbage", datetime(2026, 9, 30, 14, 55), self.CFG)
        self.assertIsNone(r2["stale"])
        self.assertIn("不可解析", r2["detail"])

    def test_report_renders_stale_warning(self):
        from core.report_generator import generate_report
        # 只验呈现函数不判定：stale=True → 警示行；stale=False/None → 无
        nf_stale = {"stale": True, "detail": "净值截至 2026-09-26，应可见至 2026-09-29"}
        nf_ok = {"stale": False, "detail": "x"}
        signals = {"002112": {"name": "n", "last_nav": 1.0, "last_nav_date": "2026-09-26",
                              "score": 0, "stance": "中性", "insufficient_history": False,
                              "purchase_status": "开放", "redeem_status": "开放",
                              "factors": {}, "_source": "fresh", "source": "",
                              "wording": "总分 +0 · 中性（弱参考）"}}
        account = {"positions": [], "total_market_value": 0.0, "total_pnl_pct": 0.0,
                   "abnormal_alerts": [], "as_of_nav_date": "2026-09-26"}
        with tempfile.TemporaryDirectory() as td:
            orig = report_generator.BASE_DIR
            report_generator.BASE_DIR = Path(td)
            try:
                rep = generate_report("post", signals, account, nav_fresh=nf_stale)
                self.assertIn("净值滞后警示", rep)
                rep2 = generate_report("post", signals, account, nav_fresh=nf_ok)
                self.assertNotIn("净值滞后警示", rep2)
                rep3 = generate_report("post", signals, account)
                self.assertNotIn("净值滞后警示", rep3)   # 旧调用方行为不变
            finally:
                report_generator.BASE_DIR = orig


# ---------------------------------------------------------------- D5-D2

class TestCardFallbackWarning(unittest.TestCase):
    ACCOUNT = {"positions": [], "total_market_value": 0.0, "total_pnl_pct": 0.0,
               "abnormal_alerts": [], "as_of_nav_date": "2026-09-26"}

    @staticmethod
    def _sig(**kw):
        s = {"name": "n", "score": 0, "stance": "中性", "insufficient_history": False,
             "last_nav": 1.0, "last_nav_date": "2026-09-26", "_source": "fresh", "source": ""}
        s.update(kw)
        return s

    def test_fallback_warning_on_card(self):
        card = notify._build_card(
            "post", {"002112": self._sig(_source="cache:fallback(boom)")}, self.ACCOUNT)
        blob = json.dumps(card, ensure_ascii=False)
        self.assertIn("旧缓存告警", blob)
        self.assertIn("002112", blob)

    def test_fresh_card_has_no_warning(self):
        card = notify._build_card("post", {"002112": self._sig()}, self.ACCOUNT)
        self.assertNotIn("旧缓存告警", json.dumps(card, ensure_ascii=False))

    def test_shared_predicate_single_source(self):
        # 卡片与报告共用 nav_fallback_notes（data_loader.is_nav_fallback 谓词），
        # notify 不得自写 startswith 判定。
        self.assertIn("nav_fallback_notes", SRC_NOTIFY)
        self.assertNotIn('startswith("cache:fallback")', SRC_NOTIFY)
        notes = report_generator.nav_fallback_notes(
            {"002112": self._sig(_source="cache:fallback(x)"),
             "025687": self._sig()})
        self.assertEqual(notes, ["002112（净值截至 2026-09-26）"])


# ---------------------------------------------------------------- D5-D3

class TestObsidianNavAnchor(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name) / "journal.md"
        run._LOG.clear()

    def tearDown(self):
        run._LOG.clear()
        self._td.cleanup()

    def _signals(self, **extra):
        return {"002112": {"stance": "中性", "score": 1, **extra}}

    def test_row_carries_nav_date_anchor(self):
        ok = run.append_obsidian_log(
            "post", self._signals(last_nav_date="2026-09-26"),
            {"positions": []}, datetime(2026, 9, 30, 14, 56), self.path)
        self.assertTrue(ok)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("@09-26", text)

    def test_legacy_signals_without_key_still_work(self):
        ok = run.append_obsidian_log(
            "post", self._signals(), {"positions": []},
            datetime(2026, 9, 30, 14, 56), self.path)
        self.assertTrue(ok)
        self.assertNotIn("@", self.path.read_text(encoding="utf-8").splitlines()[-1])


# ---------------------------------------------------------------- D5-E1

class TestDriftFreshnessCheck(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.out = Path(self._td.name) / "output"
        self.out.mkdir()
        self._orig = ap.OUTPUT
        ap.OUTPUT = self.out

    def tearDown(self):
        ap.OUTPUT = self._orig
        self._td.cleanup()

    def _check(self, now=datetime(2026, 9, 30, 15, 0)):
        a = ap.Audit()
        ap.check_drift_monitor_freshness(a, now=now)
        self.assertEqual(len(a.checks), 1)
        return a.checks[0]

    def test_recent_report_passes(self):
        (self.out / "drift_monitor_20260930.md").write_text("# x", encoding="utf-8")
        c = self._check()
        self.assertEqual(c.status, ap.PASS, c.detail)
        self.assertEqual(c.cid, "P1-15")

    def test_old_report_warns_not_fails(self):
        (self.out / "drift_monitor_20260901.md").write_text("# x", encoding="utf-8")
        c = self._check()
        self.assertEqual(c.status, ap.WARN, "观察层定位：过期 ⇒ WARN 非 FAIL")
        self.assertIn("29 天", c.detail)

    def test_missing_report_warns(self):
        c = self._check()
        self.assertEqual(c.status, ap.WARN)
        self.assertIn("无可解析报告", c.detail)

    def test_unparsable_name_ignored(self):
        (self.out / "drift_monitor_notadate.md").write_text("# x", encoding="utf-8")
        c = self._check()
        self.assertEqual(c.status, ap.WARN, "畸形文件名不计入候选（宁缺不猜）")

    def test_wired_into_run_audit(self):
        import inspect
        self.assertIn("check_drift_monitor_freshness", inspect.getsource(ap.run_audit))


# ---------------------------------------------------------------- D5-F1

class TestPanelDliteMainGuard(unittest.TestCase):
    def test_main_guard_installed_before_build(self):
        i_install = SRC_PANEL.index('_restore_main = no_net_guard.install(')
        i_build = SRC_PANEL.index("rows, stats = _build_rows()")
        self.assertLess(i_install, i_build, "守卫必须在主流程读取数据前装载")

    def test_audit_track_construction_blocked(self):
        main_seg = SRC_PANEL[SRC_PANEL.index("def main("):]
        self.assertIn("block_construction=True", main_seg)

    def test_no_cli_switch_to_disable(self):
        self.assertNotIn('"--no-net"', SRC_PANEL, "主流程守卫常开，无开关可关")


# ---------------------------------------------------------------- D5-F2

class TestDriftNetGuard(unittest.TestCase):
    def test_fresh_requires_double_flag(self):
        rc = dm.main(["--fresh"])
        self.assertEqual(rc, 3, "--fresh 无确认旗标必须拒绝启动（不触网）")

    def test_guard_selftest_blocks(self):
        ok, detail = dm.guard_selftest()
        self.assertTrue(ok, detail)
        self.assertIn("blocked", detail)

    def test_default_guard_wired_before_resolve(self):
        i_main = SRC_DRIFT.index("def main(")
        i_guard = SRC_DRIFT.index("        _install_default_guard()", i_main)
        i_resolve = SRC_DRIFT.index("resolve_samples(args.snapshot", i_main)
        self.assertLess(i_guard, i_resolve, "守卫必须在样本解析（潜在活拉点）之前")

    def test_confirm_flag_declared(self):
        self.assertIn("--i-know-this-hits-network", SRC_DRIFT)
        agents = (BASE_DIR / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("drift_monitor.py --fresh", agents, "铁律 8 名单必须补 drift_monitor --fresh")


# ---------------------------------------------------------------- D5-F3

class TestNoNetSwitchLeavesTrace(unittest.TestCase):
    def test_early_stopping_meta_records_guard_state(self):
        self.assertIn('"socket_guard": bool(args.net_guard)', SRC_ESAB)
        self.assertIn("[warn] ⚠️ --no-net", SRC_ESAB)

    def test_pit_matrix_meta_records_guard_state(self):
        self.assertIn('"socket_guard": bool(args.net_guard)', SRC_PITMX)
        self.assertIn("[warn] ⚠️ --no-net", SRC_PITMX)


if __name__ == "__main__":
    unittest.main(verbosity=2)
