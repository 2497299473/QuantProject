"""审计检查项行为测试（D 批修复，2026-09-28）。

覆盖本轮问题单落到 audit_project.py / core/model_registry.py 的六个改动面：
- D-04  P1-5 影子通道隔离：AST 结构判据（CHANNELS 常量元组 + 模块级函数定义），
        不 import shadow_policy（实测 9.88s/1245 模块/带起 numpy+scipy+sklearn）；
        注释里出现通道名不再构成假 PASS。
- D-08  update_promotion 维持下游拦截（Summer 裁决）：零逻辑改动，裁决依据写入
        docstring 留痕；安全性由 verify_approval 硬门 + 审计 P1-13 双层承担，
        既有契约测试 test_promotion_rule_v2.py 保持原样即为回归证据。
- D-05  P2-2 Tushare HTTPS：AST 字符串常量判据（注释/docstring 不计、常量必算、
        命中 FAIL、不可解析 WARN）；扫描面限定 core/experiments/根级入口。
- D-06  P2-3 测试分层：四环链（LAYERS 登记覆盖 / 死条目 / conftest 接线 /
        markers 注册），空转态必须 WARN 不得 PASS。
- D-07  P0-2 持仓隐私：.env 变体 + holdings*.json + config 禁键三面；
        *.example 模板白名单；detail 不得写出禁键的值（入库防二次泄露）。
- D-02  P1-13 授权门一致性前哨：stored/derived 四象限（一致 PASS / 手填
        approved FAIL / 漏升级 WARN / 漂移 WARN）+ active 缺位 WARN。
- D-09  P1-14 prereg 卫生：过期未清理 WARN / 有效 PASS / 文件缺失 PASS /
        expiry 不可解析 WARN。

纪律：全程注入（monkeypatch 模块属性 / tempdir 造文件），零网络、不碰真实
data/ 与 output/——fast 层惯例（同 test_model_registry / test_publish_gate）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import audit_project as ap  # noqa: E402


def _run(fn, **patches):
    """注入 ap 模块属性 → 跑单个 check → 返回唯一 Check。

    patches 里的键是 ap 的模块级名字（BASE_DIR / _config / _models /
    git_lines / PREREG ...），退出时逐一还原（同 test_provenance_binding 惯例）。
    """
    saved = {}
    try:
        for k, v in patches.items():
            saved[k] = getattr(ap, k)
            setattr(ap, k, v)
        a = ap.Audit()
        fn(a)
        assert len(a.checks) == 1, f"{fn.__name__} 应恰好产出 1 条 Check"
        return a.checks[0]
    finally:
        for k, v in saved.items():
            setattr(ap, k, v)


# ---------------------------------------------------------------- D-05 P2-2

class TestTushareHttpsAST(unittest.TestCase):
    """AST 判据：代码常量算、注释/docstring 不算、不可解析判 WARN。"""

    URL = ap._TUSHARE_INSECURE      # 拼接常量，测试也不自指

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        (self.tmp / "core").mkdir()
        (self.tmp / "experiments").mkdir()

    def tearDown(self):
        self._td.cleanup()

    def _check(self):
        return _run(ap.check_tushare_https, BASE_DIR=self.tmp)

    def test_clean_tree_passes(self):
        (self.tmp / "core" / "ok.py").write_text(
            'URL = "https://api.tushare.pro"\n', encoding="utf-8")
        c = self._check()
        self.assertEqual(c.status, ap.PASS, c.detail)
        self.assertEqual(c.cid, "P2-2")

    def test_code_constant_fails(self):
        (self.tmp / "core" / "bad.py").write_text(
            f'URL = "{self.URL}"\n', encoding="utf-8")
        c = self._check()
        self.assertEqual(c.status, ap.FAIL, "D-05 验收：命中必须 FAIL 不再 WARN")
        self.assertIn("core/bad.py:L1", c.detail.replace("\\", "/"))

    def test_comment_and_docstring_do_not_count(self):
        (self.tmp / "core" / "doc.py").write_text(
            f'"""模块 docstring 提到 {self.URL} 属文档。"""\n'
            f"# 注释里的 {self.URL} 也不算\n"
            'X = 1\n', encoding="utf-8")
        c = self._check()
        self.assertEqual(c.status, ap.PASS, c.detail)

    def test_fstring_fragment_constant_still_counts(self):
        # f-string 里的 URL 片段会以 Constant 节点存在——判据照样抓到
        (self.tmp / "core" / "fs.py").write_text(
            f'def f(x):\n    return x + "{self.URL}"\n', encoding="utf-8")
        c = self._check()
        self.assertEqual(c.status, ap.FAIL, c.detail)

    def test_unparsable_file_warns_not_passes(self):
        (self.tmp / "core" / "broken.py").write_text(
            "def f(:\n", encoding="utf-8")
        c = self._check()
        self.assertEqual(c.status, ap.WARN, "不可核验 ≠ 无：必须 WARN 不得 PASS")
        self.assertIn("broken.py", c.detail)

    def test_backups_and_tests_outside_scan(self):
        (self.tmp / "backups").mkdir()
        (self.tmp / "tests").mkdir()
        (self.tmp / "backups" / "old.py").write_text(
            f'URL = "{self.URL}"\n', encoding="utf-8")
        (self.tmp / "tests" / "test_x.py").write_text(
            f'URL = "{self.URL}"\n', encoding="utf-8")
        (self.tmp / "root_entry.py").write_text("X = 1\n", encoding="utf-8")
        c = self._check()
        self.assertEqual(c.status, ap.PASS,
                         "backups/tests 是档案与夹具（不发请求），不在扫描面")


# ---------------------------------------------------------------- D-06 P2-3

class TestLayeringEffective(unittest.TestCase):
    """四环链：任一断环 ⇒ WARN；四环齐 ⇒ PASS。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        (self.tmp / "tests").mkdir()
        (self.tmp / "tests" / "test_a.py").write_text("", encoding="utf-8")
        (self.tmp / "tests" / "conftest.py").write_text(
            "from tests.layers import layer_of\n", encoding="utf-8")
        (self.tmp / "pytest.ini").write_text(
            "[pytest]\nmarkers =\n    fast: x\n    slow: y\n", encoding="utf-8")
        self._orig_layers_map = ap._layers_map

    def tearDown(self):
        ap._layers_map = self._orig_layers_map
        self._td.cleanup()

    def _check(self, layers):
        ap._layers_map = lambda: (layers, "")
        return _run(ap.check_test_layering, BASE_DIR=self.tmp)

    def test_four_rings_intact_passes(self):
        c = self._check({"test_a.py": "fast"})
        self.assertEqual(c.status, ap.PASS, c.detail)

    def test_unregistered_test_warns(self):
        c = self._check({})
        self.assertEqual(c.status, ap.WARN, "D-06 验收：新增文件未登记必须 WARN")
        self.assertIn("test_a.py", c.detail)
        self.assertIn("未登记", c.detail)

    def test_stale_entry_warns(self):
        c = self._check({"test_a.py": "fast", "test_ghost.py": "slow"})
        self.assertEqual(c.status, ap.WARN)
        self.assertIn("死条目", c.detail)
        self.assertIn("test_ghost.py", c.detail)

    def test_conftest_unwired_warns(self):
        (self.tmp / "tests" / "conftest.py").write_text(
            "# layer_of 接线被删\n", encoding="utf-8")
        c = self._check({"test_a.py": "fast"})
        self.assertEqual(c.status, ap.WARN, "接线断 = marker 空转态")
        self.assertIn("conftest", c.detail)

    def test_markers_missing_warns(self):
        (self.tmp / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        c = self._check({"test_a.py": "fast"})
        self.assertEqual(c.status, ap.WARN)
        self.assertIn("markers", c.detail)

    def test_layers_unimportable_warns(self):
        ap._layers_map = lambda: (None, "ImportError: x")
        c = _run(ap.check_test_layering, BASE_DIR=self.tmp)
        self.assertEqual(c.status, ap.WARN, "事实源不可读 ⇒ WARN 不猜")
        self.assertIn("不可导入", c.detail)


# ---------------------------------------------------------------- D-07 P0-2

class TestHoldingsPrivacyExtended(unittest.TestCase):
    """.env 变体 + holdings*.json + config 禁键；*.example 白名单。"""

    def _check(self, tracked, cfg=None):
        return _run(ap.check_holdings_privacy,
                    git_lines=lambda *a: (list(tracked), ""),
                    _config=lambda: (cfg if cfg is not None else {}))

    def test_clean_repo_passes(self):
        c = self._check(["run.py", ".env.example", "holdings.example.json",
                         "experiments/forecast_lab/holdings_hardened.py"])
        self.assertEqual(c.status, ap.PASS, c.detail)
        self.assertEqual(c.cid, "P0-2")

    def test_env_variants_fail(self):
        for t in (".env", ".env.local", "secrets/.env.prod"):
            c = self._check(["run.py", t])
            self.assertEqual(c.status, ap.FAIL, f"{t} 必须 FAIL")
            self.assertIn(t, c.detail)

    def test_holdings_rename_fail(self):
        c = self._check(["data/holdings_real.json"])
        self.assertEqual(c.status, ap.FAIL, "换名入库（holdings*.json）必须抓到")

    def test_holdings_script_not_false_positive(self):
        # .py 处理脚本不是持仓数据文件——只匹配 *.json，不误伤代码
        c = self._check(["experiments/forecast_lab/holdings_hardened.py"])
        self.assertEqual(c.status, ap.PASS, c.detail)

    def test_config_forbidden_key_fails_and_hides_value(self):
        cfg = {"forecast": {"holdings": {"002112": 1234.5}}}
        c = self._check(["config.json"], cfg=cfg)
        self.assertEqual(c.status, ap.FAIL, "D-07 验收：config 新增 holdings ⇒ FAIL")
        self.assertIn("config.forecast.holdings", c.detail)
        self.assertNotIn("002112", c.detail, "禁键的键名可报，值不得写入 detail")
        self.assertNotIn("1234.5", c.detail)

    def test_config_fund_codes_key_fails(self):
        c = self._check([], cfg={"fund_codes": ["002112"]})
        self.assertEqual(c.status, ap.FAIL)
        self.assertIn("config.fund_codes", c.detail)

    def test_config_fund_pool_allowed(self):
        # fund_pool 是公开研究论域（掩码基准表），明确不在禁列
        c = self._check([], cfg={"fund_pool": ["002112", "025687"]})
        self.assertEqual(c.status, ap.PASS, c.detail)

    def test_git_unavailable_warns(self):
        c = _run(ap.check_holdings_privacy,
                 git_lines=lambda *a: (None, "git not found"),
                 _config=lambda: {})
        self.assertEqual(c.status, ap.WARN, "查不到 ≠ 干净（fail-open 禁令）")


# ---------------------------------------------------------------- D-02 P1-13

class TestPromotionGateConsistency(unittest.TestCase):
    """stored × derived 四象限 + 缺位分支（derive 由注入桩控制，零 core 依赖）。"""

    def _check(self, stored, derived, err="", active="v3.pkl", models=None):
        models = models if models is not None else {
            "v3.pkl": {"promotion": {"status": stored},
                       "validation": {"decision": "approved"}}}
        return _run(ap.check_promotion_gate_consistency,
                    _config=lambda: {"forecast": {"active_model": active}},
                    _models=lambda: models,
                    _derive_status_or_none=lambda v: (derived, err))

    def test_consistent_passes(self):
        c = self._check("blocked", "blocked")
        self.assertEqual(c.status, ap.PASS, c.detail)
        self.assertEqual(c.cid, "P1-13")

    def test_hand_filled_approved_fails(self):
        c = self._check("approved", "blocked_power")
        self.assertEqual(c.status, ap.FAIL, "D-02 验收：手填 approved ⇒ FAIL")
        self.assertIn("promotion_evidence_inconsistent", c.detail)

    def test_missed_upgrade_warns(self):
        c = self._check("pending", "approved")
        self.assertEqual(c.status, ap.WARN, "D-02 验收：漏升级 ⇒ WARN")
        self.assertIn("漏升级", c.detail)

    def test_drift_both_non_approved_warns(self):
        c = self._check("blocked", "research_only")
        self.assertEqual(c.status, ap.WARN)
        self.assertIn("漂移", c.detail)

    def test_derive_unavailable_warns(self):
        c = self._check("approved", None, err="core.model_registry 不可导入：X")
        self.assertEqual(c.status, ap.WARN, "不可核验 ≠ 一致")
        self.assertIn("不可核验", c.detail)

    def test_active_unregistered_warns(self):
        c = self._check("approved", "approved", active="ghost.pkl")
        self.assertEqual(c.status, ap.WARN)
        self.assertIn("未登记", c.detail)

    def test_real_derive_path_smoke(self):
        """不注入 _derive_status_or_none：走真实惰性 import（core.model_registry
        在 venv 可用；2026-09-28 socket 守卫已实证 import 零网络）。"""
        models = {"v3.pkl": {"promotion": {"status": "pending"},
                             "validation": None}}
        c = _run(ap.check_promotion_gate_consistency,
                 _config=lambda: {"forecast": {"active_model": "v3.pkl"}},
                 _models=lambda: models)
        self.assertEqual(c.status, ap.PASS,
                         "validation=None ⇒ derive=pending，与 stored 一致")


# ---------------------------------------------------------------- D-09 P1-14

class TestPreregGrantHygiene(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.prereg = self.tmp / "promotion_prereg.json"

    def tearDown(self):
        self._td.cleanup()

    def _check(self, doc=None, exists=True):
        if exists:
            self.prereg.write_text(
                json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return _run(ap.check_prereg_grants, PREREG=self.prereg)

    def _doc(self, expiry):
        return {"enabled": True,
                "grants": {"forecast_v3.pkl": {"expiry": expiry}}}

    def test_valid_grant_passes(self):
        c = self._check(self._doc("2099-12-31"))
        self.assertEqual(c.status, ap.PASS, c.detail)
        self.assertEqual(c.cid, "P1-14")

    def test_expired_grant_warns(self):
        c = self._check(self._doc("2020-01-01"))
        self.assertEqual(c.status, ap.WARN, "D-09 验收：过期未清理 ⇒ WARN")
        self.assertIn("过期未清理", c.detail)

    def test_unparsable_expiry_warns(self):
        c = self._check(self._doc("soon"))
        self.assertEqual(c.status, ap.WARN)
        self.assertIn("不可解析", c.detail)

    def test_missing_file_passes(self):
        c = self._check(exists=False)
        self.assertEqual(c.status, ap.PASS, "无登记 = 降级通道关闭，非异常")

    def test_broken_json_warns(self):
        self.prereg.write_text("{not json", encoding="utf-8")
        c = _run(ap.check_prereg_grants, PREREG=self.prereg)
        self.assertEqual(c.status, ap.WARN)

    def test_grants_not_mapping_warns(self):
        c = self._check({"enabled": True, "grants": ["x"]})
        self.assertEqual(c.status, ap.WARN)
        self.assertIn("fail-closed", c.detail)


# ---------------------------------------------------------------- D-04 P1-5

class TestShadowChannelsAST(unittest.TestCase):
    """AST 结构判据：读 CHANNELS 常量元组与函数定义，不 import shadow_policy。

    D-04（2026-09-29 裁决选项 2）。核心回归：**通道名只出现在注释/字符串里**
    时旧子串匹配会假 PASS，新判据必须 FAIL——这是 A 提出 D-04 的原点。
    """

    REAL = "CHANNELS = ('approved_full', 'prereg_degraded', 'legacy_invalid')\n"
    LOADER = "def load_records_by_channel(path):\n    return {}\n"

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.src = self.tmp / "shadow_policy.py"

    def tearDown(self):
        self._td.cleanup()

    def _check(self):
        return _run(ap.check_shadow_channels, BASE_DIR=self.tmp)

    def test_real_repo_passes(self):
        """真仓 shadow_policy.py：判据必须 PASS（不得因改写引入误报）。"""
        c = _run(ap.check_shadow_channels)
        self.assertEqual(c.status, ap.PASS, c.detail)
        self.assertEqual(c.cid, "P1-5")
        self.assertIn("approved_full", c.detail)

    def test_wellformed_passes(self):
        self.src.write_text(self.REAL + self.LOADER, encoding="utf-8")
        c = self._check()
        self.assertEqual(c.status, ap.PASS, c.detail)

    def test_comment_only_fakes_no_longer_pass(self):
        """D-04 验收核心：通道名仅在注释/docstring ⇒ FAIL（旧判据会假 PASS）。"""
        self.src.write_text(
            '"""影子记录器。\n'
            "通道有 approved_full / prereg_degraded / legacy_invalid 三条。\n"
            '"""\n'
            "# 另有 load_records_by_channel 负责分桶\n"
            "X = 1\n", encoding="utf-8")
        c = self._check()
        self.assertEqual(c.status, ap.FAIL,
                         "通道定义缺失却因注释命中而 PASS = 假 PASS，必须堵死")
        self.assertIn("CHANNELS", c.detail)

    def test_missing_channel_fails(self):
        self.src.write_text(
            "CHANNELS = ('approved_full', 'legacy_invalid')\n" + self.LOADER,
            encoding="utf-8")
        c = self._check()
        self.assertEqual(c.status, ap.FAIL)
        self.assertIn("缺通道", c.detail)
        self.assertIn("prereg_degraded", c.detail)

    def test_extra_channel_fails_and_asks_sync(self):
        """多出通道 = 两侧镜像漂移（audit classify() 会判成 unclassified）。"""
        self.src.write_text(
            "CHANNELS = ('approved_full', 'prereg_degraded', 'legacy_invalid',"
            " 'new_channel')\n" + self.LOADER, encoding="utf-8")
        c = self._check()
        self.assertEqual(c.status, ap.FAIL)
        self.assertIn("new_channel", c.detail)
        self.assertIn("EXPECTED_CHANNELS", c.detail)

    def test_dynamic_channels_fails(self):
        """动态构造（非字面量）⇒ 判据读不出定义，按漂移 FAIL 不猜。"""
        self.src.write_text(
            "CHANNELS = tuple(ch for ch in _discover())\n" + self.LOADER,
            encoding="utf-8")
        c = self._check()
        self.assertEqual(c.status, ap.FAIL)
        self.assertIn("非字面量", c.detail)

    def test_loader_missing_fails(self):
        self.src.write_text(self.REAL, encoding="utf-8")
        c = self._check()
        self.assertEqual(c.status, ap.FAIL)
        self.assertIn("load_records_by_channel", c.detail)

    def test_function_local_channels_not_counted(self):
        """函数内局部 CHANNELS 不是模块级定义 ⇒ FAIL（判据只看模块级）。"""
        self.src.write_text(
            "def f():\n"
            "    CHANNELS = ('approved_full', 'prereg_degraded', 'legacy_invalid')\n"
            "    return CHANNELS\n" + self.LOADER, encoding="utf-8")
        c = self._check()
        self.assertEqual(c.status, ap.FAIL)

    def test_file_missing_warns(self):
        c = self._check()
        self.assertEqual(c.status, ap.WARN)
        self.assertIn("未找到", c.detail)

    def test_unparsable_warns_not_fails(self):
        self.src.write_text("def f(:\n", encoding="utf-8")
        c = self._check()
        self.assertEqual(c.status, ap.WARN, "不可核验 ≠ 漂移：WARN 不 FAIL")
        self.assertIn("不可解析", c.detail)

    def test_no_heavy_import_side_effect(self):
        """裁决依据留痕：判据不得把 shadow_policy 拉进 sys.modules（18 倍代价）。"""
        self.src.write_text(self.REAL + self.LOADER, encoding="utf-8")
        self.assertNotIn("shadow_policy", sys.modules)
        self._check()
        self.assertNotIn("shadow_policy", sys.modules,
                         "P1-5 必须零依赖：import shadow_policy 实测 9.88s/1245 模块")


# ---------------------------------------------------------------- D3-07 P1-2

class TestP12ArchivedDisposition(unittest.TestCase):
    """D3-07（2026-09-30，Summer 裁决选项 2）：P1-2 的 archived 分档。

    A 单只读核实的事实：旧实现的 active/历史分档**只覆盖「缺字段」分支**，
    「报告原件缺失 / sha 不符」是无条件 ``bad.append`` ⇒ registry 里两条
    08-30/08-31 绑定（原件在本地与 git 历史中都不存在）让 P1-2 常年 FAIL。
    FAIL 常态化会掩蔽未来真失败，故引入 ``validation.archived`` 分档。

    本类钉死六件事（全程注入，零网络、不碰真实 data/——fast 层惯例）：
      ① 未打标的原件缺失 / sha 不符 ⇒ **仍 FAIL**（删标记混回去即恢复红灯）；
      ② archived=true 的缺失 / sha 不符 ⇒ WARN，detail 含「已处置档案」；
      ③ detail 分别给出已处置（WARN）与未处置（FAIL）计数，两档可独立核对；
      ④ ``archived`` 严格 True：字符串 "true" / 1 等类型污染不生效（仍 FAIL）；
      ⑤ **语义红线**：active 条目打 archived ⇒ P1-2 也降 WARN（A 单如实指出的
         代价），但 P1-13 的 derive_promotion 推导**不受扰**——标记不得成为
         绕过授权门可见性的通道；
      ⑥ 已核验计数 n 只统计「字段齐全」的条目（与旧行为一致）。
    """

    REPORT = "output/_p12_probe_report.log"
    REPORT_SHA = "ab" * 32

    def _models(self, **over):
        """一条 legacy 绑定条目（decision=rejected + metrics，同真实 registry 形态）。"""
        v = {"report_file": self.REPORT, "report_sha256": self.REPORT_SHA,
             "decision": "rejected",
             "metrics": {"1": {"rank_ic": 0.005, "ric_ci": [-0.066, 0.07],
                               "decision": "rejected"}}}
        v.update(over.pop("validation_extra", {}))
        entry = {"path": "data/models/probe.pkl", "sha256": "cd" * 32,
                 "validation": v,
                 "promotion": {"status": "blocked", "reason": "CI 跨零"}}
        entry.update(over.pop("entry_extra", {}))
        models = {"probe.pkl": entry}
        models.update(over.pop("extra_models", {}))
        return models

    def _p12(self, models, active="probe.pkl", report_exists=True,
             report_sha=None):
        tmp = Path(tempfile.mkdtemp())
        try:
            out = tmp / "output"
            out.mkdir()
            if report_exists:
                (out / "_p12_probe_report.log").write_bytes(
                    (report_sha or "irrelevant").encode("utf-8"))
            return _run(ap.check_validation_binding,
                        BASE_DIR=tmp,
                        _models=lambda: models,
                        _config=lambda: {"forecast": {"active_model": active}})
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    # ---- ① 未打标仍 FAIL ----
    def test_missing_report_without_marker_fails(self):
        c = self._p12(self._models(), report_exists=False)
        self.assertEqual(c.cid, "P1-2")
        self.assertEqual(c.status, ap.FAIL, c.detail)
        self.assertIn("报告缺失", c.detail)

    def test_sha_mismatch_without_marker_fails(self):
        c = self._p12(self._models(), report_exists=True,
                      report_sha="not-the-registered-bytes")
        self.assertEqual(c.status, ap.FAIL, c.detail)
        self.assertIn("sha256 不符", c.detail)

    # ---- ② archived ⇒ WARN + 判词 ----
    def test_archived_missing_report_warns_with_note(self):
        c = self._p12(self._models(validation_extra={"archived": True}),
                      report_exists=False)
        self.assertEqual(c.status, ap.WARN, c.detail)
        self.assertIn("已处置档案", c.detail)
        self.assertIn("报告缺失", c.detail)

    def test_archived_sha_mismatch_warns(self):
        c = self._p12(self._models(validation_extra={"archived": True}),
                      report_exists=True, report_sha="other-bytes")
        self.assertEqual(c.status, ap.WARN, c.detail)
        self.assertIn("已处置档案", c.detail)

    # ---- ③ 两档计数并存 ----
    def test_detail_separates_disposed_and_undisposed(self):
        """一条已处置 + 一条未处置 ⇒ FAIL（未处置优先）且两档计数都可见。"""
        models = self._models(validation_extra={"archived": True})
        other = {"path": "data/models/other.pkl", "sha256": "ef" * 32,
                 "validation": {"report_file": "output/_gone.log",
                                "report_sha256": "11" * 32,
                                "decision": "rejected"},
                 "promotion": {"status": "blocked", "reason": "x"}}
        models["other.pkl"] = other
        c = self._p12(models, active="probe.pkl", report_exists=False)
        self.assertEqual(c.status, ap.FAIL, c.detail)
        self.assertIn("已处置档案 1 条", c.detail)
        self.assertIn("未处置 1 条", c.detail)

    def test_all_disposed_gives_warn_not_fail(self):
        models = self._models(validation_extra={"archived": True})
        models["other.pkl"] = {
            "path": "data/models/other.pkl", "sha256": "ef" * 32,
            "validation": {"report_file": "output/_gone2.log",
                           "report_sha256": "11" * 32, "decision": "rejected",
                           "archived": True},
            "promotion": {"status": "blocked", "reason": "x"}}
        c = self._p12(models, report_exists=False)
        self.assertEqual(c.status, ap.WARN, c.detail)
        self.assertIn("已处置档案 2 条", c.detail)
        self.assertNotIn("未处置", c.detail)

    # ---- ④ 类型污染不生效 ----
    def test_archived_type_confusion_still_fails(self):
        for bad in ("true", 1, [True], {}):
            with self.subTest(archived=bad):
                c = self._p12(self._models(validation_extra={"archived": bad}),
                              report_exists=False)
                self.assertEqual(c.status, ap.FAIL,
                                 f"archived={bad!r} 不得被当成 True（严格判据）")

    # ---- ⑤ 语义红线：active 打 archived 不扰动 P1-13 ----
    def test_active_archived_p1_2_warns(self):
        """如实钉住 A 单指出的代价：active 条目的 archived 也降 WARN。

        这正是「打标权仅限 Summer 授权批次」红线的由来——审计侧不再对 active
        原件在档做强制 FAIL，强制力转移给打标授权纪律（见 check_validation_binding
        docstring 与 evidence/INDEX.md 拍板记录）。
        """
        c = self._p12(self._models(validation_extra={"archived": True}),
                      active="probe.pkl", report_exists=False)
        self.assertEqual(c.status, ap.WARN, c.detail)

    def test_archived_marker_does_not_disturb_p1_13_derivation(self):
        """active 条目带 archived ⇒ P1-13 的 derive_promotion 推导照常（blocked）。

        防标记被滥用为绕过 P1-13 可见性的通道：archived 只降 P1-2 的证据缺口噪音，
        不参与任何 promotion 裁决。
        """
        models = self._models(validation_extra={
            "archived": True, "archived_at": "2026-09-30",
            "archived_authorized_by": "Summer"})
        c = _run(ap.check_promotion_gate_consistency,
                 _models=lambda: models,
                 _config=lambda: {"forecast": {"active_model": "probe.pkl",
                                               "model_ready": False}})
        self.assertEqual(c.cid, "P1-13")
        self.assertEqual(c.status, ap.PASS, c.detail)
        self.assertIn("derived=blocked", c.detail)

    # ---- ⑥ 计数口径不变 ----
    def test_field_missing_entries_not_counted_as_verified(self):
        """缺 report_file 的条目走档案分支，不计入「已核验」n（旧行为保留）。"""
        models = self._models()
        models["nofile.pkl"] = {"path": "data/models/nofile.pkl",
                                "sha256": "99" * 32,
                                "validation": {"decision": "rejected"},
                                "promotion": {"status": "blocked", "reason": "x"}}
        c = self._p12(models, active="probe.pkl", report_exists=False)
        self.assertIn("1 份报告已核验", c.detail)
        self.assertIn("nofile.pkl 缺 report_file+report_sha256（档案）", c.detail)

    def test_clean_binding_still_passes(self):
        """正例回归：原件在档且 sha 相符 ⇒ PASS（本批不得松动正常路径）。"""
        import hashlib
        body = b"registered-report-bytes"
        sha = hashlib.sha256(body).hexdigest()
        models = self._models(validation_extra={"report_sha256": sha})
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "output").mkdir()
            (tmp / "output" / "_p12_probe_report.log").write_bytes(body)
            c = _run(ap.check_validation_binding,
                     BASE_DIR=tmp, _models=lambda: models,
                     _config=lambda: {"forecast": {"active_model": "probe.pkl"}})
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(c.status, ap.PASS, c.detail)
        self.assertIn("1 份报告已核验", c.detail)


# ---------------------------------------------------------------- 接线与排序

class TestWiring(unittest.TestCase):
    def test_run_audit_includes_new_checks(self):
        """P1-13 / P1-14 必须接进 run_audit（问题单落点，不只是函数存在）。"""
        import inspect
        src = inspect.getsource(ap.run_audit)
        self.assertIn("check_promotion_gate_consistency", src)
        self.assertIn("check_prereg_grants", src)

    def test_sorted_checks_orders_new_cids(self):
        a = ap.Audit()
        a.add("P1-14", "P1-证据链", "t", ap.PASS)
        a.add("P1-13", "P1-证据链", "t", ap.PASS)
        a.add("P0-2", "P0-研究有效性", "t", ap.PASS)
        order = [c.cid for c in a.sorted_checks()]
        self.assertEqual(order, ["P0-2", "P1-13", "P1-14"])


if __name__ == "__main__":
    unittest.main()
