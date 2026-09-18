"""freeze_verify_tool 离线测例（2026-09-18，C2 特征非不变性 P0 的收口件）。

钉死四件事（全部为实测踩到的真坑，不是假想）：
1. **换行口径**：meta 记的 sha256 是 LF 归一后的哈希；文件原始字节是 CRLF
   （实测 09-10 件 `be8e55f0…` vs `0d59f663…`）。若校验用原始字节 → 假失败。
2. **G-A 硬中止**：样本 sha 与 meta 不符 / 行数不符 / 冻结时带降级征兆 ⇒ exit 3。
3. **G-B 软标记**：K 线指纹漂移只标 `DRIFTED`，**不得**改 exit 码
   （做成硬中止会让任务在正常漂移下彻底跑不动）。
4. **侧车断链回退**：09-10 的 meta 没有 `kline_fingerprint_*` 字段（meta 生成于
   09:52，指纹接入 13:13），必须能按日期标签找到兄弟指纹文件。
"""
import hashlib
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import freeze_verify_tool as tool  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict], crlf: bool = True) -> str:
    """按 freeze_samples.py 的方式落盘（write_text ⇒ Windows 下 CRLF），返回 LF 归一 sha。"""
    body = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)
    data = body.replace("\n", "\r\n") if crlf else body
    path.write_bytes(data.encode("utf-8"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._orig_fp = tool.current_kline_fingerprint
        self._orig_commit = tool.git_commit
        tool.git_commit = lambda: "deadbeef" * 5
        self.jsonl = self.root / "samples_frozen_20260910.jsonl"
        self.meta = self.root / "samples_frozen_20260910.meta.json"
        self.kfp = self.root / "kline_fingerprint_20260910.json"
        self.rows = [{"fund": "002112", "date": "2020-04-27", "est_chg": 0.1},
                     {"fund": "002207", "date": "2020-04-28", "est_chg": -0.2}]
        self.sha = _write_jsonl(self.jsonl, self.rows, crlf=True)
        self._write_meta(sha=self.sha, n=len(self.rows))
        self._write_kfp("f5a2f607" + "0" * 56)

    def tearDown(self):
        tool.current_kline_fingerprint = self._orig_fp
        tool.git_commit = self._orig_commit
        self._tmp.cleanup()

    def _write_meta(self, sha: str, n: int, extra: dict | None = None) -> None:
        m = {"kind": "forecast_lab_samples_freeze", "sha256": sha, "n_samples": n,
             "jsonl": self.jsonl.name, "degraded_or_ratelimit_flags": []}
        m.update(extra or {})
        self.meta.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")

    def _write_kfp(self, agg: str, name: str | None = None) -> None:
        p = self.root / name if name else self.kfp
        p.write_text(json.dumps({"kind": "forecast_lab_kline_fingerprint",
                                 "aggregate_sha256": agg}), encoding="utf-8")

    def _stub_kfp(self, agg: str) -> None:
        tool.current_kline_fingerprint = lambda: {"aggregate_sha256": agg}

    def _verify(self, *extra: str) -> int:
        return tool.main(["verify", "--jsonl", str(self.jsonl), *extra])


class TestLfNormalization(_Base):
    def test_crlf_file_passes_lf_gate(self):
        """CRLF 落盘文件必须能通过 LF 归一校验（防 Get-FileHash 式假失败）。"""
        self._stub_kfp("f5a2f607" + "0" * 56)
        raw = hashlib.sha256(self.jsonl.read_bytes()).hexdigest()
        self.assertNotEqual(raw, self.sha, "前置：原始字节哈希本应与 LF 归一哈希不同")
        self.assertEqual(self._verify(), tool.EXIT_PASS)

    def test_lf_normalized_helper_matches_written_sha(self):
        self.assertEqual(tool.lf_normalized_sha256(self.jsonl), self.sha)
        self.assertNotEqual(tool.raw_bytes_sha256(self.jsonl), self.sha)


class TestGateAInternal(_Base):
    def test_tampered_samples_abort(self):
        """样本被改（sha 不符）⇒ 硬中止 exit 3。"""
        self._stub_kfp("f5a2f607" + "0" * 56)
        _write_jsonl(self.jsonl, self.rows + [{"fund": "025687", "date": "2026-01-01"}])
        self.assertEqual(self._verify(), tool.EXIT_MISMATCH)

    def test_row_count_mismatch_abort(self):
        self._stub_kfp("f5a2f607" + "0" * 56)
        self._write_meta(sha=self.sha, n=99)
        self.assertEqual(self._verify(), tool.EXIT_MISMATCH)

    def test_degraded_freeze_flags_abort(self):
        self._stub_kfp("f5a2f607" + "0" * 56)
        self._write_meta(sha=self.sha, n=len(self.rows),
                         extra={"degraded_or_ratelimit_flags": ["DegradedResponse"]})
        self.assertEqual(self._verify(), tool.EXIT_MISMATCH)

    def test_missing_meta_is_fail_closed(self):
        self._stub_kfp("f5a2f607" + "0" * 56)
        self.meta.unlink()
        self.assertEqual(self._verify(), tool.EXIT_MISSING)

    def test_missing_samples_is_fail_closed(self):
        self.jsonl.unlink()
        self.assertEqual(self._verify(), tool.EXIT_MISSING)


class TestGateBComparability(_Base):
    def test_kfp_drift_is_marked_not_fatal(self):
        """指纹漂移必须只标 DRIFTED，exit 仍为 0 —— 硬中止会让任务跑不动。"""
        self._stub_kfp("b5b25cd3" + "1" * 56)
        self.assertEqual(self._verify(), tool.EXIT_PASS)

    def test_kfp_same_when_equal(self):
        self._stub_kfp("f5a2f607" + "0" * 56)
        out = self.root / "triple.json"
        self.assertEqual(self._verify("--triple-out", str(out)), tool.EXIT_PASS)
        t = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(t["gate_comparability"], "SAME")
        self.assertEqual(t["gate_internal"], "PASS")

    def test_kfp_recompute_failure_is_unknown_not_fatal(self):
        tool.current_kline_fingerprint = lambda: None
        self.assertEqual(self._verify(), tool.EXIT_PASS)

    def test_triple_records_code_commit_and_both_shas(self):
        self._stub_kfp("b5b25cd3" + "1" * 56)
        out = self.root / "triple.json"
        self._verify("--triple-out", str(out))
        t = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(t["gate_comparability"], "DRIFTED")
        self.assertEqual(t["samples_sha256_lf"], self.sha)
        self.assertNotEqual(t["samples_sha256_raw_bytes"], self.sha)
        self.assertTrue(t["code_commit"])
        self.assertEqual(t["kline_fingerprint_recorded"], "f5a2f607" + "0" * 56)
        self.assertEqual(t["kline_fingerprint_current"], "b5b25cd3" + "1" * 56)


class TestSidecarDiscovery(_Base):
    def test_finds_sibling_kfp_without_meta_field(self):
        """09-10 实况：meta 无指纹字段，仍须按日期标签找到兄弟指纹文件。"""
        self._stub_kfp("f5a2f607" + "0" * 56)
        self._write_meta(sha=self.sha, n=len(self.rows))   # 故意不写 kfp 字段
        self.assertNotIn("kline_fingerprint_sha256",
                         json.loads(self.meta.read_text(encoding="utf-8")))
        out = self.root / "triple.json"
        self.assertEqual(self._verify("--triple-out", str(out)), tool.EXIT_PASS)
        t = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(t["kline_fingerprint_recorded"], "f5a2f607" + "0" * 56)
        self.assertEqual(t["kline_fingerprint_file"], self.kfp.name)

    def test_meta_kfp_field_used_when_sibling_absent(self):
        self._stub_kfp("aaaa" + "0" * 60)
        self.kfp.unlink()
        self._write_meta(sha=self.sha, n=len(self.rows),
                         extra={"kline_fingerprint_sha256": "cccc" + "0" * 60,
                                "kline_fingerprint_file": "gone.json"})
        out = self.root / "triple.json"
        self.assertEqual(self._verify("--triple-out", str(out)), tool.EXIT_PASS)
        t = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(t["kline_fingerprint_recorded"], "cccc" + "0" * 60)
        self.assertEqual(t["gate_comparability"], "DRIFTED")


class TestFreezeNoClobber(unittest.TestCase):
    """freeze_samples 的 no-clobber（2026-09-18）：同日件存在即拒绝重采。

    必须钉死两件事：
    1. 闸门在 **load_samples() 之前** —— 否则东财请求已发出去才说不采，
       既没防住重采、又白烧一次配额（这正是本次事故的形态）。
    2. --force 才放行。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "forecast_outputs").mkdir()
        sys.path.insert(0, str(BASE_DIR / "experiments" / "forecast_lab"))
        import freeze_samples as fs
        self.fs = fs
        self._orig_base = fs.BASE_DIR
        fs.BASE_DIR = self.root
        # 哨兵：load_samples 一旦被调用即失败
        self.called = 0
        fake = types.ModuleType("backtest_spread")

        def _load_samples():
            self.called += 1
            raise AssertionError("load_samples 被调用 —— 闸门没能在网络之前拦住")
        fake.load_samples = _load_samples
        self._orig_mod = sys.modules.get("backtest_spread")
        sys.modules["backtest_spread"] = fake
        self.decoy = (self.root / "forecast_outputs"
                      / f"samples_frozen_{datetime.now():%Y%m%d}.jsonl")
        self.decoy.write_text("decoy\n", encoding="utf-8")

    def tearDown(self):
        self.fs.BASE_DIR = self._orig_base
        if self._orig_mod is None:
            sys.modules.pop("backtest_spread", None)
        else:
            sys.modules["backtest_spread"] = self._orig_mod
        self._tmp.cleanup()

    def test_existing_freeze_aborts_before_any_network(self):
        rc = self.fs.main([])
        self.assertEqual(rc, 2)
        self.assertEqual(self.called, 0, "闸门必须在 load_samples 之前")
        self.assertEqual(self.decoy.read_text(encoding="utf-8"), "decoy\n",
                         "拒绝重采时不得改动既有件")

    def test_force_bypasses_gate(self):
        with self.assertRaises(AssertionError):
            self.fs.main(["--force"])
        self.assertEqual(self.called, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
