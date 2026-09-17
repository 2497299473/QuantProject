"""步 5 报告层 source trace 测例（2026-09-17）——纯内存，零网络不碰 data/。

覆盖（对齐重构方案 §四步 5 验收「飞书推送显示实际来源」）：
1. _data_source_notice：eastmoney 主源 fresh 不提示；回落到 sina 出「数据源切换」；
   cache / cache:fallback 旧行为回归锁定；cache:fallback + 换源同时出现时两行并存。
2. signal_engine.compute_signals：透传 source 附加键；无键时为空串（cache 态语义）。
3. data_loader.load_fund：cache / cache:fallback 命中时剥除 stale source 键
   （缓存文件里的 source 是上次取数的，对本次加载已失真）。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import data_loader  # noqa: E402
from core.report_generator import _data_source_notice  # noqa: E402
from core.signal_engine import compute_signals  # noqa: E402


def _sig(**kw):
    s = {"last_nav_date": "2026-09-17", "_source": "fresh", "source": "eastmoney"}
    s.update(kw)
    return s


class TestDataSourceNotice(unittest.TestCase):
    def test_main_source_fresh_no_notice(self):
        self.assertEqual(_data_source_notice({"002112": _sig()}), "")

    def test_sina_switch_shows_notice(self):
        out = _data_source_notice({"002112": _sig(source="sina")})
        self.assertIn("数据源切换", out)
        self.assertIn("002112（实际来源 sina，东财主源本次未取到）", out)

    def test_cache_and_fallback_regressions(self):
        out = _data_source_notice({
            "002112": _sig(_source="cache"),
            "025687": _sig(_source="cache:fallback(boom)"),
        })
        self.assertIn("本地缓存复用", out)
        self.assertIn("数据降级告警", out)
        # 三态分支优先：cache:fallback 即使带 source 键也不出「切换」（stale 已剥除）
        self.assertNotIn("数据源切换", _data_source_notice(
            {"002112": _sig(_source="cache", source="sina")}))

    def test_no_source_key_tolerated(self):
        # 旧信号 dict（无 source 键）不得 KeyError
        s = _sig()
        s.pop("source")
        self.assertEqual(_data_source_notice({"002112": s}), "")

    def test_source_trace_notes_single_source_of_truth(self):
        from core.report_generator import source_trace_notes
        self.assertEqual(source_trace_notes({"002112": _sig()}), [])
        self.assertEqual(len(source_trace_notes({"002112": _sig(source="sina")})), 1)
        # cache 态不参与（stale 已剥除）
        self.assertEqual(source_trace_notes(
            {"002112": _sig(_source="cache", source="sina")}), [])

    def test_feishu_card_shows_switch(self):
        # 验收「飞书推送显示实际来源」：卡片 fields 里必须出现数据源切换行
        from core import notify
        account = {"positions": [], "total_market_value": 0, "total_pnl_pct": 0,
                   "abnormal_alerts": [], "as_of_nav_date": "2026-09-17"}
        card = notify._build_card("post", {"002112": dict(
            _sig(source="sina"), name="n", score=0, stance="中性",
            insufficient_history=False, last_nav=1.0)}, account)
        texts = json.dumps(card, ensure_ascii=False)
        self.assertIn("数据源切换", texts)
        self.assertIn("实际来源 sina", texts)
        # 主源时不出现
        card2 = notify._build_card("post", {"002112": dict(
            _sig(), name="n", score=0, stance="中性",
            insufficient_history=False, last_nav=1.0)}, account)
        self.assertNotIn("数据源切换", json.dumps(card2, ensure_ascii=False))


class TestSignalEnginePassthrough(unittest.TestCase):
    def _fund(self, **extra):
        navs = [[f"2026-{i//20:02d}-{i%20+1:02d}", 1.0 + i * 0.001]
                for i in range(150)]
        return {"name": "测试", "navs": navs, **extra}

    def test_source_key_transferred(self):
        funds = {"002112": self._fund(_source="fresh", source="sina")}
        with mock.patch.object(sys.stdout, "write", lambda *_: None):
            r = compute_signals(funds)
        self.assertEqual(r["002112"]["_source"], "fresh")
        self.assertEqual(r["002112"]["source"], "sina")

    def test_missing_source_defaults_empty(self):
        funds = {"002112": self._fund(_source="cache")}
        with mock.patch.object(sys.stdout, "write", lambda *_: None):
            r = compute_signals(funds)
        self.assertEqual(r["002112"]["source"], "")


class TestLoadFundStripsStaleSource(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._orig = data_loader._cache_path
        data_loader._cache_path = lambda code: tmp / f"{code}.json"
        # 造一份「上次经 sina 取的」缓存：fresh 态 source=sina 已落进缓存文件
        (tmp / "002112.json").write_text(json.dumps({
            "code": "002112", "name": "n", "navs": [["2026-09-16", 1.0]],
            "_source": "fresh", "source": "sina"}), encoding="utf-8")

    def tearDown(self):
        data_loader._cache_path = self._orig
        self._tmp.cleanup()

    def test_cache_hit_strips_source(self):
        fund = data_loader.load_fund("002112")
        self.assertEqual(fund["_source"], "cache")
        self.assertNotIn("source", fund)

    def test_fallback_strips_source(self):
        # force_refresh=True 跳过 TTL 命中，逼代码走「取数失败 → cache:fallback」分支
        with mock.patch.object(data_loader, "_fetch_fund_nav",
                               side_effect=RuntimeError("all dead")):
            fund = data_loader.load_fund("002112", force_refresh=True)
        self.assertTrue(fund["_source"].startswith("cache:fallback"))
        self.assertNotIn("source", fund)


if __name__ == "__main__":
    unittest.main(verbosity=2)
