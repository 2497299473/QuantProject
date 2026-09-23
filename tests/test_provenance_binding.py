# -*- coding: utf-8 -*-
"""P1-8 审批绑定测例（2026-09-16）。

覆盖两条链路：
1. **新登记自动绑定**：``register_model`` 写入 ``data_manifest_sha256`` / ``git_commit``，
   且口径与 shadow journal 内嵌、audit allowed 集合一致（sha256 前 16 位 hex）。
2. **历史回填的证据闸门**：``bind_provenance`` 只接受「快照逐文件记录了该 pkl 且哈希
   一致」的在场证明；找不到就拒绝写入 —— 宁缺毋滥，绝不把不可证的值绑进 registry。

隔离约定（吸取 08-30 registry 污染教训）：MODELS_DIR + REGISTRY_PATH 一并重定向到
temp 目录，测试全程不触碰真实 data/model_registry/registry.json。
"""
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import model_registry  # noqa: E402
import bind_provenance as bp     # noqa: E402


def _sha16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


class TestRegisterBindsProvenance(unittest.TestCase):
    """新登记必须自带 provenance（否则 P1-8 会随每次重训重新劣化）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_registry = (model_registry.REGISTRY_PATH.read_text(encoding="utf-8")
                               if model_registry.REGISTRY_PATH.exists() else None)
        self._orig_models_dir = model_registry.MODELS_DIR
        model_registry.MODELS_DIR = self.tmp
        model_registry.REGISTRY_PATH = self.tmp / "registry.json"

    def tearDown(self):
        model_registry.MODELS_DIR = self._orig_models_dir
        if self._orig_registry is not None:
            model_registry.REGISTRY_PATH.write_text(self._orig_registry, encoding="utf-8")
        else:
            model_registry.REGISTRY_PATH.unlink(missing_ok=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_capture_provenance_shape(self):
        prov = model_registry.capture_provenance()
        self.assertIn("data_manifest_sha256", prov)
        self.assertIn("git_commit", prov)
        man = BASE_DIR / "data" / "manifest.json"
        if man.is_file():
            self.assertRegex(prov["data_manifest_sha256"], r"^[0-9a-f]{16}$")
            # 口径必须等于该文件的 sha256 前 16 位（与 shadow / audit 三处一致）
            self.assertEqual(prov["data_manifest_sha256"], _sha16(man.read_bytes()))

    def test_register_model_stores_provenance(self):
        pkl = self.tmp / "_prov_model.pkl"
        pkl.write_bytes(b"prov-bytes")
        self.assertIsNotNone(model_registry.register_model(pkl, meta={"n_train": 1}))
        entry = model_registry.load_registry()["models"][pkl.name]
        man = BASE_DIR / "data" / "manifest.json"
        if man.is_file():
            self.assertEqual(entry["data_manifest_sha256"], _sha16(man.read_bytes()))
        # git 可用时必须绑 HEAD；不可用则诚实留 None（不编造）
        try:
            head = subprocess.run(["git", "-C", str(BASE_DIR), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            head = ""
        self.assertEqual(entry["git_commit"], head or entry["git_commit"])
        if head:
            self.assertEqual(entry["git_commit"], head)

    def test_snapshot_provenance_passthrough_and_honest_absence(self):
        """V4.3.1 ③：冻结件三元组透传进条目；未提供 ⇒ 键在但全 None（不伪造）。"""
        pkl = self.tmp / "_snap_prov.pkl"
        pkl.write_bytes(b"snap-prov")
        triple = {"snapshot_file": "samples_frozen_20260910.jsonl",
                  "samples_sha256_lf": "ab" * 32,
                  "kfp_recorded_sha256": "cd" * 32,
                  "kfp_current_sha256": "ef" * 32,
                  "kfp_comparability": "UNKNOWN"}     # 不论闸门状态照写全
        self.assertIsNotNone(model_registry.register_model(
            pkl, meta={"n_train": 2}, snapshot_provenance=triple))
        got = model_registry.load_registry()["models"][pkl.name]["snapshot_provenance"]
        self.assertEqual(got["snapshot_file"], triple["snapshot_file"])
        self.assertEqual(got["samples_sha256_lf"], triple["samples_sha256_lf"])
        self.assertEqual(got["kfp_comparability"], "UNKNOWN")

        pkl2 = self.tmp / "_snap_absent.pkl"
        pkl2.write_bytes(b"absent")
        model_registry.register_model(pkl2, meta={})
        got2 = model_registry.load_registry()["models"][pkl2.name]["snapshot_provenance"]
        self.assertEqual(set(got2), set(triple), "空 provenance 也必须键齐全（防缺键漂移）")
        self.assertTrue(all(v is None for v in got2.values()),
                        "未提供 = 诚实 None，绝不伪造")

        # 白名单：脏键不得入条目
        pkl3 = self.tmp / "_snap_dirty.pkl"
        pkl3.write_bytes(b"dirty")
        model_registry.register_model(
            pkl3, meta={}, snapshot_provenance={**triple, "evil_key": "x"})
        got3 = model_registry.load_registry()["models"][pkl3.name]["snapshot_provenance"]
        self.assertNotIn("evil_key", got3)


class TestAttestationGate(unittest.TestCase):
    """回填的证据闸门：无独立在场证明 ⇒ 拒绝绑定。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _snap(self, name: str, generated_at: str, files: dict) -> dict:
        p = self.tmp / name
        p.write_text(json.dumps({"generated_at": generated_at, "files": files}),
                     encoding="utf-8")
        return {"path": p, "payload": json.loads(p.read_text(encoding="utf-8")),
                "generated_at": generated_at}

    def test_matches_slash_and_backslash_key_forms(self):
        """旧 manifest 用 '/'、新版用 '\\' —— 两种键形态都必须能证明在场。"""
        for i, key in enumerate(("data/models/x.pkl", "data\\models\\x.pkl")):
            payload = {key: {"sha256": "ab" * 32}}
            s = self._snap(f"m_forms_{i}.json", "2026-08-31T00:00:00", payload)
            got = bp.attest_snapshot([s], "x.pkl", "ab" * 32)
            self.assertIsNotNone(got, f"键形态 {key} 未被识别")

    def test_hash_mismatch_refuses_binding(self):
        """快照里记的是**另一组字节** → 不能拿来冒充本模型的证据。"""
        s = self._snap("m1.json", "2026-08-31T00:00:00",
                       {"data/models/x.pkl": {"sha256": "ff" * 32}})
        self.assertIsNone(bp.attest_snapshot([s], "x.pkl", "ab" * 32))

    def test_absent_pkl_refuses_binding(self):
        s = self._snap("m2.json", "2026-08-31T00:00:00",
                       {"data/holidays.json": {"sha256": "ab" * 32}})
        self.assertIsNone(bp.attest_snapshot([s], "x.pkl", "ab" * 32))

    def test_picks_earliest_attesting_snapshot(self):
        """多个快照都能证明时取最早的 —— 晚生成的不冒充"登记当时"。"""
        late = self._snap("late.json", "2026-09-16T00:00:00",
                          {"data/models/x.pkl": {"sha256": "ab" * 32}})
        early = self._snap("early.json", "2026-08-31T00:00:00",
                           {"data/models/x.pkl": {"sha256": "ab" * 32}})
        got = bp.attest_snapshot([late, early], "x.pkl", "ab" * 32)
        self.assertEqual(got["generated_at"], "2026-08-31T00:00:00")

    def test_registry_binding_is_resolvable_by_audit(self):
        """真实 registry 已绑定值必须在 audit 的 allowed 集合内（否则 P0-3 会翻 FAIL）。"""
        reg = model_registry.load_registry()   # 只读真实 registry，不写
        allowed = set()
        for p in bp.manifest_snapshots():
            allowed.add(_sha16(p["path"].read_bytes()))
        self.assertTrue(allowed, "本地无可解析快照，测例前提不成立")
        for name, entry in reg["models"].items():
            bound = entry.get("data_manifest_sha256")
            if bound:
                self.assertIn(bound, allowed, f"{name} 绑定的快照 hash 不可解析")


class TestRegistryPathNormalization(unittest.TestCase):
    """V4.5（2026-09-23）：``entry["path"]`` 必须是可判读的仓库相对路径。

    实况缺陷：两个条目都存着 WSL 时代绝对路径
    ``/home/summer/QuantV1/data/models/forecast_v2.pkl``，Windows 迁移后不可判读，
    却因消费点用 ``Path(...).name`` 兜底而静默通过（审计 P1-10 现把它显式化）。
    以下测例钉住：登记写相对路径、旧绝对路径可归一、归一**不动证据字段**。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_models_dir = model_registry.MODELS_DIR
        self._orig_registry_path = model_registry.REGISTRY_PATH
        self._orig_pkl_dir = model_registry.PKL_DIR
        model_registry.MODELS_DIR = self.tmp / "model_registry"
        model_registry.REGISTRY_PATH = model_registry.MODELS_DIR / "registry.json"
        model_registry.PKL_DIR = self.tmp / "models"
        model_registry.PKL_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        model_registry.MODELS_DIR = self._orig_models_dir
        model_registry.REGISTRY_PATH = self._orig_registry_path
        model_registry.PKL_DIR = self._orig_pkl_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, name="forecast_v9.pkl", path_value=None, **extra):
        """手写一份 registry（绕开 register_model 的自动绑定，专测 path 字段）。"""
        entry = {"path": path_value if path_value is not None else
                 f"/home/summer/QuantV1/data/models/{name}",
                 "sha256": "ab" * 32, "size_bytes": 10,
                 "registered_at": "2026-08-31T08:22:23", "meta": {"n_train": 2424},
                 "validation": {"decision": "rejected", "report_sha256": "cd" * 32},
                 "promotion": {"status": "blocked", "reason": "测试"}}
        entry.update(extra)
        model_registry._save_registry({"models": {name: entry}})
        return entry

    def test_tracked_path_is_repo_relative_with_forward_slashes(self):
        p = model_registry.PKL_DIR / "forecast_v9.pkl"
        p.write_bytes(b"x")
        # PKL_DIR 被重定向到 temp ⇒ 落在仓库外，诚实退回文件名（不编造相对路径）
        self.assertEqual(model_registry.tracked_path(p), "forecast_v9.pkl")
        inside = model_registry.BASE_DIR / "data" / "models" / "forecast_v9.pkl"
        self.assertEqual(model_registry.tracked_path(inside),
                         "data/models/forecast_v9.pkl")

    def test_normalize_rewrites_absolute_path(self):
        self._seed()
        changed, details = model_registry.normalize_registry_paths()
        self.assertTrue(changed, details)
        self.assertEqual(len(details), 1)
        got = model_registry.load_registry()["models"]["forecast_v9.pkl"]["path"]
        self.assertFalse(model_registry._is_absolute_path_str(got), got)
        self.assertNotIn("\\", got, "必须用正斜杠（跨环境键可比）")

    def test_normalize_dry_run_does_not_write(self):
        self._seed()
        changed, details = model_registry.normalize_registry_paths(dry_run=True)
        self.assertTrue(changed, "dry-run 也要报告「有待归一项」")
        self.assertEqual(len(details), 1)
        got = model_registry.load_registry()["models"]["forecast_v9.pkl"]["path"]
        self.assertTrue(model_registry._is_absolute_path_str(got), "dry-run 不得落盘")

    def test_normalize_leaves_evidence_fields_untouched(self):
        """只动 path 措辞：sha256 / validation / promotion 必须逐字节不变。"""
        before = self._seed(validation={"decision": "rejected", "report_sha256": "cd" * 32},
                            promotion={"status": "blocked", "reason": "测试"},
                            git_commit="6e0c9dd66be02666bcbceb7c48c867710d9e8971",
                            data_manifest_sha256="afd156ab12a3bff1")
        model_registry.normalize_registry_paths()
        after = model_registry.load_registry()["models"]["forecast_v9.pkl"]
        for key in ("sha256", "size_bytes", "registered_at", "meta", "validation",
                    "promotion", "git_commit", "data_manifest_sha256"):
            self.assertEqual(after[key], before[key], f"{key} 不得被归一化改动")
        self.assertNotEqual(after["path"], before["path"])

    def test_normalize_is_idempotent(self):
        self._seed()
        model_registry.normalize_registry_paths()
        first = model_registry.load_registry()
        changed, details = model_registry.normalize_registry_paths()
        self.assertFalse(changed, "已是相对路径 ⇒ 无待归一项")
        self.assertEqual(details, [])
        self.assertEqual(model_registry.load_registry(), first)

    def test_register_model_writes_relative_path(self):
        pkl = model_registry.PKL_DIR / "forecast_v9.pkl"
        pkl.write_bytes(b"bytes")
        model_registry.register_model(pkl, meta={"n_train": 1})
        got = model_registry.load_registry()["models"]["forecast_v9.pkl"]["path"]
        self.assertFalse(model_registry._is_absolute_path_str(got), got)

    def test_register_model_normalizes_legacy_sibling_entry(self):
        """登记新模型时顺手归一旧条目（迁移无需单独跑脚本）。"""
        self._seed(name="forecast_v8.pkl")
        pkl = model_registry.PKL_DIR / "forecast_v9.pkl"
        pkl.write_bytes(b"bytes")
        model_registry.register_model(pkl, meta={"n_train": 1})
        reg = model_registry.load_registry()["models"]
        self.assertFalse(model_registry._is_absolute_path_str(reg["forecast_v8.pkl"]["path"]),
                         "旧绝对路径条目应被就地归一")

    def test_audit_check_flags_absolute_path_as_warn(self):
        """审计 P1-10 必须把绝对路径抓出来（旧实况是静默通过）。"""
        import audit_project as ap
        self._seed()
        orig = ap.REGISTRY
        ap.REGISTRY = model_registry.REGISTRY_PATH
        try:
            a = ap.Audit()
            ap.check_registry_paths(a)
        finally:
            ap.REGISTRY = orig
        self.assertEqual(a.checks[0].cid, "P1-10")
        self.assertEqual(a.checks[0].status, ap.WARN, a.checks[0].detail)
        self.assertIn("绝对路径", a.checks[0].detail)

    def test_audit_check_passes_after_normalization(self):
        import audit_project as ap
        self._seed()
        model_registry.normalize_registry_paths()
        orig = ap.REGISTRY
        ap.REGISTRY = model_registry.REGISTRY_PATH
        try:
            a = ap.Audit()
            ap.check_registry_paths(a)
        finally:
            ap.REGISTRY = orig
        self.assertEqual(a.checks[0].status, ap.PASS, a.checks[0].detail)


class TestGitWorktreeClean(unittest.TestCase):
    """V4.5（2026-09-23）：工作区干净度检查——「查不到」不得当成「干净」。"""

    def _check(self, monkeypatch_result):
        import audit_project as ap
        orig = ap.git_worktree_clean
        ap.git_worktree_clean = lambda: monkeypatch_result
        try:
            a = ap.Audit()
            ap.check_worktree_clean(a)
        finally:
            ap.git_worktree_clean = orig
        return a.checks[0]

    def test_clean_worktree_passes(self):
        chk = self._check((True, "工作区干净"))
        self.assertEqual(chk.cid, "P1-11")
        self.assertEqual(chk.status, "PASS", chk.detail)

    def test_dirty_worktree_warns_not_fails(self):
        """本地开发常态：判 FAIL 会让 audit_health 常年 FAIL 并触发自锁。"""
        chk = self._check((False, "5 项未提交改动"))
        self.assertEqual(chk.status, "WARN", chk.detail)
        self.assertIn("未提交改动", chk.detail)

    def test_unavailable_git_warns_not_passes(self):
        """fail-closed：把「git 查不到」当「干净」是 fail-open。"""
        chk = self._check((None, "git 不可用或非仓库（OSError）"))
        self.assertEqual(chk.status, "WARN", chk.detail)
        self.assertIn("无法确认", chk.detail)

    def test_real_git_status_shape(self):
        """真实调用只验形状（不假定干净）：返回值类型与 detail 非空。"""
        import audit_project as ap
        clean, detail = ap.git_worktree_clean()
        self.assertIn(clean, (True, False, None))
        self.assertTrue(detail)


if __name__ == "__main__":
    unittest.main()
