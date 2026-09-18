"""daily_runs_tool 离线测例（V4 观察链修复，2026-09-18）——纯临时文件/临时库，零网络不碰 data/。

钉死两个 09-17 事故的根因防线：
1. runs：scheduler_runs.started_at 为 UTC ISO，必须按 +08:00 归一后再按北京日期过滤
   （09-17 事故：按字符串过滤漏掉 16:00 实例 → 假警报）。
2. append：只追加不重写；同名小节已存在即 SKIP（09-17 事故：读-改-写抹掉 [16:00]/[21:30]，
   [23:00] 写了两份）。
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import daily_runs_tool as tool  # noqa: E402


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE scheduler_runs (
        job_id TEXT, started_at TEXT, finished_at TEXT, success INTEGER, error TEXT)""")
    # 09-17 16:00:17 北京时间 = 08:00:17 UTC —— 按北京日 09-17 必须能查到
    conn.executemany("INSERT INTO scheduler_runs VALUES (?,?,?,?,?)", [
        ("fa6d9cdd-aaaa-bbbb-cccc-000000000001",
         "2026-09-17T08:00:17.908585+00:00", "2026-09-17T08:02:18+00:00", 1, None),
        ("93439da7-aaaa-bbbb-cccc-000000000002",
         "2026-09-17T01:30:10+00:00", "2026-09-17T01:31:26+00:00", 1, ""),
        # 北京 09-18 00:30 = UTC 09-17 16:30 → 归到 09-18，不得混进 09-17
        ("69b20a70-aaaa-bbbb-cccc-000000000003",
         "2026-09-17T16:30:00+00:00", None, 0, "boom"),
    ])
    conn.commit()
    conn.close()


class TestRunsTzFilter(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "sched.db"
        _make_db(self.db)
        self._orig = tool.DB_PATH
        tool.DB_PATH = self.db

    def tearDown(self):
        tool.DB_PATH = self._orig
        self._tmp.cleanup()

    def test_beijing_day_includes_utc_morning_rows(self):
        rows = {label: st for label, st, *_ in tool.runs_for_date("2026-09-17")}
        self.assertIsNotNone(rows["16:00 板块K线"], "UTC 08:00 行必须归入北京 09-17")
        self.assertEqual(rows["16:00 板块K线"].hour, 16)   # 已转北京时间
        self.assertIsNotNone(rows["09:30 晨检"])

    def test_utc_1630_belongs_to_next_beijing_day(self):
        r17 = {label: st for label, st, *_ in tool.runs_for_date("2026-09-17")}
        r18 = {label: st for label, st, *_ in tool.runs_for_date("2026-09-18")}
        self.assertIsNone(r17["22:30 Shadow"])
        self.assertIsNotNone(r18["22:30 Shadow"])

    def test_missing_jobs_get_placeholder(self):
        rows = tool.runs_for_date("2026-09-18")
        self.assertEqual(len(rows), 4, "逐 job 固定出全行，不留给 agent 自由裁量")


class TestAppend(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._orig_dir = tool.RUNS_DIR
        tool.RUNS_DIR = root / "daily_runs"
        self.body = root / "body.md"
        self.body.write_text("正文\n需人工介入的点：无", encoding="utf-8")

    def tearDown(self):
        tool.RUNS_DIR = self._orig_dir
        self._tmp.cleanup()

    def _append(self, date, section, force=False):
        argv = ["append", "--date", date, "--section", section, "--file", str(self.body)]
        if force:
            argv.append("--force")
        return tool.main(argv)

    def test_creates_and_never_rewrites_existing(self):
        self._append("2099-01-01", "[23:00 日汇总]")
        p = tool.RUNS_DIR / "2099-01-01.md"
        p.write_text("## 既有小节勿动\n", encoding="utf-8")
        self._append("2099-01-01", "[23:00 日汇总]")
        txt = p.read_text(encoding="utf-8")
        self.assertTrue(txt.startswith("## 既有小节勿动"), "既有内容必须原样保留")
        self.assertIn("[23:00 日汇总]", txt)

    def test_same_section_skips_then_force_appends(self):
        self._append("2099-01-02", "[23:00 日汇总]")
        self._append("2099-01-02", "[23:00 日汇总]")          # SKIP
        txt = (tool.RUNS_DIR / "2099-01-02.md").read_text(encoding="utf-8")
        self.assertEqual(txt.count("## [23:00 日汇总]"), 1)
        self._append("2099-01-02", "[23:00 日汇总]", force=True)
        txt = (tool.RUNS_DIR / "2099-01-02.md").read_text(encoding="utf-8")
        self.assertEqual(txt.count("## [23:00 日汇总]"), 2)

    def test_empty_body_rejected(self):
        self.body.write_text("", encoding="utf-8")
        self.assertEqual(tool.main(["append", "--date", "2099-01-03",
                                    "--section", "[x]", "--file", str(self.body)]), 2)
        self.assertFalse((tool.RUNS_DIR / "2099-01-03.md").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
