"""P0-2：split_date_oos label-end purge 纪律测试（2026-08-29，GPT 三审）。

核心断言：train 样本的标签终点必须严格早于 oos_start——
T 日样本的 fwd<=5 标签窗口为 [T, T+5]，若 T+5 落在 OOS 段内，
等于训练时偷看了未来（与 OOS 评估段的信息重叠）。
"""
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from backtest_forecast import split_date_oos  # noqa: E402


def _mk_samples(dates: list[str], funds: list[str] = ("A", "B")) -> list[dict]:
    return [{"date": d, "fund": f, "fwd1": 0.01, "fwd5": 0.02} for d in dates for f in funds]


class TestLabelEndPurge(unittest.TestCase):
    def test_purge_removes_label_end_overlap(self):
        """train 尾部 5 个交易日被 purge：这些样本 label_end >= oos_start。"""
        dates = [f"2025-01-{i:02d}" for i in range(1, 11)]          # 10 个交易日
        train, oos, oos_start = split_date_oos(_mk_samples(dates),
                                               ratio=0.8, max_horizon=5)
        self.assertEqual(oos_start, "2025-01-09")
        # split_i=8 → cutoff=dates[3]=2025-01-04 → train 只含 01~03
        self.assertTrue(all(s["date"] < "2025-01-04" for s in train))
        self.assertLess(sorted({s["date"] for s in train})[-1], "2025-01-04")

    def test_purge_boundary_strict(self):
        """cutoff 边界：< cutoff 严格小于，cutoff 当日样本不入 train。"""
        dates = [f"2025-01-{i:02d}" for i in range(1, 21)]          # 20 个交易日
        train, oos, oos_start = split_date_oos(_mk_samples(dates),
                                               ratio=0.8, max_horizon=3)
        # split_i=16 → oos_start=2025-01-17 → cutoff=dates[13]=2025-01-14
        self.assertEqual(oos_start, "2025-01-17")
        self.assertTrue(all(s["date"] < "2025-01-14" for s in train))
        self.assertTrue(all(s["date"] >= "2025-01-17" for s in oos))

    def test_purge_extreme_small_pool(self):
        """极端小池子：split_i 小于 max_horizon 时 train 可为空（不崩、不泄漏）。"""
        dates = [f"2025-01-{i:02d}" for i in range(1, 11)]
        train, oos, oos_start = split_date_oos(_mk_samples(dates),
                                               ratio=0.0, max_horizon=3)
        # split_i=1 → oos_start=02 → cutoff=dates[0]=01 → train 空
        self.assertEqual(oos_start, "2025-01-02")
        self.assertEqual(train, [])
        self.assertTrue(all(s["date"] >= "2025-01-02" for s in oos))

    def test_purge_partition_complete(self):
        """train+oos 恰好 = 全样本 - purge 段（不漏不重，purge 段两端无泄漏）。"""
        dates = [f"2025-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 21)]
        samples = _mk_samples(dates)
        train, oos, oos_start = split_date_oos(samples, ratio=0.8, max_horizon=5)
        self.assertTrue(all(s["date"] < oos_start for s in train))
        self.assertTrue(all(s["date"] >= oos_start for s in oos))
        # 240 日 ×2 基金 = 480；split_i=192 → purge 5 日×2 基金=10
        self.assertEqual(len(train) + len(oos), len(samples) - 5 * 2)

    def test_train_label_end_strictly_before_oos(self):
        """纪律本质：train 最大日期 + 5 个交易日 < oos_start（purge 的语义断言）。"""
        dates = [f"2025-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 21)]
        samples = _mk_samples(dates)
        train, oos, oos_start = split_date_oos(samples, ratio=0.8, max_horizon=5)
        train_max = max(s["date"] for s in train)
        idx = dates.index(train_max)
        # train 尾部样本的 label_end 最远到 dates[idx+5]，必须仍 < oos_start
        self.assertLess(dates[idx + 5], oos_start)

    def test_empty_samples_graceful(self):
        """空样本优雅降级（2026-08-28 已有行为，回归保护）。"""
        self.assertEqual(split_date_oos([]), ([], [], None))


if __name__ == "__main__":
    unittest.main()
