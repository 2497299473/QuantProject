"""分层清单漂移守护（V4-A，2026-09-17）。

`tests/layers.py` 是分层唯一事实来源。本测试防止两类漂移：

1. **漏登记**：新增测试文件却没进 `LAYERS` —— 会被静默按 slow 处理，
   于是「fast 快车道」悄悄少跑一个文件而无人察觉；
2. **模块名不可加载**：`modules_for()` 产出的名字必须能被 unittest 解析，
   否则 `run_tests.py --layer fast` 会静默跳过。

只读文件系统，不执行任何被测逻辑。
"""
import ast
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from tests.layers import FAST, SLOW, LAYERS, layer_of, modules_for   # noqa: E402


class TestLayerRegistry(unittest.TestCase):
    def _test_files(self):
        return {p.name for p in (BASE_DIR / "tests").glob("test_*.py")}

    def test_every_test_file_is_registered(self):
        missing = sorted(self._test_files() - set(LAYERS))
        self.assertEqual(missing, [], f"未登记分层的测试文件：{missing}")

    def test_no_registry_entry_points_at_missing_file(self):
        ghost = sorted(set(LAYERS) - self._test_files())
        self.assertEqual(ghost, [], f"登记了不存在的测试文件：{ghost}")

    def test_every_layer_value_is_known(self):
        bad = {n: l for n, l in LAYERS.items() if l not in (FAST, SLOW)}
        self.assertEqual(bad, {}, f"未知层：{bad}")

    def test_module_names_are_importable_paths(self):
        for mod in modules_for(FAST) + modules_for(SLOW):
            self.assertTrue(mod.startswith("tests."), mod)
            path = BASE_DIR / (mod.replace(".", "/") + ".py")
            self.assertTrue(path.is_file(), f"模块名指向不存在的文件：{mod}")

    def test_unregistered_defaults_to_slow(self):
        # 未登记不得默认进 fast 快车道（宁可慢，不可漏）
        self.assertEqual(layer_of("test_not_registered_yet.py"), SLOW)

    def test_layers_are_disjoint(self):
        fast, slow = set(modules_for(FAST)), set(modules_for(SLOW))
        self.assertEqual(fast & slow, set(), "同一文件不得同时属于两层")

    def test_registry_covers_all_files_exactly_once(self):
        self.assertEqual(len(LAYERS), len(self._test_files()))


class TestMarkerDocs(unittest.TestCase):
    def test_conftest_maps_registry_to_markers(self):
        src = (BASE_DIR / "tests" / "conftest.py").read_text(encoding="utf-8")
        self.assertIn("layer_of", src)
        self.assertIn("pytest_collection_modifyitems", src)

    def test_conftest_does_not_break_unittest(self):
        # conftest.py 只在 pytest 下加载，不得在 import 期触碰 unittest 发现逻辑
        tree = ast.parse((BASE_DIR / "tests" / "conftest.py").read_text(encoding="utf-8"))
        names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        self.assertIn("pytest_collection_modifyitems", names)


if __name__ == "__main__":
    unittest.main()
