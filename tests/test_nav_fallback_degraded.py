"""V4.1 ③（2026-09-18）：`cache:fallback` 必须进入 Runtime Degraded 状态机。

背景：`core/data_loader.py` 定义 `_source` 三态，其中 `cache:fallback` 的语义是
「**全链失败**后退回旧缓存」。关键点：这种降级下 `load_fund()` **正常返回、不抛
异常**。而旧 `run.py` 只用 `try/except` 兜数据获取失败，于是存在一条漏网路径：

    EastMoney 失败 → Sina 失败 → 旧缓存 fallback → load_fund 成功返回
    → run.py 不记 degraded → exit=0 → publish_gate 看不见污染

与 V4 契约「数据降级 → DEGRADED → 必要时阻止发布」不一致。修复后该事实由
`data_loader.is_nav_fallback()` 单一事实源表达，run.py / run_manifest /
publish_gate / 报告层共享同一判定。

全程 tempdir + 内存结构，零网络，不碰真实 data/ 与 output/。
"""
import json
import re
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

NOW = datetime(2026, 9, 18, 14, 56, 0)
POOL = ["002112", "025687"]      # V4.1 ④ 别名基准表（升序 → 002112=F1、025687=F2）


class TestFallbackPredicate(unittest.TestCase):
    """谓词本体：三态里只有 cache:fallback 算降级。"""

    def test_fresh_is_not_degraded(self):
        self.assertFalse(
            data_loader.is_nav_fallback({"_source": "fresh", "source": "eastmoney"}))

    def test_sina_fresh_is_not_degraded(self):
        # 换源成功仍是 fresh（V4 明确"不要再改"的 SourceRegistry 链），不得误判
        self.assertFalse(
            data_loader.is_nav_fallback({"_source": "fresh", "source": "sina"}))

    def test_ttl_cache_is_not_degraded(self):
        self.assertFalse(data_loader.is_nav_fallback({"_source": "cache"}))

    def test_fallback_is_degraded(self):
        self.assertTrue(
            data_loader.is_nav_fallback({"_source": "cache:fallback(network_error)"}))

    def test_missing_source_key_is_not_degraded(self):
        self.assertFalse(data_loader.is_nav_fallback({}))
        self.assertFalse(data_loader.is_nav_fallback(None))

    def test_prefix_constant_matches_generation_literal(self):
        # 生成侧与判定侧同源守护：常量若与 data_loader 写 _source 的字面量漂移，
        # 判定会静默失效（run/gate 双双瞎眼），必须炸在测试里而不是生产里。
        src = (BASE_DIR / "core" / "data_loader.py").read_text(encoding="utf-8")
        m = re.search(r'fallback\["_source"\] = f"([^"]*)\{e\}', src)
        self.assertIsNotNone(m, "找不到 cache:fallback 生成点，本守护需同步重写")
        self.assertTrue(m.group(1).startswith(data_loader.NAV_FALLBACK_PREFIX),
                        f"生成侧 {m.group(1)!r} 与前缀常量 "
                        f"{data_loader.NAV_FALLBACK_PREFIX!r} 漂移")


class TestNavFallbackFunds(unittest.TestCase):
    def test_filters_only_fallback(self):
        funds = {
            "002112": {"_source": "fresh"},
            "025687": {"_source": "cache:fallback(EastMoney+HTTP 500)"},
            "022853": {"_source": "cache"},
            "002207": {"_source": "cache:fallback(Sina:empty)"},
        }
        self.assertEqual(run.nav_fallback_funds(funds), ["025687", "002207"])

    def test_empty_input(self):
        self.assertEqual(run.nav_fallback_funds({}), [])
        self.assertEqual(run.nav_fallback_funds(None), [])


class TestGateAttribution(unittest.TestCase):
    """降级项必须能归因到具体基金，归因不成立时 fail-closed。"""

    def test_prefix_registered_in_gate_table(self):
        self.assertEqual(ap.GATE_FUND_KEYED.get("fund_data_fallback"),
                         ("data", "fallback"))

    def test_attributed_per_fund_not_run_level(self):
        run_level, per_fund = ap.attribute_degradations(
            ["fund_data_fallback:002112"],
            {"data": {"failed": [], "fallback": ["002112"]}},
            {"002112", "025687"})
        self.assertEqual(run_level, [])
        self.assertEqual(list(per_fund), ["002112"])

    def test_unattributable_is_fail_closed_run_level(self):
        # 旧清单没有 fallback 字段、后缀又无代码 ⇒ 升运行级污染全部，不得静默放行
        run_level, per_fund = ap.attribute_degradations(
            ["fund_data_fallback"], {"data": {}}, {"002112"})
        self.assertEqual(run_level, ["fund_data_fallback"])
        self.assertEqual(per_fund, {})

    def test_out_of_universe_code_is_fail_closed(self):
        run_level, _ = ap.attribute_degradations(
            ["fund_data_fallback:999999"],
            {"data": {"fallback": ["999999"]}}, {"002112"})
        self.assertEqual(run_level, ["fund_data_fallback:999999"])


def full(failed=(), fallback=(), lt=(), rt_failed=(), rt_deg=()):
    """构造 fund_evidence_complete() 认账的完整逐基金评估结构。"""
    return {"data": {"ok": not failed, "failed": list(failed),
                     "fallback": list(fallback), "n_funds": 4},
            "lookthrough": {"ok": not lt, "missing": list(lt)},
            "realtime": {"ok": not (rt_failed or rt_deg),
                         "failed": list(rt_failed), "degraded": list(rt_deg)}}


class GateHarness(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.man_dir = self.root / "run_manifest"
        self.man_dir.mkdir()
        self._orig = (ap.RUN_MANIFEST_DIR, ap.HOLDINGS)
        ap.RUN_MANIFEST_DIR = self.man_dir
        ap.HOLDINGS = self.root / "holdings.json"
        ap.HOLDINGS.write_text(
            json.dumps({"funds": {"002112": {"shares": 100.0},
                                  "025687": {"shares": 50.0}}}), encoding="utf-8")

    def tearDown(self):
        ap.RUN_MANIFEST_DIR, ap.HOLDINGS = self._orig
        self._td.cleanup()

    def gate(self, extra):
        return ap.publish_gate(NOW, universe=("002112", "025687"), extra=extra)


class TestPublishGateBlocks(GateHarness):
    def test_fallback_blocks_publish_for_that_fund(self):
        g = self.gate([(["fund_data_fallback:002112"], full(fallback=["002112"]))])
        self.assertFalse(g["ok"], "净值退回旧缓存必须拦发布")
        self.assertEqual(g["reason"], "contaminated_funds")
        self.assertEqual(sorted(g["contaminated"]), ["002112"])
        self.assertEqual(sorted(g["eligible"]), ["025687"],
                         "污染归因到涉事基金，其余不受牵连（逐基金口径）")

    def test_clean_fresh_run_passes(self):
        g = self.gate([([], full())])
        self.assertTrue(g["ok"], g["detail"])

    def test_batch_semantics_any_contamination_blocks_all(self):
        # 整批裁决（方案 A）不得因新增降级项而变：任一只脏 ⇒ 整体不推
        g = self.gate([(["fund_data_fallback:025687"], full(fallback=["025687"]))])
        self.assertFalse(g["ok"])

    def test_fallback_is_not_in_self_lock_whitelist(self):
        # 白名单只放被门禁控制的**输出**；数据降级是输入，进去就变成假放行
        self.assertNotIn("fund_data_fallback", ap.GATE_EXCLUDED_REASONS)

    def test_disk_manifest_of_earlier_run_also_counts(self):
        """同一事实在「落盘清单」路径下同样拦得住（不只内存 extra 路径）。"""
        payload = {"run_id": "20260918_113003_mid", "slot": "mid",
                   "degraded_reasons": ["fund_data_fallback:002112"],
                   "notification": {"ok": True, "reason": None}}
        payload.update(full(fallback=["002112"]))
        (self.man_dir / "run_manifest_20260918_113003_mid.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        g = ap.publish_gate(NOW, universe=("002112", "025687"))
        self.assertFalse(g["ok"], g["detail"])


class TestManifestAndExitCode(unittest.TestCase):
    """降级事实进清单 + 退出码离开 0（BASE_DIR 换到 tempdir，不碰真实 output/）。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self._orig_base = run.BASE_DIR
        run.BASE_DIR = self.root
        run._LOG.clear()

    def tearDown(self):
        run.BASE_DIR = self._orig_base
        run._LOG.clear()
        self._td.cleanup()

    def _payload(self):
        files = sorted((self.root / "output" / "run_manifest").glob("*.json"))
        self.assertEqual(len(files), 1, f"应恰好落盘 1 份清单：{files}")
        return json.loads(files[0].read_text(encoding="utf-8"))

    def test_fallback_survives_manifest_roundtrip(self):
        ok = run._write_run_manifest(NOW, {
            "slot": "post", "status": "DEGRADED",
            "data": {"ok": True, "failed": [], "fallback": ["002112"], "n_funds": 4},
            "degraded_reasons": ["fund_data_fallback:002112"],
        }, POOL)
        self.assertTrue(ok)
        p = self._payload()
        # V4.1 ④：落盘侧只留位置别名（002112 = 基准表升序第 1 位），真实代码不进仓库
        self.assertEqual(p["data"]["fallback"], ["F1"])
        self.assertEqual(p["degraded_reasons"], ["fund_data_fallback:F1"])
        self.assertEqual(p["fund_refs"]["pool_sha256_8"], ap.fund_pool_fingerprint(POOL))
        for f in (self.root / "output" / "run_manifest").glob("*.json"):
            self.assertNotIn("002112", f.read_text(encoding="utf-8"))

    def test_finalize_exit_two_when_only_fallback(self):
        # 只有 fallback 一项降级（无异常失败）：修复前这条路径会安静地返回 0
        code = run._finalize_run(
            NOW, {"slot": "post", "status": "DEGRADED",
                  "degraded_reasons": ["fund_data_fallback:002112"]},
            ["fund_data_fallback:002112"], POOL)
        self.assertEqual(code, 2)

    def test_clean_run_exit_zero_unchanged(self):
        code = run._finalize_run(NOW, {"slot": "post", "status": "SUCCESS",
                                       "degraded_reasons": []}, [], POOL)
        self.assertEqual(code, 0)


class TestWiringGuards(unittest.TestCase):
    """源码级守护：接线不许日后被悄悄拆掉（与本仓 test_publish_gate 同风格）。"""

    def setUp(self):
        self.src = (BASE_DIR / "run.py").read_text(encoding="utf-8")

    def test_run_emits_fallback_reason(self):
        self.assertIn('"fund_data_fallback:"', self.src)
        self.assertIn("nav_fallback_funds(funds)", self.src)
        self.assertIn('"fallback": fund_fallbacks', self.src)

    def test_fallback_counts_into_degraded_before_gate(self):
        # 关键链路顺序：提取 fallback → 进 degraded → 才有 exit=2 与 gate 输入。
        # 只写清单字段不进 degraded 是半修（exit 仍 0）。
        i_take = self.src.index("fund_fallbacks = nav_fallback_funds(funds)")
        i_add = self.src.index('degraded.append("fund_data_fallback:"')
        i_gate = self.src.index("def gate_eval()")
        self.assertLess(i_take, i_add)
        self.assertLess(i_add, i_gate, "降级项必须在发布门禁评估前落入 degraded")

    def test_report_layer_shares_predicate_not_second_startswith(self):
        src = (BASE_DIR / "core" / "report_generator.py").read_text(encoding="utf-8")
        self.assertIn("data_loader.is_nav_fallback", src)
        # 判定只留一处：报告层不得再自己写 cache:fallback 前缀判定（TTL cache
        # 的轻提示分支 `== "cache"` 保留是允许的）
        self.assertNotIn('src.startswith("cache:fallback")', src)


if __name__ == "__main__":
    unittest.main()
