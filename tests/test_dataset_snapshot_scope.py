"""V4.1 ①（2026-09-18）：Dataset Snapshot Scope —— manifest 与 registry 解环。

背景（Evidence Contract 的自指缺陷）：`data_fingerprint.py` 旧实现全量扫描
`data/`，把 `data/model_registry/registry.json` 也收进清单；而
`model_registry.capture_provenance()` 又把「本清单文件的 sha256」写进 registry
的每个 entry，于是形成

    manifest ──hash──▶ registry.json ──内嵌──▶ manifest 的 hash

**任何一次重新生成都会让「manifest 记录的 registry hash」立刻过时**——这不是理论
问题：2026-09-18 实测 manifest 记 registry 为 `2390f0f1…`，registry 实际为
`54619525…`，已不一致。修后依赖变单向：

    Dataset Snapshot ──▶ Model Provenance（registry）──▶ 模型权重 / git commit

本文件全部在临时目录跑，不碰真实 `data/`（避免生产数据 churn，也守住本仓
FAST 层「纯函数 / 不碰真实数据与输出」的分层约定）。生产文件的常驻守护在
审计侧：`audit_project.check_manifest_scope_acyclic`（P0-5）。
"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import data_fingerprint as df                            # noqa: E402


class TestScopeExcludesProvenance(unittest.TestCase):
    """排除集必须显式包含 provenance 目录，且不许被"顺手改回来"。"""

    def test_provenance_dirs_are_excluded(self):
        self.assertIn("model_registry", df.EXCLUDE_DIRS)
        self.assertIn("models", df.EXCLUDE_DIRS)

    def test_exclude_dirs_is_derived_from_provenance_declaration(self):
        # PROVENANCE_DIRS 与 EXCLUDE_DIRS 必须同步——环检测判据与实际排除集
        # 若漂移，_assert_acyclic 会形同虚设。
        self.assertTrue(df.PROVENANCE_DIRS <= set(df.EXCLUDE_DIRS),
                        "provenance 目录未全部进 EXCLUDE_DIRS，自指环可能复现")

    def test_prereg_and_holidays_remain_in_scope(self):
        """Summer 2026-09-18 拍板：promotion_prereg.json 等治理凭据保留在清单内。

        它们不构成环（不内嵌 manifest hash），且是授权判据的证据，属于实验输入。
        """
        for name in ("promotion_prereg.json", "holidays.json"):
            self.assertNotIn(name, df.EXCLUDE_FILES)

    def test_scope_constant_is_declared(self):
        self.assertEqual(df.SNAPSHOT_SCOPE, "dataset_inputs")


class TestAcyclicGuard(unittest.TestCase):
    """硬环检测：即使有人改了排除集，生成时也必须炸，而不是静默产出自指清单。"""

    def test_detects_registry_json(self):
        files = {"data/manifest.json": {}, "data/model_registry/registry.json": {}}
        with self.assertRaises(RuntimeError) as cm:
            df._assert_acyclic(files)
        self.assertIn("registry.json", str(cm.exception))

    def test_detects_model_pkl(self):
        with self.assertRaises(RuntimeError):
            df._assert_acyclic({"data/models/forecast_v3.pkl": {}})

    def test_clean_dataset_inputs_passes(self):
        df._assert_acyclic({                      # 不得抛
            "data/stock_klines/002112.json": {},
            "data/promotion_prereg.json": {},
            "data/intraday/2026-09-18_mid.json": {},
        })

    def test_guard_runs_before_any_write(self):
        """环检测必须先于落盘：否则自指清单已经写出去了。"""
        src = (BASE_DIR / "data_fingerprint.py").read_text(encoding="utf-8")
        # build_manifest 内部第一句就是校验；main 先 build 再 write
        build = src[src.index("def build_manifest"):src.index("def main")]
        self.assertLess(build.index("_assert_acyclic(files)"),
                        build.index("\"files\": files"),
                        "环检测晚于正文组装")
        main = src[src.index("def main"):]
        self.assertLess(main.index("build_manifest("), main.index("tmp.write_text"),
                        "main 先写盘后校验 = 自指清单已产出")


class TestSnapshotId(unittest.TestCase):
    """内容寻址 ID：同数据必同 ID，与生成时刻/mtime 无关。"""

    @staticmethod
    def _by_sha(sha_map: dict) -> dict:
        return {k: {"sha256": v} for k, v in sha_map.items()}

    def test_deterministic_regardless_of_insertion_order(self):
        a = df._snapshot_id(self._by_sha({"data/x.json": "aa", "data/y.json": "bb"}))
        b = df._snapshot_id(self._by_sha({"data/y.json": "bb", "data/x.json": "aa"}))
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)

    def test_content_change_changes_id(self):
        a = df._snapshot_id(self._by_sha({"data/x.json": "aa"}))
        b = df._snapshot_id(self._by_sha({"data/x.json": "cc"}))
        self.assertNotEqual(a, b)

    def test_mtime_size_metadata_does_not_change_id(self):
        # 旧版把 mtime 当快照身份的一部分；同一份数据换机器复制会"看起来变了"
        a = df._snapshot_id({"data/x.json": {"sha256": "aa", "mtime": "2026-01-01",
                                             "size": 10}})
        b = df._snapshot_id({"data/x.json": {"sha256": "aa", "mtime": "2026-09-18",
                                             "size": 999}})
        self.assertEqual(a, b, "快照 ID 应只由内容决定")

    def test_file_added_changes_id(self):
        a = df._snapshot_id(self._by_sha({"data/x.json": "aa"}))
        b = df._snapshot_id(self._by_sha({"data/x.json": "aa", "data/z.json": "zz"}))
        self.assertNotEqual(a, b)


class TestScanEndToEnd(unittest.TestCase):
    """端到端：tempdir 里造一份带 provenance 的小目录，跑真实扫描逻辑。

    不碰真实 data/（避免生产数据 churn，也符合本仓 FAST 层「不碰真实数据与输出」
    的分层定义）。生产文件的常驻守护另有其人：审计 P0-5 check_manifest_scope_acyclic。
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.data = self.root / "data"
        (self.data / "model_registry").mkdir(parents=True)
        (self.data / "models").mkdir(parents=True)
        (self.data / "stock_klines").mkdir(parents=True)
        # 真环会长的样子：registry 内嵌 manifest 的 sha256，模型权重是产物
        (self.data / "model_registry" / "registry.json").write_text(
            json.dumps({"models": {"forecast_v3.pkl":
                                   {"data_manifest_sha256": "deadbeefdeadbeef"}}}),
            encoding="utf-8")
        (self.data / "models" / "forecast_v3.pkl").write_bytes(b"\x80\x04proto")
        (self.data / "stock_klines" / "002112.json").write_text('{"k": 1}', encoding="utf-8")
        (self.data / "holidays.json").write_text('{"years": {}}', encoding="utf-8")
        (self.data / "promotion_prereg.json").write_text('{}', encoding="utf-8")
        (self.data / "manifest.json").write_text('{"old": true}', encoding="utf-8")

    def tearDown(self):
        self._td.cleanup()

    def test_scan_drops_provenance_and_keeps_dataset_inputs(self):
        files = df.scan_files(self.root, self.data)
        keys = set(files)
        self.assertNotIn("data/model_registry/registry.json", keys)
        self.assertNotIn("data/models/forecast_v3.pkl", keys)
        self.assertNotIn("data/manifest.json", keys, "清单不得描述自己")
        self.assertEqual(keys, {"data/holidays.json", "data/promotion_prereg.json",
                                "data/stock_klines/002112.json"})

    def test_scan_keys_are_forward_slash(self):
        # Windows 的 str(relative_to) 会产出反斜杠；跨环境可比性依赖 posix 口径
        for k in df.scan_files(self.root, self.data):
            self.assertNotIn("\\", k, f"键名含反斜杠：{k}")

    def test_build_manifest_is_acyclic_and_self_consistent(self):
        files = df.scan_files(self.root, self.data)
        man = df.build_manifest(files, self.data)
        self.assertEqual(man["scope"], "dataset_inputs")
        self.assertEqual(man["schema_version"], "2.0")
        self.assertEqual(man["n_files"], len(files))
        self.assertEqual(man["snapshot_id"], df._snapshot_id(files),
                         "snapshot_id 必须可由 files 复算（自证一致）")
        df._assert_acyclic(man["files"])          # 不得抛

    def test_build_manifest_refuses_cyclic_input(self):
        # 若排除集被改坏，build 阶段必须炸，不得产出自指清单
        with self.assertRaises(RuntimeError):
            df.build_manifest({"data/model_registry/registry.json": {"sha256": "x"}},
                              self.data)

    def test_snapshot_id_tracks_content_not_touch(self):
        files = df.scan_files(self.root, self.data)
        sid = df._snapshot_id(files)
        # 只改 mtime/size（复制文件即可做到：内容不变）→ ID 不变
        (self.data / "holidays.json").write_text('{"years": {}}', encoding="utf-8")
        self.assertEqual(df._snapshot_id(df.scan_files(self.root, self.data)), sid,
                         "内容未变仅时间戳变，不得当成新快照")
        # 真改内容 → ID 必变
        (self.data / "holidays.json").write_text('{"years": {"2027": {}}}',
                                                  encoding="utf-8")
        self.assertNotEqual(df._snapshot_id(df.scan_files(self.root, self.data)), sid)

    def test_prereg_file_is_in_scope_per_2026_09_18_decision(self):
        # Summer 拍板：promotion_prereg.json 是授权判据证据，保留在清单内
        self.assertIn("data/promotion_prereg.json", df.scan_files(self.root, self.data))


class TestProvenanceChainStillResolves(unittest.TestCase):
    """解环不得打断回溯：构造 registry 引历史 manifest hash 的场景，验证可解析。

    tempdir 版：自己造 manifest + manifest_history + registry，不读真实文件。
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.data = self.root / "data"
        (self.data / "manifest_history").mkdir(parents=True)
        (self.data / "model_registry").mkdir(parents=True)

    def tearDown(self):
        self._td.cleanup()

    def _allowed(self):
        import hashlib
        cur = self.data / "manifest.json"
        allowed = {hashlib.sha256(cur.read_bytes()).hexdigest()[:16]}
        for p in sorted((self.data / "manifest_history").glob("manifest_*.json")):
            allowed.add(hashlib.sha256(p.read_bytes()).hexdigest()[:16])
        return allowed

    def test_archived_snapshot_still_resolves_after_scope_change(self):
        # 旧版全量清单（含 registry）已归档；新版重生成后，旧引用仍须可解析，
        # 否则历史 shadow / registry 记录会集体变成「指向不存在的快照」。
        old = json.dumps({"generated_at": "2026-08-31T09:12:53",
                          "files": {"data/model_registry/registry.json":
                                    {"sha256": "x"}}}, ensure_ascii=False)
        archived = self.data / "manifest_history" / "manifest_20260831T091253.json"
        archived.write_text(old, encoding="utf-8")
        cur = df.build_manifest(df.scan_files(self.root, self.data), self.data)
        (self.data / "manifest.json").write_text(json.dumps(cur), encoding="utf-8")

        ref = hashlib.sha256(archived.read_bytes()).hexdigest()[:16]
        self.assertIn(ref, self._allowed(), "归档快照必须仍在 allowed 集内")

    def test_negative_control_dangling_reference_is_caught(self):
        """反向自验：归档丢失时上面的判据真能报警（不是永真断言）。"""
        archived = self.data / "manifest_history" / "manifest_20260831T091253.json"
        archived.write_text('{"archived": "v-20260831"}', encoding="utf-8")
        # 当前版必须与归档内容**不同**，否则两者 sha256 相同，删除归档后仍会
        # 因为「当前版在 allowed 集里」而误判可解析（2026-09-18 首跑即踩此坑）。
        (self.data / "manifest.json").write_text('{"current": "v-20260918"}',
                                                 encoding="utf-8")
        ref_before = hashlib.sha256(archived.read_bytes()).hexdigest()[:16]
        self.assertNotIn(ref_before,
                         {hashlib.sha256((self.data / "manifest.json")
                                          .read_bytes()).hexdigest()[:16]},
                         "测例前置条件：归档与当前版必须不同内容")
        self.assertIn(ref_before, self._allowed())

        archived.unlink()                      # 模拟归档被误删
        self.assertNotIn(ref_before, self._allowed(),
                         "归档消失后仍判可解析 ⇒ 上面的测例是永真的")


class TestAuditGuardP0_5(unittest.TestCase):
    """审计侧常驻守护 check_manifest_scope_acyclic（P0-5）本身也要可测。

    它是「生产文件当前是否真的无环」的守卫——fast 测例只覆盖逻辑，真实文件
    由每次 `python audit_project.py` 复核。三种判据各测一例（全程 tempdir）。
    """

    def setUp(self):
        import audit_project as ap
        self.ap = ap
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self._orig = (ap.MANIFEST,)
        ap.MANIFEST = self.data / "manifest.json"

    def tearDown(self):
        self.ap.MANIFEST = self._orig[0]
        self._td.cleanup()

    def _run(self):
        a = self.ap.Audit()
        self.ap.check_manifest_scope_acyclic(a)
        self.assertEqual(len(a.checks), 1)
        return a.checks[0]

    def _write(self, files, **extra):
        payload = {"schema_version": "2.0", "scope": "dataset_inputs",
                   "n_files": len(files), "files": files}
        payload.update(extra)
        if "snapshot_id" not in payload:
            payload["snapshot_id"] = df._snapshot_id(files)
        self.ap.MANIFEST.write_text(json.dumps(payload), encoding="utf-8")

    def test_missing_manifest_fails(self):
        self.assertEqual(self._run().status, "FAIL")

    def test_clean_scope_passes(self):
        files = {"data/holidays.json": {"sha256": "aa"},
                 "data/promotion_prereg.json": {"sha256": "bb"}}
        self._write(files)
        chk = self._run()
        self.assertEqual(chk.status, "PASS", chk.detail)
        self.assertEqual(chk.cid, "P0-5")

    def test_cyclic_manifest_fails(self):
        # 有人用旧脚本重生成 / 手工回填 → registry 回到清单里 ⇒ 必须 FAIL
        files = {"data/holidays.json": {"sha256": "aa"},
                 "data/model_registry/registry.json": {"sha256": "bb"}}
        self._write(files)
        chk = self._run()
        self.assertEqual(chk.status, "FAIL", "自指环复现必须 FAIL，不得 WARN 放过")
        self.assertIn("registry.json", chk.detail)

    def test_legacy_backslash_keys_still_detected(self):
        # 旧版键是 Windows 反斜杠口径；审计侧必须归一化后再判，否则环会漏网
        files = {"data\\model_registry\\registry.json": {"sha256": "bb"}}
        self._write(files)
        self.assertEqual(self._run().status, "FAIL")

    def test_missing_scope_marks_warn_not_pass(self):
        # 无环但语义未标注（旧版清单）⇒ WARN，提示重生成而不是谎报合格
        files = {"data/holidays.json": {"sha256": "aa"}}
        self._write(files)
        self.ap.MANIFEST.write_text(json.dumps(
            {"n_files": 1, "files": files, "snapshot_id": df._snapshot_id(files)}),
            encoding="utf-8")
        self.assertEqual(self._run().status, "WARN")

    def test_tampered_snapshot_id_fails(self):
        # 内容与 ID 不符 = 清单被手工改过 ⇒ FAIL（ID 是自证一致性的锚）
        files = {"data/holidays.json": {"sha256": "aa"}}
        self._write(files, snapshot_id="0123456789abcdef")
        chk = self._run()
        self.assertEqual(chk.status, "FAIL")
        self.assertIn("snapshot_id", chk.detail)


if __name__ == "__main__":
    unittest.main()
