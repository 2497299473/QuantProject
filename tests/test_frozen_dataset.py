"""frozen_dataset（V4.3 P0-1）离线测例（2026-09-20）。

钉死的事（09-18 C2 特征非不变性的真实病灶 + GPT 评审 P0-1）：
1. **fail-closed**：无冻结件且非 --fresh ⇒ MISSING，**不静默活拉**
   （静默活拉正是「两次同代码跑出两套数字」的根）；
2. **G-A 硬闸门**：sha（LF 归一）/ 行数 / 降级征兆 / 个股K线失败任一不符 ⇒ INVALID；
3. **canonical 选择**：只认 8 位数字日期标签的 samples_frozen_*.jsonl，
   非规范命名不选；取最新标签；
4. **--fresh 显式**：活拉且标 FRESH（与冻结基线不可比）；
5. **G-B scope-aware**：留档带 stock_codes_scope/cutoff ⇒ 同口径重算
   （scope 参数必须真传到重算），漂移只标 DRIFTED 不拦截。

全程 tempdir + stub，零网络、不碰真实 data/ 与 forecast_outputs/。
"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import frozen_dataset as fd  # noqa: E402
import freeze_verify_tool as tool  # noqa: E402  fd._vt 与它是同一模块对象，patch 其函数


def _write_frozen(root: Path, tag: str, rows: list[dict],
                  kfp_agg: str = "f5a2f607" + "0" * 56,
                  meta_extra: dict | None = None) -> Path:
    """在临时根下造一份 canonical 三件套（jsonl + meta + kfp 侧车），返回 jsonl 路径。"""
    outdir = root / "forecast_outputs"
    outdir.mkdir(parents=True, exist_ok=True)
    jsonl = outdir / f"samples_frozen_{tag}.jsonl"
    body = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)
    # 与 freeze_samples 落盘同口径：Windows 下 write_text 产生 CRLF，sha 按 LF 归一记
    jsonl.write_bytes(body.replace("\n", "\r\n").encode("utf-8"))
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    meta = {"kind": "forecast_lab_samples_freeze", "schema_version": "2",
            "sha256": sha, "n_samples": len(rows), "jsonl": jsonl.name,
            "degraded_or_ratelimit_flags": [], "stock_data_failures": [],
            "code_commit": "deadbeef" * 5}
    meta.update(meta_extra or {})
    jsonl.with_suffix(".meta.json").write_text(json.dumps(meta, ensure_ascii=False),
                                               encoding="utf-8")
    kfp = {"kind": "forecast_lab_kline_fingerprint", "aggregate_sha256": kfp_agg}
    (outdir / f"kline_fingerprint_{tag}.json").write_text(json.dumps(kfp), encoding="utf-8")
    return jsonl


class TestResolveSamples(unittest.TestCase):
    ROWS = [{"fund": "002112", "date": "2020-04-27", "est_chg": 0.1},
            {"fund": "002207", "date": "2020-04-28", "est_chg": -0.2}]
    KFP = "f5a2f607" + "0" * 56

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._orig = tool.current_kline_fingerprint
        self._kfp_calls: list = []

    def tearDown(self):
        tool.current_kline_fingerprint = self._orig
        self._tmp.cleanup()

    def _stub_kfp(self, agg: str) -> None:
        def _fake(recorded=None):
            self._kfp_calls.append(recorded)
            return {"aggregate_sha256": agg}
        tool.current_kline_fingerprint = _fake

    def _live(self) -> tuple:
        calls = []

        def _live_loader():
            calls.append(1)
            return list(self.ROWS)
        return _live_loader, calls

    def test_missing_fails_closed_without_live(self):
        live, calls = self._live()
        samples, info = fd.resolve_samples(None, False, self.root, live)
        self.assertEqual(samples, [])
        self.assertEqual(info["mode"], "MISSING")
        self.assertEqual(calls, [], "MISSING 不得静默活拉")

    def test_frozen_pass_same(self):
        _write_frozen(self.root, "20260910", self.ROWS, kfp_agg=self.KFP)
        self._stub_kfp(self.KFP)
        live, calls = self._live()
        samples, info = fd.resolve_samples(None, False, self.root, live)
        self.assertEqual(info["mode"], "FROZEN")
        self.assertEqual(info["gate_internal"], "PASS")
        self.assertEqual(info["gate_comparability"], "SAME")
        self.assertEqual(len(samples), len(self.ROWS))
        self.assertIn("FROZEN", info["report_line"])
        self.assertEqual(calls, [], "FROZEN 路径不得触发活拉")

    def test_latest_picks_highest_tag_ignores_decoys(self):
        _write_frozen(self.root, "20260910", self.ROWS, kfp_agg=self.KFP)
        _write_frozen(self.root, "20260915", self.ROWS, kfp_agg=self.KFP)
        (self.root / "forecast_outputs" / "samples_frozen_v2.jsonl").write_text("x",
                                                                               encoding="utf-8")
        self._stub_kfp(self.KFP)
        _, info = fd.resolve_samples(None, False, self.root, lambda: list(self.ROWS))
        self.assertEqual(info["file"], "samples_frozen_20260915.jsonl")

    def test_explicit_snapshot_path(self):
        p = _write_frozen(self.root, "20260910", self.ROWS, kfp_agg=self.KFP)
        self._stub_kfp(self.KFP)
        _, info = fd.resolve_samples(str(p), False, self.root, lambda: list(self.ROWS))
        self.assertEqual(info["mode"], "FROZEN")
        self.assertEqual(info["file"], p.name)

    def test_tampered_sha_invalid(self):
        p = _write_frozen(self.root, "20260910", self.ROWS, kfp_agg=self.KFP)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"fund": "025687", "date": "2026-01-01"}) + "\n")
        self._stub_kfp(self.KFP)
        samples, info = fd.resolve_samples(None, False, self.root, lambda: list(self.ROWS))
        self.assertEqual(samples, [])
        self.assertEqual(info["mode"], "INVALID")
        self.assertEqual(info["gate_internal"], "INVALID")

    def test_row_count_mismatch_invalid(self):
        p = _write_frozen(self.root, "20260910", self.ROWS, kfp_agg=self.KFP)
        meta_p = p.with_suffix(".meta.json")
        m = json.loads(meta_p.read_text(encoding="utf-8"))
        m["n_samples"] = 99
        meta_p.write_text(json.dumps(m), encoding="utf-8")
        self._stub_kfp(self.KFP)
        _, info = fd.resolve_samples(None, False, self.root, lambda: list(self.ROWS))
        self.assertEqual(info["mode"], "INVALID")

    def test_degraded_flags_invalid(self):
        _write_frozen(self.root, "20260910", self.ROWS, kfp_agg=self.KFP,
                      meta_extra={"degraded_or_ratelimit_flags": ["DegradedResponse"]})
        self._stub_kfp(self.KFP)
        _, info = fd.resolve_samples(None, False, self.root, lambda: list(self.ROWS))
        self.assertEqual(info["mode"], "INVALID")

    def test_stock_data_failures_invalid(self):
        """V4.3 P0-3：meta 记录的个股K线失败非空 ⇒ 快照结构性无效。"""
        _write_frozen(self.root, "20260910", self.ROWS, kfp_agg=self.KFP,
                      meta_extra={"stock_data_failures": [{"code": "000001", "error": "x"}]})
        self._stub_kfp(self.KFP)
        samples, info = fd.resolve_samples(None, False, self.root, lambda: list(self.ROWS))
        self.assertEqual(samples, [])
        self.assertEqual(info["mode"], "INVALID")

    def test_legacy_meta_without_stock_failures_field_passes(self):
        """旧件（无 stock_data_failures 字段）不判——向后兼容 09-10 锚点件。"""
        p = _write_frozen(self.root, "20260910", self.ROWS, kfp_agg=self.KFP)
        meta_p = p.with_suffix(".meta.json")
        m = json.loads(meta_p.read_text(encoding="utf-8"))
        m.pop("stock_data_failures")
        m["schema_version"] = "1"
        meta_p.write_text(json.dumps(m), encoding="utf-8")
        self._stub_kfp(self.KFP)
        _, info = fd.resolve_samples(None, False, self.root, lambda: list(self.ROWS))
        self.assertEqual(info["mode"], "FROZEN")

    def test_fresh_mode_marks_uncomparable(self):
        live, calls = self._live()
        samples, info = fd.resolve_samples(None, True, self.root, live)
        self.assertEqual(calls, [1], "--fresh 必须真调 live_loader")
        self.assertEqual(info["mode"], "FRESH")
        self.assertIn("FRESH", info["report_line"])
        self.assertEqual(len(samples), len(self.ROWS))

    def test_gb_scope_aware_recompute_and_drift(self):
        """V4.3 P0-2：留档带 scope ⇒ 同口径重算（scope 参数真传）；漂移只软标记。"""
        _write_frozen(self.root, "20260910", self.ROWS, kfp_agg="cccc" + "0" * 60)
        kfp_p = self.root / "forecast_outputs" / "kline_fingerprint_20260910.json"
        kfp = json.loads(kfp_p.read_text(encoding="utf-8"))
        kfp["stock_codes_scope"] = ["000001"]
        kfp["cutoff"] = "2026-09-10"
        kfp_p.write_text(json.dumps(kfp), encoding="utf-8")
        self._stub_kfp("dddd" + "0" * 60)   # 不同聚合 ⇒ DRIFTED
        samples, info = fd.resolve_samples(None, False, self.root, lambda: list(self.ROWS))
        self.assertEqual(info["mode"], "FROZEN", "漂移是软标记，不得拦截")
        self.assertEqual(info["gate_comparability"], "DRIFTED")
        rec = self._kfp_calls[-1]
        self.assertEqual(rec.get("stock_codes_scope"), ["000001"], "scope 必须传给重算")
        self.assertEqual(rec.get("cutoff"), "2026-09-10")
        self.assertIn("DRIFTED", info["report_line"])

    def test_gb_legacy_unchanged_scope(self):
        """09-20 前锚点件（无 scope 字段）⇒ 重算记录不带 scope/cutoff（全量口径）。"""
        _write_frozen(self.root, "20260910", self.ROWS, kfp_agg=self.KFP)
        self._stub_kfp(self.KFP)
        fd.resolve_samples(None, False, self.root, lambda: list(self.ROWS))
        rec = self._kfp_calls[-1]
        self.assertNotIn("stock_codes_scope", rec or {})
        self.assertNotIn("cutoff", rec or {})

    def test_gb_unknown_when_recompute_fails(self):
        _write_frozen(self.root, "20260910", self.ROWS, kfp_agg=self.KFP)
        tool.current_kline_fingerprint = lambda recorded=None: None
        samples, info = fd.resolve_samples(None, False, self.root, lambda: list(self.ROWS))
        self.assertEqual(info["mode"], "FROZEN")
        self.assertEqual(info["gate_comparability"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main(verbosity=2)
