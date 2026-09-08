# -*- coding: utf-8 -*-
"""Market Context 观察层专项测试（2026-09-02 P0 建立并隔离；2026-09-03 v0.1 扩展）。

设计（与 test_netutil 一致）：离线，零真实网络；全部用 fake K 线缓存。
覆盖：
- PIT：今日半根 bar 必须排除，as_of = 昨日
- r1d/r5d/r20d 计算正确性
- MA20 边界（正好等于 MA20 → above=False）
- 缺失代理（主题无代理 / 缓存缺失）跳过并记入 errors
- 单一代理失败不破坏其余主题（逐项隔离）
- primary 优先 + fallback 时才打 proxy_switch（P0 代理固定）
- original / relaxed 分层统计（P0 relaxed 隔离）
- regime 四档（risk-on / neutral / risk-off）+ 数据不足 unknown
- as_of 可重放：_theme_metrics 单点 PIT + compute 全链路（文件名/顶层 as_of/PIT 一致性）
- Data Quality v0.1：coverage / usable_for_oos / oos_quality（fallback → degraded）+
  usable_for_oos_reason 机器可读原因 + proxy_switch_themes 清单
- history.jsonl：实时 save 每日一行追加，同日不重复，历史重演不写入
"""
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

# 用可注入 data 目录的方式避免污染真实快照/缓存
import core.market_context as mc


def _make_klines(start: date, n: int, base: float = 100.0, step: float = 1.0):
    """生成 n 根 (date, close) 升序 K 线，close 单调递增，从 start 起每天 +1 日。"""
    one_day = date(2000, 1, 2) - date(2000, 1, 1)
    ds = [(start + one_day * i).strftime("%Y-%m-%d") for i in range(n)]
    closes = [base + step * i for i in range(n)]
    return list(zip(ds, closes))


class TestThemeMetrics(unittest.TestCase):
    def test_pit_excludes_today_bar(self):
        """今日（同日期）的 bar 必须排除，as_of 应为昨日且不被今日价污染。"""
        today = date.today()
        one_day = date(2000, 1, 2) - date(2000, 1, 1)
        # 26 根昨日及更早（升序），再在末尾塞一根今日半根（高价）
        rows = []
        for i in range(26):
            d = today - one_day * (26 - i)  # 从最早到昨日
            rows.append((d.strftime("%Y-%m-%d"), 100.0 + i))
        rows.append((today.strftime("%Y-%m-%d"), 999.0))  # 今日应被排除
        m = mc._theme_metrics(rows)
        self.assertIsNotNone(m)
        self.assertLess(m["as_of"], today.strftime("%Y-%m-%d"))
        # 今日 999 价不应成为 last
        self.assertNotEqual(m["r1d"], 999.0)
        self.assertLess(m.get("r1d") or 0, 50)

    def test_metrics_r1_r5_r20(self):
        start = date(2026, 1, 1)
        closes = _make_klines(start, 30, base=100.0, step=1.0)  # 100..129
        m = mc._theme_metrics(closes)
        self.assertIsNotNone(m)
        last, prev = 129.0, 128.0
        self.assertAlmostEqual(m["r1d"], (last / prev - 1) * 100, places=4)
        self.assertAlmostEqual(m["r5d"], (last / 124.0 - 1) * 100, places=4)
        self.assertAlmostEqual(m["r20d"], (last / 109.0 - 1) * 100, places=4)

    def test_ma20_boundary(self):
        closes = [(f"2026-01-{i+1:02d}", 100.0) for i in range(40)]
        m = mc._theme_metrics(closes)
        self.assertIsNotNone(m)
        # 全 100 → last == ma20 → above False
        self.assertFalse(m["above_ma20"])


class TestProxySelection(unittest.TestCase):
    """P0：primary 优先，fallback 才打 proxy_switch。用 monkeypatch 缓存目录。"""

    def setUp(self):
        self._orig_bk = mc._BK_CACHE
        self._orig_stock = mc._STOCK_CACHE
        self._orig_snap = mc.SNAP_DIR
        self._tmp = tempfile.TemporaryDirectory()
        mc._BK_CACHE = Path(self._tmp.name)
        mc._STOCK_CACHE = Path(self._tmp.name)
        mc.SNAP_DIR = Path(self._tmp.name) / "snapshots"  # P0：隔离快照，绝不污染生产 data/market_context/
        mc.SNAP_DIR.mkdir(exist_ok=True)
        # 制造单主题篮子（覆盖 primary/fallback/switch）
        self.basket = {
            "offensive": ["主题A"],
            "defensive": [],
            "proxies": {"主题A": [
                {"code": "590001", "name": "A主", "gate": "original"},
                {"code": "590002", "name": "A备", "gate": "original"},
            ]},
        }
        mc.BASKET_PATH = Path(self._tmp.name) / "basket.json"
        mc.BASKET_PATH.write_text(json.dumps(self.basket, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        mc._BK_CACHE = self._orig_bk
        mc._STOCK_CACHE = self._orig_stock
        mc.SNAP_DIR = self._orig_snap
        mc.BASKET_PATH = Path(mc.BASE_DIR) / "data" / "basket_20260902.json"
        self._tmp.cleanup()

    def _write_stock(self, code, closes):
        # 真实 fetch_stock_kline 缓存 schema：klines 是 (date, open, close, high, low, vol) 6 列
        rows = [[d, c, c, c, c, 0] for d, c in closes]
        (mc._STOCK_CACHE / f"{code}.json").write_text(
            json.dumps({"code": code, "klines": rows}), encoding="utf-8")

    def test_primary_used_when_available(self):
        closes = _make_klines(date(2026, 1, 2), 30)
        self._write_stock("590001", closes)
        self._write_stock("590002", closes)
        snap = mc.compute("test", save=False)
        ma = snap["themes"]["主题A"]
        self.assertEqual(ma["code"], "590001")
        self.assertFalse(ma["proxy_switch"])
        self.assertEqual(ma["proxy_primary"], "590001")
        # P0：save=False 不得写任何快照文件（含隔离目录）
        self.assertNotIn("saved_to", snap)
        self.assertFalse(any(Path(mc.SNAP_DIR).glob("*.json")))
        # v0.1 Data Quality：完整覆盖 + 无切换 → usable_for_oos
        q = snap["market_context_quality"]
        self.assertEqual(q["themes_expected"], 1)
        self.assertAlmostEqual(q["coverage"], 1.0)
        self.assertTrue(q["usable_for_oos"])
        self.assertEqual(q["oos_quality"], "ok")
        # 封版补充：干净日原因清单为空，顶层无切换主题
        self.assertEqual(q["usable_for_oos_reason"], [])
        self.assertIsNone(snap["proxy_switch_themes"])

    def test_fallback_used_when_primary_missing(self):
        # 只有 fallback 有缓存
        closes = _make_klines(date(2026, 1, 2), 30)
        self._write_stock("590002", closes)
        snap = mc.compute("test", save=False)
        self.assertTrue(snap["ok"])
        self.assertTrue(snap["themes"]["主题A"]["proxy_switch"])
        self.assertEqual(snap["themes"]["主题A"]["code"], "590002")
        self.assertIsNotNone(snap.get("proxy_switch_markers"))
        # v0.1：fallback 切换 → OOS 自动降级（生产观察照常，仅标记）
        q = snap["market_context_quality"]
        self.assertEqual(q["themes_switched"], 1)
        self.assertFalse(q["usable_for_oos"])
        self.assertEqual(q["oos_quality"], "degraded")
        # 封版补充：降级原因机器可读 + 切换主题清单可查
        self.assertIn("proxy_switch", q["usable_for_oos_reason"])
        self.assertEqual(snap["proxy_switch_themes"], ["主题A"])


class TestHistoryJsonl(unittest.TestCase):
    """封版补充：实时 save 追加 history.jsonl 每日一行；同日不重复；重演不写入。"""

    def setUp(self):
        self._orig_bk = mc._BK_CACHE
        self._orig_stock = mc._STOCK_CACHE
        self._orig_snap = mc.SNAP_DIR
        self._tmp = tempfile.TemporaryDirectory()
        mc._BK_CACHE = Path(self._tmp.name)
        mc._STOCK_CACHE = Path(self._tmp.name)
        mc.SNAP_DIR = Path(self._tmp.name) / "snapshots"
        mc.SNAP_DIR.mkdir(exist_ok=True)
        self.basket = {
            "offensive": ["主题A"],
            "defensive": [],
            "proxies": {"主题A": [{"code": "590001", "name": "A主", "gate": "original"}]},
        }
        mc.BASKET_PATH = Path(self._tmp.name) / "basket.json"
        mc.BASKET_PATH.write_text(json.dumps(self.basket, ensure_ascii=False), encoding="utf-8")
        rows = [[d, c, c, c, c, 0]
                for d, c in _make_klines(date(2025, 12, 20), 40)]  # 重演 01-20 前有 31 根 ≥ 21
        (mc._STOCK_CACHE / "590001.json").write_text(
            json.dumps({"code": "590001", "klines": rows}), encoding="utf-8")

    def tearDown(self):
        mc._BK_CACHE = self._orig_bk
        mc._STOCK_CACHE = self._orig_stock
        mc.SNAP_DIR = self._orig_snap
        mc.BASKET_PATH = Path(mc.BASE_DIR) / "data" / "basket_20260902.json"
        self._tmp.cleanup()

    def _hist_lines(self):
        p = Path(mc.SNAP_DIR) / "history.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

    def test_realtime_save_appends_once_per_day(self):
        snap = mc.compute("test", save=True)  # 实时（as_of_date=None）→ 追加一行
        hist = self._hist_lines()
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["date"], snap["as_of"])
        self.assertEqual(hist[0]["regime"], snap["regime"])
        self.assertEqual(hist[0]["themes_total"], 1)
        self.assertTrue(hist[0]["usable_for_oos"])
        # 同日第二次实时 save → 不重复（首算为准）
        mc.compute("test", save=True)
        self.assertEqual(len(self._hist_lines()), 1)

    def test_replay_does_not_write_history(self):
        mc.compute("test", save=True, as_of_date="2026-01-20")  # 重演 → 只落快照
        self.assertTrue((Path(mc.SNAP_DIR) / "2026-01-20.json").exists())
        self.assertEqual(self._hist_lines(), [])


class TestRegime(unittest.TestCase):
    """P1：regime 四档由纯函数 classify_regime 驱动，逐档断言。"""

    def test_risk_on(self):
        self.assertEqual(mc.classify_regime(1.0, 0.8), "risk-on")
        self.assertEqual(mc.classify_regime(0.01, 0.6), "risk-on")

    def test_risk_off(self):
        self.assertEqual(mc.classify_regime(-1.0, 0.3), "risk-off")
        self.assertEqual(mc.classify_regime(-0.01, 0.4), "risk-off")

    def test_neutral(self):
        # 其它有效组合
        self.assertEqual(mc.classify_regime(1.0, 0.5), "neutral")
        self.assertEqual(mc.classify_regime(-1.0, 0.8), "neutral")
        self.assertEqual(mc.classify_regime(0.0, 0.5), "neutral")

    def test_unknown(self):
        self.assertEqual(mc.classify_regime(None, 0.8), "unknown")
        self.assertEqual(mc.classify_regime(1.0, None), "unknown")
        self.assertEqual(mc.classify_regime(None, None), "unknown")

    def test_no_data_unknown(self):
        # 底层：无数据 → 指标 None
        self.assertIsNone(mc._theme_metrics([]))
        self.assertIsNone(mc._theme_metrics([("2026-01-01", 100.0)]))


class TestAsOfAndBreadth(unittest.TestCase):
    """P1：as_of 可重放（显式日期PIT）+ breadth 分层。"""

    def test_theme_metrics_as_of_replay(self):
        # 显式 as_of：截止 2026-01-19，但需 ≥21 根在 cutoff 以前的已完成 bar。
        # 起点 2025-12-20 → 到 2026-01-19 共 31 根，满足 min 21。
        one_day = date(2000, 1, 2) - date(2000, 1, 1)
        base = date(2025, 12, 20)
        rows = [(base + one_day * i, 100.0 + i) for i in range(40)]
        rows = [(d.strftime("%Y-%m-%d"), c) for d, c in rows]
        m = mc._theme_metrics(rows, as_of_date="2026-01-20")
        self.assertIsNotNone(m)  # cutoff 以前有 31 根已完成 bar
        self.assertEqual(m["as_of"], "2026-01-19")  # 截止上日，2026-01-20 被排除
        # 未被 2026-01-20 及之后的高价 bar 污染：
        # 2026-01-19(idx30)=130.0, 2026-01-18(idx29)=129.0 → r1d=(130/129-1)*100
        self.assertAlmostEqual(m["r1d"], (130.0 / 129.0 - 1.0) * 100, places=4)

    def test_breadth_layer_delegation(self):
        """P1：breadth 分层字段存在；v0.1 全篮子口径键名 breadth_all_above_ma20
        （all = 全部主题篮子，非仅进攻侧）。"""
        snap = {
            "breadth_all_above_ma20": 0.5,
            "breadth_original_above_ma20": 0.4,
            "breadth_relaxed_above_ma20": 0.8,
        }
        self.assertEqual(snap["breadth_all_above_ma20"], 0.5)
        self.assertEqual(snap["breadth_original_above_ma20"], 0.4)
        self.assertEqual(snap["breadth_relaxed_above_ma20"], 0.8)


class TestComputeAsOfReplay(unittest.TestCase):
    """v0.1：compute 全链路历史重演——PIT 截断、顶层 as_of、快照文件名一致。"""

    def setUp(self):
        self._orig_bk = mc._BK_CACHE
        self._orig_stock = mc._STOCK_CACHE
        self._orig_snap = mc.SNAP_DIR
        self._tmp = tempfile.TemporaryDirectory()
        mc._BK_CACHE = Path(self._tmp.name)
        mc._STOCK_CACHE = Path(self._tmp.name)
        mc.SNAP_DIR = Path(self._tmp.name) / "snapshots"
        mc.SNAP_DIR.mkdir(exist_ok=True)
        self.basket = {
            "offensive": ["主题A"],
            "defensive": [],
            "proxies": {"主题A": [{"code": "590001", "name": "A主", "gate": "original"}]},
        }
        mc.BASKET_PATH = Path(self._tmp.name) / "basket.json"
        mc.BASKET_PATH.write_text(json.dumps(self.basket, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        mc._BK_CACHE = self._orig_bk
        mc._STOCK_CACHE = self._orig_stock
        mc.SNAP_DIR = self._orig_snap
        mc.BASKET_PATH = Path(mc.BASE_DIR) / "data" / "basket_20260902.json"
        self._tmp.cleanup()

    def _write_stock(self, code, closes):
        rows = [[d, c, c, c, c, 0] for d, c in closes]
        (mc._STOCK_CACHE / f"{code}.json").write_text(
            json.dumps({"code": code, "klines": rows}), encoding="utf-8")

    def test_compute_as_of_full_replay(self):
        closes = _make_klines(date(2025, 12, 20), 40)  # 2025-12-20..2026-01-28
        self._write_stock("590001", closes)
        snap = mc.compute("test", save=True, as_of_date="2026-01-20")
        self.assertTrue(snap["ok"])
        # 顶层 as_of = 显式重演日；主题 PIT as_of = 截止上日
        self.assertEqual(snap["as_of"], "2026-01-20")
        self.assertEqual(snap["themes"]["主题A"]["as_of"], "2026-01-19")
        # 快照文件名跟随 as_of，不依赖 datetime.now()
        self.assertEqual(Path(snap["saved_to"]).name, "2026-01-20.json")
        self.assertTrue((Path(mc.SNAP_DIR) / "2026-01-20.json").exists())
        # 重演一致性：之后追加的高价 bar 不得影响 as_of=2026-01-20 的重算结果
        late = _make_klines(date(2026, 1, 21), 5, base=999.0)
        self._write_stock("590001", closes + late)
        snap2 = mc.compute("test", save=False, as_of_date="2026-01-20")
        self.assertAlmostEqual(snap2["themes"]["主题A"]["r1d"],
                               snap["themes"]["主题A"]["r1d"], places=8)
        self.assertAlmostEqual(snap2["themes"]["主题A"]["r5d"],
                               snap["themes"]["主题A"]["r5d"], places=8)
        self.assertAlmostEqual(snap2["themes"]["主题A"]["r20d"],
                               snap["themes"]["主题A"]["r20d"], places=8)


class TestStaleDetection(unittest.TestCase):
    """P0-0（2026-09-08）：缓存陈旧不再静默通过 usable_for_oos。

    背景：BK0457 无备份源（Tushare 不付费、腾讯不覆盖 BK 码），东财频控下
    陈旧是大概率常态；旧 usable 口径（coverage/n_switch/errors）只看有没有
    取到数，不看新不新，导致滞后样本直进 60 日 OOS。本组测试锁死修复行为。"""

    def setUp(self):
        self._orig_bk = mc._BK_CACHE
        self._orig_stock = mc._STOCK_CACHE
        self._orig_snap = mc.SNAP_DIR
        self._tmp = tempfile.TemporaryDirectory()
        mc._BK_CACHE = Path(self._tmp.name)
        mc._STOCK_CACHE = Path(self._tmp.name)
        mc.SNAP_DIR = Path(self._tmp.name) / "snapshots"
        mc.SNAP_DIR.mkdir(exist_ok=True)
        self.basket = {
            "offensive": ["主题A", "主题B"],
            "defensive": ["主题C"],
            "proxies": {
                "主题A": [{"code": "590001", "name": "A主", "gate": "original"}],
                "主题B": [{"code": "590002", "name": "B主", "gate": "original"}],
                "主题C": [{"code": "590003", "name": "C主", "gate": "original"}],
            },
        }
        mc.BASKET_PATH = Path(self._tmp.name) / "basket.json"
        mc.BASKET_PATH.write_text(json.dumps(self.basket, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        mc._BK_CACHE = self._orig_bk
        mc._STOCK_CACHE = self._orig_stock
        mc.SNAP_DIR = self._orig_snap
        mc.BASKET_PATH = Path(mc.BASE_DIR) / "data" / "basket_20260902.json"
        self._tmp.cleanup()

    def _write_stock(self, code, closes):
        rows = [[d, c, c, c, c, 0] for d, c in closes]
        (mc._STOCK_CACHE / f"{code}.json").write_text(
            json.dumps({"code": code, "klines": rows}), encoding="utf-8")

    def test_stale_theme_degrades_oos(self):
        # 主题A 停在 E，主题B/C 到 E+3；as_of = E+4（新鲜主题末根之后一天）
        one_day = date(2000, 1, 2) - date(2000, 1, 1)
        e = date(2026, 3, 10)
        a_closes = _make_klines(e - one_day * 29, 30)   # 末根 = e（滞后）
        fresh_closes = _make_klines(e - one_day * 26, 30)  # 末根 = e+3
        self.assertEqual(a_closes[-1][0], e.strftime("%Y-%m-%d"))
        self.assertEqual(fresh_closes[-1][0], (e + one_day * 3).strftime("%Y-%m-%d"))
        self._write_stock("590001", a_closes)
        self._write_stock("590002", fresh_closes)
        self._write_stock("590003", fresh_closes)
        snap = mc.compute("test", save=False,
                          as_of_date=(e + one_day * 4).strftime("%Y-%m-%d"))
        q = snap["market_context_quality"]
        # 主题A 滞后 3 个交易日 → stale → 降级
        self.assertEqual(snap["themes"]["主题A"]["as_of_lag"], 3)
        self.assertEqual(snap["themes"]["主题B"]["as_of_lag"], 0)
        self.assertEqual(snap["themes"]["主题C"]["as_of_lag"], 0)
        self.assertEqual(q["stale_themes"], ["主题A"])
        self.assertEqual(q["max_lag_trading_days"], 3)
        self.assertFalse(q["usable_for_oos"])
        self.assertEqual(q["oos_quality"], "degraded")
        self.assertIn("stale", q["usable_for_oos_reason"])
        # 报告层可见
        md = mc.render_section(snap)
        self.assertIn("陈旧 1 主题", md)
        self.assertIn("主题A", md)
        self.assertIn("OOS 降级", md)

    def test_fresh_all_themes_no_stale(self):
        # 三主题末根同日、as_of 紧随其后 → 无 stale，usable 保持 True
        one_day = date(2000, 1, 2) - date(2000, 1, 1)
        e = date(2026, 3, 10)
        closes = _make_klines(e - one_day * 29, 30)  # 末根 = e
        self._write_stock("590001", closes)
        self._write_stock("590002", closes)
        self._write_stock("590003", closes)
        snap = mc.compute("test", save=False,
                          as_of_date=(e + one_day).strftime("%Y-%m-%d"))
        q = snap["market_context_quality"]
        self.assertIsNone(q["stale_themes"])
        self.assertEqual(q["max_lag_trading_days"], 0)
        self.assertTrue(q["usable_for_oos"])
        self.assertEqual(q["oos_quality"], "ok")
        self.assertNotIn("陈旧", mc.render_section(snap))

    def test_all_themes_equally_stale_limitation(self):
        """已知局限（留痕）：全篮子整体同陈旧 → 参考日随数据后移，
        滞后感应不到、usable 仍 True，仅靠 calendar_gap_days 暴露供人工复核。"""
        one_day = date(2000, 1, 2) - date(2000, 1, 1)
        e = date(2026, 3, 10)
        closes = _make_klines(e - one_day * 29, 30)  # 末根 = e
        self._write_stock("590001", closes)
        self._write_stock("590002", closes)
        self._write_stock("590003", closes)
        snap = mc.compute("test", save=False,
                          as_of_date=(e + one_day * 6).strftime("%Y-%m-%d"))
        q = snap["market_context_quality"]
        self.assertIsNone(q["stale_themes"])
        self.assertTrue(q["usable_for_oos"])
        self.assertEqual(q["calendar_gap_days"], 6)
        self.assertEqual(q["data_ref_last"], e.strftime("%Y-%m-%d"))


class TestRenderRobustnessAndFallback(unittest.TestCase):
    """P1（2026-09-08）：报告健壮性 + BK→ETF fallback 切换。

    - render_section 在侧别分数缺失（None）时不崩（旧代码 :+.2f 直接炸）
    - 电网设备场景离线模拟：primary BK 缓存缺失 → 切 ETF fallback 并打 proxy_switch
    （真实链路 09-08 晚已用生产 basket 验证：BK0457 缺失时自动切 159326）"""

    def setUp(self):
        self._orig = (mc._BK_CACHE, mc._STOCK_CACHE, mc.SNAP_DIR, mc.BASKET_PATH)
        self._tmp = tempfile.TemporaryDirectory()
        mc._BK_CACHE = Path(self._tmp.name) / "bk"
        mc._STOCK_CACHE = Path(self._tmp.name) / "stk"
        mc._BK_CACHE.mkdir(parents=True)
        mc._STOCK_CACHE.mkdir(parents=True)
        mc.SNAP_DIR = Path(self._tmp.name) / "snapshots"
        mc.SNAP_DIR.mkdir()

    def tearDown(self):
        (mc._BK_CACHE, mc._STOCK_CACHE, mc.SNAP_DIR, mc.BASKET_PATH) = self._orig
        self._tmp.cleanup()

    def _write_basket(self, proxies, offensive, defensive):
        mc.BASKET_PATH = Path(self._tmp.name) / "basket.json"
        b = {"offensive": offensive, "defensive": defensive, "proxies": proxies}
        mc.BASKET_PATH.write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")

    def _write_stock(self, code, closes):
        rows = [[d, c, c, c, c, 0] for d, c in closes]
        (mc._STOCK_CACHE / f"{code}.json").write_text(
            json.dumps({"code": code, "klines": rows}), encoding="utf-8")

    def test_render_section_survives_none_side_scores(self):
        """防守代理全缺 → def5=None，render 不崩且以 — 展示（P1 None 防御）。"""
        one_day = date(2000, 1, 2) - date(2000, 1, 1)
        e = date(2026, 3, 10)
        closes = _make_klines(e - one_day * 29, 30)
        self._write_basket(
            {"进攻甲": [{"code": "590001", "name": "A", "gate": "original"}],
             "防守乙": [{"code": "590009", "name": "C", "gate": "original"}]},
            ["进攻甲"], ["防守乙"])
        self._write_stock("590001", closes)  # 防守 590009 故意无缓存
        snap = mc.compute("test", save=False,
                          as_of_date=(e + one_day).strftime("%Y-%m-%d"))
        self.assertIsNone(snap["defensive_score_5d"])
        out = mc.render_section(snap)
        self.assertIsNotNone(out)
        self.assertIn("—", out)

    def test_bk_primary_missing_switches_to_etf_fallback(self):
        """电网设备场景：BK 缓存缺失（东财封锁）→ 自动切 ETF fallback。"""
        one_day = date(2000, 1, 2) - date(2000, 1, 1)
        e = date(2026, 3, 10)
        closes = _make_klines(e - one_day * 29, 30)
        self._write_basket(
            {"电网设备": [
                {"code": "BK0457", "name": "电网设备·东财板块", "first": "2015-01-05",
                 "type": "bk_push2his"},
                {"code": "159326", "name": "电网设备ETF华夏（fallback）",
                 "first": "2024-08-29", "gate": "relaxed"},
            ]},
            ["电网设备"], [])
        self._write_stock("159326", closes)  # BK0457 缓存故意缺失
        snap = mc.compute("test", save=False,
                          as_of_date=(e + one_day).strftime("%Y-%m-%d"))
        t = snap["themes"]["电网设备"]
        self.assertEqual(t["code"], "159326")
        self.assertTrue(t["proxy_switch"])
        self.assertEqual(t["data_source"], "tencent_etf")
        self.assertEqual(t["proxy_primary"], "BK0457")
        q = snap["market_context_quality"]
        self.assertFalse(q["usable_for_oos"])
        self.assertIn("proxy_switch", q["usable_for_oos_reason"])


if __name__ == "__main__":
    unittest.main()
