"""P1-B：stock_data 缓存容错/原子写回归测试，零网络。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import stock_data
from core.datasource import FetchResult


class _FakeProvider:
    name = "fake"
    category = "stock_kline"
    priority = 0
    timeout_s = 0

    def __init__(self):
        self.calls = 0

    def fetch(self, **params):
        self.calls += 1
        return FetchResult(
            ok=True, source=self.name,
            payload={"code": params["code"], "market": params["market"],
                     "klines": [["2026-09-24", 1, 2, 2, 1]], "source": self.name})


class _FakeRegistry:
    def __init__(self, provider):
        self.provider = provider
        self.health = None

    def chain_for(self, category):
        return [self.provider]


class TestStockCache(unittest.TestCase):
    def test_corrupt_fresh_cache_falls_through_provider(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = root / "data" / "stock_klines" / "000001.json"
            cache.parent.mkdir(parents=True)
            cache.write_text("{bad json", encoding="utf-8")
            provider = _FakeProvider()
            with patch.object(stock_data, "BASE_DIR", root), \
                 patch.object(stock_data, "_registry", return_value=_FakeRegistry(provider)):
                out = stock_data.fetch_stock_kline("000001", "0")
            self.assertEqual(out["source"], "fake")
            self.assertEqual(provider.calls, 1)
            self.assertEqual(json.loads(cache.read_text(encoding="utf-8"))["source"], "fake")

    def test_success_cache_write_is_replace_style(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            provider = _FakeProvider()
            with patch.object(stock_data, "BASE_DIR", root), \
                 patch.object(stock_data, "_registry", return_value=_FakeRegistry(provider)):
                stock_data.fetch_stock_kline("000001", "0")
            cache = root / "data" / "stock_klines" / "000001.json"
            self.assertTrue(cache.is_file())
            self.assertFalse(cache.with_name(cache.name + ".tmp").exists())
            self.assertEqual(provider.calls, 1)


if __name__ == "__main__":
    unittest.main()
