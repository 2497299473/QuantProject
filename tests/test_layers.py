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

    def test_no_pytest_style_suffix_naming(self):
        """面 6 D6-A-2（2026-10-01）：`*_test.py` 是三口径共同盲区。

        pytest 默认同时收集 `test_*.py` 与 `*_test.py`，但本仓 runner
        （run_tests.py）、unittest discover 与本守护的 glob 都只认 `test_*.py`。
        若有人按 pytest 习惯写 `foo_test.py`，三口径同时静默漏跑——本断言把
        该盲区变成红灯（旧路径必须死亡：不允许只靠约定）。
        """
        offenders = sorted(p.name for p in (BASE_DIR / "tests").glob("*_test.py"))
        self.assertEqual(
            offenders, [],
            f"pytest 风格命名不被本仓 runner/discover 识别，请改名为 test_*.py：{offenders}")

    def test_all_files_are_unittest_style(self):
        """面 6 D6-E-2（2026-10-01）：discover 口径一致性守护。

        三口径（run_tests.py / discover / 休眠的 pytest）加载集合一致的前提是
        所有测试都是 unittest.TestCase 风格——模块级 `def test_*` 函数与
        pytest 裸类（不继承 TestCase）discover 收不到，会静默漏跑。
        AST 判据：每个 test_*.py 至少含 1 个 TestCase 子类（基类沿本文件
        内继承链传递解析，如 _RegistrySandbox/_Base/Harness 惯例），且
        0 个模块级 test_ 函数。只读文件系统，不执行任何被测逻辑。
        """
        for path in sorted((BASE_DIR / "tests").glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            module_test_funcs = [
                n.name for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
            self.assertEqual(
                module_test_funcs, [],
                f"{path.name}：模块级 test_ 函数 unittest discover 收不到（静默漏跑），"
                f"请收进 TestCase 子类：{module_test_funcs}")
            # 本文件内的类名 → 基类名（只解析 Name/Attribute 两种形态）
            bases: dict[str, list[str]] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    bases[node.name] = [
                        b.id if isinstance(b, ast.Name) else ast.unparse(b)
                        for b in node.bases]
            roots = {"unittest.TestCase", "TestCase"}
            known = set(roots)
            changed = True
            while changed:                       # 传递闭包：Harness/_Base 链
                changed = False
                for cls, parents in bases.items():
                    if cls not in known and any(p in known for p in parents):
                        known.add(cls)
                        changed = True
            self.assertTrue(
                known - roots,
                f"{path.name}：未发现任何 unittest.TestCase 子类——discover 与 "
                f"run_tests.py 都收不到 pytest 风格裸类，测试会静默消失")


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
