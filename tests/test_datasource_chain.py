"""数据源层骨架测例（V4 重构 · 步 1）——纯内存 fake provider，零网络、不碰 data/。

覆盖（对齐重构方案 §四步 1 验收「import 通过，旧路径行为不变」+ §四测试行）：
1. FetchResult 契约校验（成功不带 error / 失败必带原因）
2. SourceRegistry 登记 / 排序 / 重复登记拒绝 / 降级过滤
3. run_chain 三种链行为：首源成功短路、全灭留痕、协议违反兜底继续走链
4. HealthTracker 网络类 vs 确定性失败的区分计数（08-27 科创板教训的编码化）
5. source trace：attempts 逐环记录来源、成败、原因
"""
import sys
import unittest
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.datasource import (  # noqa: E402
    FetchResult, SourceRegistry, ChainResult, run_chain, HealthTracker,
)
from core.datasource.registry import DuplicateProvider  # noqa: E402


class FakeProvider:
    """可编程 fake：按脚本依次返回结果或抛异常，并计数调用次数。"""

    def __init__(self, name: str, script: list, category: str = "stock_kline",
                 priority: int = 0, timeout_s: float = 5.0) -> None:
        self.name = name
        self.category = category
        self.priority = priority
        self.timeout_s = timeout_s
        self._script = list(script)
        self.calls = 0

    def fetch(self, **params: Any) -> FetchResult:
        self.calls += 1
        item = self._script.pop(0) if self._script else FetchResult(
            ok=False, source=self.name, error="network:script exhausted")
        if isinstance(item, Exception):
            raise item
        return item


def _ok(name: str) -> FetchResult:
    return FetchResult(ok=True, source=name, payload={"data": name})


def _net_fail(name: str) -> FetchResult:
    return FetchResult(ok=False, source=name, error=f"network:{name} timeout")


def _hard_fail(name: str) -> FetchResult:
    # 确定性失败：数据本身没有（如未知代码），不是网络抖动
    return FetchResult(ok=False, source=name, error=f"data:{name} not found")


class TestFetchResultContract(unittest.TestCase):
    def test_success_must_not_carry_error(self):
        with self.assertRaises(ValueError):
            FetchResult(ok=True, source="x", error="bug")

    def test_failure_must_carry_reason(self):
        with self.assertRaises(ValueError):
            FetchResult(ok=False, source="x")

    def test_frozen(self):
        import dataclasses
        with self.assertRaises(dataclasses.FrozenInstanceError):
            _ok("x").source = "y"  # type: ignore[misc]


class TestRegistry(unittest.TestCase):
    def test_sorted_by_priority_then_name(self):
        reg = SourceRegistry()
        p2 = FakeProvider("beta", [], priority=2)
        p0 = FakeProvider("alpha", [], priority=0)
        p1 = FakeProvider("zeta", [], priority=1)
        for p in (p2, p0, p1):
            reg.register(p)
        self.assertEqual([p.name for p in reg.chain_for("stock_kline")],
                         ["alpha", "zeta", "beta"])

    def test_duplicate_name_rejected(self):
        reg = SourceRegistry()
        reg.register(FakeProvider("dup", []))
        with self.assertRaises(DuplicateProvider):
            reg.register(FakeProvider("dup", []))

    def test_same_name_different_category_ok(self):
        reg = SourceRegistry()
        reg.register(FakeProvider("tencent", [], category="stock_kline"))
        reg.register(FakeProvider("tencent", [], category="realtime_quote"))
        self.assertEqual(len(reg.chain_for("stock_kline")), 1)
        self.assertEqual(len(reg.chain_for("realtime_quote")), 1)

    def test_unknown_category_returns_empty_chain(self):
        reg = SourceRegistry()
        self.assertEqual(reg.chain_for("nope"), [])

    def test_get_missing_raises(self):
        reg = SourceRegistry()
        with self.assertRaises(KeyError):
            reg.get("stock_kline", "ghost")


class TestHealth(unittest.TestCase):
    def test_network_failures_degrade_after_threshold(self):
        h = HealthTracker(threshold=2)
        self.assertTrue(h.healthy("a"))
        h.record("a", ok=False, error="network:timeout")
        self.assertTrue(h.healthy("a"))          # 1 次，未达阈值
        h.record("a", ok=False, error="network:timeout")
        self.assertFalse(h.healthy("a"))         # 2 次，降级

    def test_deterministic_failure_never_degrades(self):
        # 08-27 科创板教训：稳定「无数据」不是网络病，不该把源冤枉踢出链
        h = HealthTracker(threshold=2)
        for _ in range(5):
            h.record("a", ok=False, error="data:not found")
        self.assertTrue(h.healthy("a"))
        self.assertEqual(h.fail_count("a"), 0)

    def test_success_resets_counter(self):
        h = HealthTracker(threshold=2)
        h.record("a", ok=False, error="network:x")
        h.record("a", ok=True)
        h.record("a", ok=False, error="network:x")
        self.assertTrue(h.healthy("a"))

    def test_registry_chain_skips_degraded(self):
        reg = SourceRegistry()
        p0 = FakeProvider("fast_fail", [], priority=0)
        p1 = FakeProvider("backup", [], priority=1)
        reg.register(p0)
        reg.register(p1)
        for _ in range(3):
            reg.health.record("fast_fail", ok=False, error="network:down")
        self.assertEqual([p.name for p in reg.chain_for("stock_kline")], ["backup"])
        self.assertEqual([p.name for p in
                          reg.chain_for("stock_kline", include_degraded=True)],
                         ["fast_fail", "backup"])

    def test_threshold_guard(self):
        with self.assertRaises(ValueError):
            HealthTracker(threshold=0)


class TestRunChain(unittest.TestCase):
    def test_first_success_short_circuits(self):
        a = FakeProvider("a", [_ok("a")])
        b = FakeProvider("b", [_ok("b")])
        r = run_chain([a, b], code="x")
        self.assertTrue(r.ok)
        self.assertEqual(r.source, "a")
        self.assertEqual(r.payload, {"data": "a"})
        self.assertEqual(b.calls, 0)                       # 没轮到它
        self.assertEqual([(at.source, at.ok) for at in r.attempts], [("a", True)])

    def test_fallback_records_source_trace(self):
        a = FakeProvider("a", [_net_fail("a")])
        b = FakeProvider("b", [_ok("b")])
        r = run_chain([a, b], code="x")
        self.assertTrue(r.ok)
        self.assertEqual(r.source, "b")
        self.assertEqual([at.source for at in r.attempts], ["a", "b"])
        self.assertIn("network:", r.attempts[0].error)

    def test_all_fail_returns_attempts(self):
        a = FakeProvider("a", [_net_fail("a")])
        b = FakeProvider("b", [_hard_fail("b")])
        r = run_chain([a, b])
        self.assertFalse(r.ok)
        self.assertIsNone(r.source)
        self.assertIsNone(r.payload)
        self.assertEqual(r.last_error, "data:b not found")
        self.assertEqual(len(r.attempts), 2)

    def test_protocol_violation_becomes_error_and_chain_continues(self):
        bad = FakeProvider("bad", [RuntimeError("provider bug")])
        good = FakeProvider("good", [_ok("good")])
        r = run_chain([bad, good])
        self.assertTrue(r.ok)
        self.assertEqual(r.source, "good")
        self.assertTrue(r.attempts[0].error.startswith("protocol:"))

    def test_empty_chain(self):
        r = run_chain([])
        self.assertFalse(r.ok)
        self.assertEqual(r.attempts, [])

    def test_health_wired_when_tracker_passed(self):
        reg = SourceRegistry()
        a = FakeProvider("a", [_net_fail("a"), _net_fail("a"), _net_fail("a")],
                         priority=0)
        reg.register(a)
        for _ in range(3):
            run_chain(reg.chain_for("stock_kline"), health=reg.health, code="x")
        self.assertFalse(reg.health.healthy("a"))
        # 降级后默认链里已无 a；include_degraded 仍可诊断
        self.assertEqual(reg.chain_for("stock_kline"), [])
        self.assertEqual([p.name for p in
                          reg.chain_for("stock_kline", include_degraded=True)], ["a"])


class TestSkeletonGuards(unittest.TestCase):
    """硬约束的回归锚点：骨架不得偷偷引入网络 / 缓存 / 副作用。"""

    def test_providers_dir_matches_migration_steps(self):
        # 步 2 起 providers/ 允许有实现；本守护防止「悄悄加源」绕过迁移步序
        pkg = BASE_DIR / "core" / "datasource" / "providers"
        mods = {p.name for p in pkg.glob("*.py")}
        self.assertEqual(mods, {"__init__.py", "stock_tencent.py",
                               "stock_eastmoney.py", "stock_tushare.py",
                               "realtime_tencent.py",
                               "fund_eastmoney.py", "fund_sina.py"},
                         "providers/ 文件集变化时请同步更新本守护与迁移步序")

    def test_provider_classes_expose_contract_attrs(self):
        # 契约字段齐备：chain/registry 依赖 name/category/priority/timeout_s
        from core.datasource.providers.stock_eastmoney import EastmoneyKlineProvider
        from core.datasource.providers.stock_tencent import TencentKlineProvider
        from core.datasource.providers.stock_tushare import TushareKlineProvider
        expected = [("tencent", 0), ("eastmoney", 1), ("tushare", 2)]
        got = [(p().name, p().priority) for p in
               (TencentKlineProvider, EastmoneyKlineProvider, TushareKlineProvider)]
        self.assertEqual(got, expected)
        for cls in (TencentKlineProvider, EastmoneyKlineProvider, TushareKlineProvider):
            inst = cls()
            self.assertEqual(inst.category, "stock_kline")
            self.assertGreater(inst.timeout_s, 0)
            self.assertTrue(callable(inst.fetch))

    def test_providers_reach_network_only_through_netutil(self):
        # 传输层单一入口：providers 只许 import ...netutil，不得自带网络客户端
        import ast
        pkg = BASE_DIR / "core" / "datasource" / "providers"
        for py in pkg.glob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            imported: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported += [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.append(node.module)
            for n in imported:
                if n.split(".")[-1] in {"netutil"}:
                    continue
                self.assertNotIn(n.split(".")[0],
                                 {"requests", "httpx", "urllib", "socket", "curl_cffi"},
                                 f"{py.name} 直接引入网络依赖 {n}：应经 ...netutil")

    def test_no_networking_imports_in_package(self):
        import ast
        for py in (BASE_DIR / "core" / "datasource").rglob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for n in names:
                    root = n.split(".")[0]
                    self.assertNotIn(
                        root, {"requests", "httpx", "urllib", "socket", "curl_cffi"},
                        f"{py.name} 引入网络依赖 {n}：骨架应零 I/O，传输归 netutil")

    def test_old_call_sites_importable(self):
        # 硬约束 1：调用面冻结——骨架合入后旧模块照常 import，行为不变
        import importlib
        for mod in ("core.data_loader", "core.stock_data", "core.real_time"):
            importlib.import_module(mod)


if __name__ == "__main__":
    unittest.main(verbosity=2)
