"""V4-A（2026-09-16）：run.py 运行清单落盘 + 三态退出码语义。

覆盖两件事：
1. `_write_run_manifest` 把逐环状态写成机器可读 JSON，且不污染真实 output/；
2. DEGRADED 判定与退出码映射的语义（降级 ⇒ 2，干净 ⇒ 0）。

不覆盖完整 `_run()` 链路（需网络与真实模型），那部分由每日实际运行留档验证。
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import run                                              # noqa: E402


class TestRunManifestWrite(unittest.TestCase):
    def setUp(self):
        self._orig = run.BASE_DIR
        self._td = tempfile.TemporaryDirectory()
        run.BASE_DIR = Path(self._td.name)
        run._LOG.clear()

    def tearDown(self):
        run.BASE_DIR = self._orig
        run._LOG.clear()
        self._td.cleanup()

    def _written(self):
        files = sorted((Path(self._td.name) / "output" / "run_manifest").glob("*.json"))
        self.assertEqual(len(files), 1, f"应恰好落盘 1 份清单，实际 {files}")
        return files[0], json.loads(files[0].read_text(encoding="utf-8"))

    def test_success_manifest_roundtrip(self):
        now = datetime(2026, 9, 16, 18, 30, 0)
        run._write_run_manifest(now, {
            "slot": "post", "status": "SUCCESS", "degraded_reasons": [],
            "data": {"ok": True, "failed": [], "n_funds": 4},
            "lookthrough": {"ok": True, "missing": []},
            "notification": {"ok": True, "reason": None},
            "shadow": {"ok": True, "reason": None},
        })
        path, payload = self._written()
        self.assertEqual(path.name, "run_manifest_20260916_183000_post.json")
        self.assertEqual(payload["run_id"], "20260916_183000_post")
        self.assertEqual(payload["status"], "SUCCESS")
        self.assertEqual(payload["degraded_reasons"], [])
        self.assertTrue(payload["shadow"]["ok"])
        # 逐环状态齐备（缺失即监控瞎眼）
        for key in ("data", "lookthrough", "notification", "shadow", "slot", "ts"):
            self.assertIn(key, payload)

    def test_degraded_manifest_keeps_reasons(self):
        run._write_run_manifest(datetime(2026, 9, 16, 14, 55, 0), {
            "slot": "post", "status": "DEGRADED",
            "degraded_reasons": ["feishu_push_failed", "shadow_failed:exit_1"],
            "notification": {"ok": False, "reason": "webhook_403"},
        })
        _, payload = self._written()
        self.assertEqual(payload["status"], "DEGRADED")
        self.assertEqual(payload["degraded_reasons"],
                         ["feishu_push_failed", "shadow_failed:exit_1"])
        self.assertFalse(payload["notification"]["ok"])

    def test_write_failure_does_not_raise(self):
        # 落盘失败必须静默降级（不得让证据写入拖垮主流程）
        run.BASE_DIR = Path(self._td.name) / "nul" / ("x" * 300)
        try:
            run._write_run_manifest(datetime(2026, 9, 16, 18, 0, 0),
                                    {"slot": "mid", "status": "SUCCESS"})
        finally:
            run.BASE_DIR = self._orig
        self.assertTrue(any("运行清单写入失败" in ln for ln in run._LOG))


class TestExitCodeSemantics(unittest.TestCase):
    """退出码契约：0=SUCCESS / 2=DEGRADED / 1=FAILED（源码级守护）。"""

    def test_labels_documented(self):
        src = (BASE_DIR / "run.py").read_text(encoding="utf-8")
        for marker in ("return 2", "return 1", "degraded_reasons"):
            self.assertIn(marker, src)
        # 无数据 ⇒ FAILED（1），降级 ⇒ 2，干净 ⇒ 0
        self.assertIn("return 1", src)
        self.assertIn("log(f\"[exit] DEGRADED", src)


if __name__ == "__main__":
    unittest.main()
