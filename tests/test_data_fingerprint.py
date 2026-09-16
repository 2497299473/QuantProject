"""V4-A（2026-09-16）：manifest 生成器的可追溯性 —— 上一版归档 + 原子写。

背景：shadow 每条记录内嵌 `data_manifest_sha256`（记录计算时 manifest 文件的
sha256 前 16 位）。若重生成时直接覆盖，旧版只剩 git 历史；未提交时彻底丢失，
审计无法把记录对应回当时的数据快照。实测 2026-09-16 的 12 条前瞻记录即因
重生成而一度「指向不可解析的哈希」。

本文件只测归档/原子写的纯文件行为，不重跑全量指纹（避免产生真实数据 churn）。
"""
import hashlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import data_fingerprint as df                            # noqa: E402


class TestArchivePrevious(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.out = self.tmp / "manifest.json"

    def tearDown(self):
        self._td.cleanup()

    def _write(self, generated_at, payload=b'{"generated_at": "x"}'):
        self.out.write_text(
            json.dumps({"generated_at": generated_at, "files": {}}), encoding="utf-8")

    def test_no_existing_file_returns_none(self):
        self.assertIsNone(df._archive_previous(self.out))
        self.assertFalse((self.tmp / df.HISTORY_DIRNAME).exists())

    def test_archive_preserves_bytes_verbatim(self):
        # 归档必须逐字节一致：否则 sha256 对照失效，等于没归档
        self._write("2026-08-31T09:12:53")
        original = self.out.read_bytes()
        dest = df._archive_previous(self.out)
        self.assertIsNotNone(dest)
        self.assertEqual(dest.name, "manifest_20260831T091253.json")
        self.assertEqual(dest.read_bytes(), original)
        self.assertEqual(hashlib.sha256(dest.read_bytes()).digest(),
                         hashlib.sha256(original).digest())

    def test_idempotent_when_same_version_rearchived(self):
        self._write("2026-08-31T09:12:53")
        first = df._archive_previous(self.out)
        size = first.stat().st_size
        second = df._archive_previous(self.out)       # 同版本重复归档
        self.assertEqual(first, second)
        self.assertEqual(second.stat().st_size, size)
        self.assertEqual(len(list((self.tmp / df.HISTORY_DIRNAME).glob("*.json"))), 1)

    def test_falls_back_to_mtime_stamp_on_bad_metadata(self):
        # generated_at 缺失/损坏时不得崩溃，退化为 mtime 命名
        self.out.write_text("{not json}", encoding="utf-8")
        dest = df._archive_previous(self.out)
        self.assertIsNotNone(dest)
        expected = time.strftime("%Y%m%dT%H%M%S",
                                 time.localtime(self.out.stat().st_mtime))
        self.assertEqual(dest.name, f"manifest_{expected}.json")

    def test_history_dir_excluded_from_scan(self):
        # 归档目录不得被自身扫描进去，否则 manifest 无限自我膨胀
        self.assertIn(df.HISTORY_DIRNAME, df.EXCLUDE_DIRS)
        self.assertIn("manifest.json", df.EXCLUDE_FILES)


class TestAtomicWriteContract(unittest.TestCase):
    def test_main_writes_via_replace(self):
        src = (BASE_DIR / "data_fingerprint.py").read_text(encoding="utf-8")
        self.assertIn("tmp.replace(out)", src)
        self.assertIn("_archive_previous(out)", src)


if __name__ == "__main__":
    unittest.main()
