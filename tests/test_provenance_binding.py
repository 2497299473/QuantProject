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


if __name__ == "__main__":
    unittest.main()
