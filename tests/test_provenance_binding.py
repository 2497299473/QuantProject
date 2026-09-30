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

REAL_BASE = Path(bp.__file__).resolve().parent   # 真实仓库根（不随测例 patch 漂移）


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


class TestSnapshotTrustworthiness(unittest.TestCase):
    """D3-02（2026-09-29 面 3 审查）：在场证明的可信度校。

    A 单指出的攻击面：``manifest_snapshots()`` 只做 JSON 解析，不校验快照出自
    data_fingerprint.py —— 任何人写一份 ``manifest_20660101T000000.json`` 塞进
    ``data/manifest_history/``，files 里记上目标 pkl 与 registry 的 sha，即构成
    「在场证明」（伪造者自己 commit 就有 git 痕迹，但痕迹不等于证明）。

    修法：attest 前复算 ``snapshot_id``（口径复用 data_fingerprint._snapshot_id）
    并校 ``n_files == len(files)``；复算失败 ⇒ 不可信，不进候选池。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _honest_manifest(self, files: dict, generated_at="2026-09-18T15:29:26") -> dict:
        """造一份**诚实**快照（含内容寻址 ID）。

        不经过 ``data_fingerprint.build_manifest``：它的 ``_assert_acyclic`` 会正确
        拒绝 files 里出现 ``data/models/*``（V4.1 ① 解自指环），而**历史格式件
        （20260831 / 20260916）确实逐文件记录了 pkl**——那是 09-18 改造前的合法
        形态。故直接复用 ``_snapshot_id`` 算内容寻址 ID（口径单一真源不变）。
        """
        import data_fingerprint as dfp
        return {
            "schema_version": "2.0",
            "scope": dfp.SNAPSHOT_SCOPE,
            "snapshot_id": dfp._snapshot_id(files),
            "generated_at": generated_at,
            "root": str(self.tmp / "data"),
            "n_files": len(files),
            "excluded_dirs": sorted(dfp.EXCLUDE_DIRS),
            "files": files,
        }

    def test_honest_manifest_is_trusted_and_content_addressed(self):
        payload = self._honest_manifest({"data/models/x.pkl": {
            "sha256": "ab" * 32, "size": 3, "mtime": "2026-09-18T15:29:26"}})
        ok, reason, has_cid = bp.snapshot_self_consistent(payload)
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "ok")
        self.assertTrue(has_cid, "build_manifest 必产 snapshot_id ⇒ 必须按内容寻址校")

    def test_forged_files_with_stolen_snapshot_id_rejected(self):
        """核心验收：篡改 files 内容但**沿用真 snapshot_id** ⇒ 复算值不等 ⇒ 拒。"""
        payload = self._honest_manifest({"data/models/x.pkl": {
            "sha256": "ab" * 32, "size": 3, "mtime": "2026-09-18T15:29:26"}})
        # 攻击：把在场证明的 pkl 换成伪造字节（sha 改、条数不变、snapshot_id 不动）
        payload["files"]["data/models/x.pkl"]["sha256"] = "ff" * 32
        ok, reason, has_cid = bp.snapshot_self_consistent(payload)
        self.assertFalse(ok)
        self.assertTrue(has_cid)
        self.assertIn("snapshot_id", reason)
        self.assertIn("不符", reason)

    def test_forged_added_file_rejected(self):
        """凭空加一条记录（snapshot_id 沿用、n_files 同步改）⇒ 复算值不等 ⇒ 拒。

        必须同步改 n_files：否则先被 n_files 自洽校拦下，测不到 snapshot_id 判据。
        """
        payload = self._honest_manifest({"data/holidays.json": {
            "sha256": "cd" * 32, "size": 1, "mtime": "2026-09-18T15:29:26"}})
        payload["files"]["data/models/x.pkl"] = {
            "sha256": "ab" * 32, "size": 3, "mtime": "2026-09-18T15:29:26"}
        payload["n_files"] = len(payload["files"])     # 同步计数，让 snapshot_id 成为唯一破绻
        ok, reason, has_cid = bp.snapshot_self_consistent(payload)
        self.assertFalse(ok)
        self.assertTrue(has_cid)
        self.assertIn("snapshot_id", reason)
        self.assertIn("不符", reason)

    def test_n_files_mismatch_rejected(self):
        """files 被增删而没同步计数 ⇒ 结构不自洽 ⇒ 拒。"""
        payload = self._honest_manifest({"data/holidays.json": {
            "sha256": "cd" * 32, "size": 1, "mtime": "2026-09-18T15:29:26"}})
        payload["n_files"] = 99
        ok, reason, _ = bp.snapshot_self_consistent(payload)
        self.assertFalse(ok)
        self.assertIn("n_files", reason)

    def test_n_files_type_confusion_rejected(self):
        payload = self._honest_manifest({"data/holidays.json": {
            "sha256": "cd" * 32, "size": 1, "mtime": "2026-09-18T15:29:26"}})
        for bad in ("6", None, True, 1.0):
            with self.subTest(n_files=repr(bad)):
                payload["n_files"] = bad
                ok, reason, _ = bp.snapshot_self_consistent(payload)
                self.assertFalse(ok)
                self.assertIn("n_files", reason)

    def test_malformed_snapshot_id_type_rejected(self):
        payload = self._honest_manifest({"data/holidays.json": {
            "sha256": "cd" * 32, "size": 1, "mtime": "2026-09-18T15:29:26"}})
        payload["snapshot_id"] = 12345          # 非 str ⇒ 畸形，不得静默当无 ID
        ok, reason, _ = bp.snapshot_self_consistent(payload)
        self.assertFalse(ok)
        self.assertIn("snapshot_id", reason)

    def test_files_missing_or_non_dict_rejected(self):
        for payload in ({}, {"files": "not-a-dict"}, {"files": None}):
            with self.subTest(payload=str(sorted(payload))):
                ok, reason, has_cid = bp.snapshot_self_consistent(payload)
                self.assertFalse(ok)
                self.assertFalse(has_cid)
                self.assertIn("files", reason)

    def test_legacy_without_snapshot_id_still_trusted_but_flagged(self):
        """**诚实边界（铁律 7）**：09-18 V4.1 ① 改造前的旧格式快照无 snapshot_id
        可比，只能校 n_files。仍可信（否则 A 单验收「现存两份真实历史快照回填
        回归通过」不成立），但 has_cid=False 供调用方如实标注强度，不假装密码学证明。"""
        legacy = {"generated_at": "2026-08-31T09:12:53", "n_files": 1,
                  "files": {"data/models/x.pkl": {"sha256": "ab" * 32}}}
        ok, reason, has_cid = bp.snapshot_self_consistent(legacy)
        self.assertTrue(ok, reason)
        self.assertFalse(has_cid)
        self.assertIn("legacy_no_snapshot_id", reason)

    def test_legacy_with_wrong_n_files_still_rejected(self):
        """旧格式不是免检金牌：n_files 不自洽照样拒。"""
        legacy = {"generated_at": "2026-08-31T09:12:53", "n_files": 5,
                  "files": {"data/models/x.pkl": {"sha256": "ab" * 32}}}
        ok, reason, _ = bp.snapshot_self_consistent(legacy)
        self.assertFalse(ok)
        self.assertIn("n_files", reason)

    def test_forged_history_file_excluded_from_candidate_pool(self):
        """端到端：manifest_history/ 里混入伪造件 ⇒ 不进候选池，诚实件保留。"""
        hist = self.tmp / "data" / "manifest_history"
        hist.mkdir(parents=True)
        forged = self._honest_manifest({"data/models/x.pkl": {
            "sha256": "ab" * 32, "size": 3, "mtime": "2026-09-18T15:29:26"}})
        forged["files"]["data/models/x.pkl"]["sha256"] = "ff" * 32   # 沿用真 ID
        (hist / "manifest_20660101T000000.json").write_text(
            json.dumps(forged, ensure_ascii=False), encoding="utf-8")
        honest = self._honest_manifest({"data/holidays.json": {
            "sha256": "cd" * 32, "size": 1, "mtime": "2026-09-18T15:29:26"}})
        (hist / "manifest_20260918T000000.json").write_text(
            json.dumps(honest, ensure_ascii=False), encoding="utf-8")
        (hist / "manifest_broken.json").write_text("{not json", encoding="utf-8")

        orig_m, orig_h = bp.MANIFEST, bp.MANIFEST_HIST
        bp.MANIFEST = self.tmp / "data" / "manifest.json"        # 不存在
        bp.MANIFEST_HIST = hist
        try:
            snaps = bp.manifest_snapshots()
        finally:
            bp.MANIFEST, bp.MANIFEST_HIST = orig_m, orig_h

        names = [s["path"].name for s in snaps]
        self.assertEqual(names, ["manifest_20260918T000000.json"],
                         f"伪造件与损坏件都不得进候选池，got {names}")
        self.assertTrue(snaps[0]["content_addressed"])

    def test_forged_snapshot_cannot_attest_pkl(self):
        """攻击闭环：伪造在场证明**不得**为任意 pkl 提供 attest。

        伪造者声称 x.pkl 的字节是 ``ab*32``，但 files 里真写的是 ``ff*32``（snapshot_id
        按真内容算）——旧实现只校 pkl 条目哈希与目标值相等就放行，而这里把条目
        改成目标值后 snapshot_id 即失配 ⇒ 整份快照不进候选池 ⇒ attest 不到。
        """
        target = "ab" * 32
        forged = self._honest_manifest({"data/models/x.pkl": {
            "sha256": "ff" * 32, "size": 3, "mtime": "2026-09-18T15:29:26"}})
        # 攻击：把条目改成目标 sha（声称这就是 x.pkl 的字节），snapshot_id 不同步
        forged["files"]["data/models/x.pkl"]["sha256"] = target
        trusted, reason, has_cid = bp.snapshot_self_consistent(forged)
        self.assertFalse(trusted, "伪造件必须被自洽校拒")
        self.assertTrue(has_cid)
        self.assertIn("snapshot_id", reason)
        # 不进候选池 ⇒ attest 拿不到（即使 pkl 条目哈希与目标值逐字相等）
        snap = {"path": self.tmp / "forged.json", "payload": forged,
                "generated_at": forged["generated_at"]}
        self.assertEqual(forged["files"]["data/models/x.pkl"]["sha256"], target,
                         "前置：条目确实已声称目标字节")
        pool = [snap] if trusted else []
        self.assertIsNone(bp.attest_snapshot(pool, "x.pkl", target))

    def test_honest_snapshot_can_attest_pkl(self):
        """反向保底：诚实快照（snapshot_id 与 files 相符）仍能为 pkl 提供在场证明。

        没有这条，上面所有拒例都可能是「一律拒」的假绿。
        """
        target = "ab" * 32
        honest = self._honest_manifest({"data/models/x.pkl": {
            "sha256": target, "size": 3, "mtime": "2026-09-18T15:29:26"}})
        trusted, reason, has_cid = bp.snapshot_self_consistent(honest)
        self.assertTrue(trusted, reason)
        self.assertTrue(has_cid)
        snap = {"path": self.tmp / "honest.json", "payload": honest,
                "generated_at": honest["generated_at"]}
        got = bp.attest_snapshot([snap], "x.pkl", target)
        self.assertIsNotNone(got, "诚实快照必须仍可 attest")
        self.assertEqual(got["payload"]["snapshot_id"], honest["snapshot_id"])

    def test_real_history_snapshots_still_pass_regression(self):
        """验收「现存两份真实历史快照回填回归通过」：只读真实 data/，不得写。"""
        snaps = bp.manifest_snapshots()
        names = [s["path"].name for s in snaps]
        for expected in ("manifest_20260831T091253.json",
                         "manifest_20260916T181240.json"):
            self.assertIn(expected, names, f"真实历史快照被误判为不可信：{expected}")
        by_name = {s["path"].name: s for s in snaps}
        # 09-18 前的旧格式：无内容寻址 ID（如实标注，不冒充强度）
        self.assertFalse(by_name["manifest_20260831T091253.json"]["content_addressed"])
        self.assertFalse(by_name["manifest_20260916T181240.json"]["content_addressed"])
        # 当前件（2026-09-18 后）：有 snapshot_id 且复算相等
        if "manifest.json" in by_name:
            self.assertTrue(by_name["manifest.json"]["content_addressed"])

    def test_real_registry_attest_and_dry_run_unchanged(self):
        """验收「bind --dry-run 在真实 registry 全条目仍可判定」+ 回填值不变。"""
        reg = model_registry.load_registry()
        snaps = bp.manifest_snapshots()
        self.assertTrue(snaps)
        for name, entry in sorted(reg["models"].items()):
            sha = str(entry.get("sha256") or "")
            s = bp.attest_snapshot(snaps, name, sha)
            self.assertIsNotNone(s, f"{name} 全长比对后 attest 失败（D3-06 回归）")
            self.assertEqual(bp.sha256_16(s["path"]), entry.get("data_manifest_sha256"),
                             f"{name} 回填的 manifest_sha16 变了")
            commit, rel = bp.first_identical_commit(name, sha)
            self.assertEqual(commit, entry.get("git_commit"),
                             f"{name} 回填的 git_commit 变了")
            self.assertEqual(rel, "data/models/" + name)
        # dry-run 全条目仍可判定（不写入）
        self.assertEqual(bp.bind(sorted(reg["models"]), dry_run=True), 0)


class _TempGitRepoMixin:
    """在 tempdir 里造**真实** git 历史（D3-03 要求断言具体 commit 值）。"""

    MODEL_REL = "data/models/x.pkl"

    def _git(self, *args: str) -> str:
        r = subprocess.run(["git", "-C", str(self.repo), *args],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} 失败：{r.stderr.strip()}")
        return r.stdout.strip()

    def _init_repo(self) -> str:
        self.repo = Path(tempfile.mkdtemp())
        (self.repo / "data" / "models").mkdir(parents=True)
        self._git("init", "-q")
        self._git("config", "user.email", "d3-03@test.local")
        self._git("config", "user.name", "d3-03-test")
        self._git("config", "commit.gpgsign", "false")
        (self.repo / "README.md").write_text("seed\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "seed")
        return self._git("rev-parse", "HEAD")

    def _commit_bytes(self, data: bytes, msg: str) -> str:
        (self.repo / "data" / "models" / "x.pkl").write_bytes(data)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", msg)
        return self._git("rev-parse", "HEAD")

    def _patch_bp_base(self):
        """first_identical_commit 用模块级 BASE_DIR 拼 git -C ⇒ 可注入 temp repo。"""
        self._orig_base = bp.BASE_DIR
        bp.BASE_DIR = self.repo

    def _restore_bp_base(self):
        bp.BASE_DIR = self._orig_base


class TestFirstIdenticalCommit(unittest.TestCase, _TempGitRepoMixin):
    """D3-03（2026-09-29 面 3 审查）：``first_identical_commit`` 三类测例。

    A 单事实：该函数**零测试覆盖**（grep tests/ 0 命中）；``--all`` 检索含任意
    分支且不验证 commit 对 HEAD 可达（分支删除/GC 后绑定腐化）。

    修法：① 补三类测例（同名重建不认旧血统 / 无历史拒绑 / 多候选取最旧）；
    ② 加 ``git merge-base --is-ancestor`` 可达性校验。

    注：A 单建议的「检索限定 main 历史」与既有设计意图冲突——本仓库历史始于
    09-08 迁移基线，登记时点（08-30/31）无提交存在，且并行修复分支
    （v4.4-r2fix/r4fix）的 blob 也是合法血统来源；限定 main 会把它们全拒掉。
    故取 A 单给的第二个选项（可达性校验），效果等价于「只用能复核的锚点」，
    代价是不拒绝并行分支上的可达提交（如实记录，供 A 复核该取舍）。
    """

    SHA = "ab" * 32

    def setUp(self):
        self.base_commit = self._init_repo()
        self._patch_bp_base()

    def tearDown(self):
        self._restore_bp_base()
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_finds_first_byte_identical_commit(self):
        c1 = self._commit_bytes(b"model-bytes-v1", "add x.pkl")
        got, rel = bp.first_identical_commit("x.pkl", hashlib.sha256(b"model-bytes-v1").hexdigest())
        self.assertEqual(got, c1, "必须返回具体 commit 值")
        self.assertEqual(rel, self.MODEL_REL)

    def test_same_name_rebuild_pins_each_bytes_to_own_commit(self):
        """① 同名重建（字节变了）：每串字节只锚定到**它存在过的提交**。

        语义钉死：查 v2（重建后）字节 ⇒ 得 c2，**不得继承** c1（血统串线）；
        查 v1（历史）字节 ⇒ 仍得 c1（git 历史里它确实存在过，这不是误认）。
        旧截断口径的风险形态（前 16 位碰撞认串血统）由 D3-06 测例另行钉住。
        """
        c1 = self._commit_bytes(b"model-bytes-v1", "add x.pkl")
        c2 = self._commit_bytes(b"model-bytes-v2-REBUILT", "rebuild x.pkl 同名不同字节")
        got_v2, rel2 = bp.first_identical_commit(
            "x.pkl", hashlib.sha256(b"model-bytes-v2-REBUILT").hexdigest())
        got_v1, rel1 = bp.first_identical_commit(
            "x.pkl", hashlib.sha256(b"model-bytes-v1").hexdigest())
        self.assertEqual(got_v2, c2, "重建后的字节必须锚自己的提交，不得继承旧 commit")
        self.assertEqual(got_v1, c1, "历史字节锚历史提交（存在即可证，不是误认）")
        self.assertNotEqual(got_v1, got_v2)
        self.assertEqual(rel1, self.MODEL_REL)
        self.assertEqual(rel2, self.MODEL_REL)

    def test_no_history_returns_none(self):
        """② 无历史 ⇒ (None, None)，bind() 据此 fail-closed 拒绑。"""
        got, rel = bp.first_identical_commit("never_committed.pkl", self.SHA)
        self.assertEqual((got, rel), (None, None))

    def test_multiple_identical_picks_oldest(self):
        """③ 多候选（同一字节被反复提交）取**最旧**的一致提交。"""
        c1 = self._commit_bytes(b"same-bytes", "first")
        self._commit_bytes(b"other", "interlude")            #  intervening 不同字节
        self._commit_bytes(b"same-bytes", "second same")     # 同字节再次出现
        got, _ = bp.first_identical_commit(
            "x.pkl", hashlib.sha256(b"same-bytes").hexdigest())
        self.assertEqual(got, c1, "必须取最旧候选，不得取最新")

    def test_unreachable_branch_commit_filtered_out(self):
        """② 可达性：只存在于**旁支**的 blob 不得充当血统锚点。

        master 上该 pkl 从未出现；分支 tmp-lineage 上有 ⇒ 切回 master 后
        merge-base --is-ancestor 判不可达 ⇒ (None, None) ⇒ bind fail-closed。
        旧实现会返回旁支 commit（分支删除/GC 后绑定即腐化且不可复核）。
        """
        self._git("checkout", "-q", "-b", "tmp-lineage")
        side_c = self._commit_bytes(b"side-only-bytes", "side branch add x.pkl")
        self._git("checkout", "-q", "master")
        got, rel = bp.first_identical_commit(
            "x.pkl", hashlib.sha256(b"side-only-bytes").hexdigest())
        self.assertEqual((got, rel), (None, None),
                         f"旁支 commit {side_c[:9]} 对 HEAD 不可达，不得绑")
        # 前置自证：该 blob 确实存在于仓库（证明拒绝来自可达性过滤，而非查不到）
        self.assertIn(side_c, self._git("log", "--all", "--format=%H", "--", self.MODEL_REL))

    def test_reachable_commit_on_branch_still_accepted(self):
        """可达性过滤的**反向保底**：分支已合并进 master（对 HEAD 可达）⇒ 仍可绑。

        不得把并行修复分支上的合法血统一起打死（本仓库 v4.4-r2fix/r4fix 实况）。
        """
        self._git("checkout", "-q", "-b", "merged-lineage")
        c = self._commit_bytes(b"merged-bytes", "branch add x.pkl")
        self._git("checkout", "-q", "master")
        self._git("merge", "-q", "--no-ff", "-m", "merge merged-lineage", "merged-lineage")
        got, _ = bp.first_identical_commit(
            "x.pkl", hashlib.sha256(b"merged-bytes").hexdigest())
        self.assertEqual(got, c, "合并后对 HEAD 可达 ⇒ 必须仍能作为血统锚点")

    def test_real_registry_entry_unchanged(self):
        """验收「真实 registry 现有条目回填结果不变（6e0c9dd66）」——真实仓库只读。

        不依赖 _orig_base（若前序测例 setUp 中途抛异常，泄漏的 patch 值会污染
        它）——直接钉 REAL_BASE（由 bp.__file__ 算出，永为真实仓库根）。
        """
        patched = bp.BASE_DIR
        bp.BASE_DIR = REAL_BASE
        try:
            reg = model_registry.load_registry()
            for name, entry in sorted(reg["models"].items()):
                got, rel = bp.first_identical_commit(name, str(entry.get("sha256") or ""))
                self.assertEqual(got, "6e0c9dd66be02666bcbceb7c48c867710d9e8971",
                                 f"{name} 的真实血统锚点变了（D3-03/D3-06 回归）")
                self.assertEqual(got, entry.get("git_commit"))
                self.assertEqual(rel, "data/models/" + name)
        finally:
            bp.BASE_DIR = patched


class TestBindFailClosedOnMissingLineage(unittest.TestCase, _TempGitRepoMixin):
    """D3-03 验收：``bind()`` 对无历史模型输出 ``[fail] … git_commit 不绑`` 且退出码 1。"""

    def setUp(self):
        self.base_commit = self._init_repo()
        self._patch_bp_base()
        # 造一份可解析且自洽的 manifest（attest 侧能过，专测 git 侧 fail-closed）。
        # 不用 build_manifest：_assert_acyclic 会（正确地）拒 data/models/* 入清单，
        # 而历史格式件确实逐文件记录了 pkl（见 TestSnapshotTrustworthiness 注）。
        import data_fingerprint as dfp
        hist = self.repo / "data" / "manifest_history"
        hist.mkdir(parents=True, exist_ok=True)
        files = {"data/models/nohistory.pkl": {"sha256": "ab" * 32, "size": 3,
                                               "mtime": "2026-09-18T15:29:26"}}
        payload = {
            "schema_version": "2.0", "scope": dfp.SNAPSHOT_SCOPE,
            "snapshot_id": dfp._snapshot_id(files),
            "generated_at": "2026-09-18T00:00:00",
            "root": str(self.repo / "data"), "n_files": len(files),
            "excluded_dirs": sorted(dfp.EXCLUDE_DIRS), "files": files,
        }
        self.manifest_path = hist / "manifest_20260918T000000.json"
        self.manifest_path.write_text(json.dumps(payload, ensure_ascii=False),
                                      encoding="utf-8")
        self._orig_m, self._orig_h = bp.MANIFEST, bp.MANIFEST_HIST
        bp.MANIFEST = self.repo / "data" / "manifest.json"
        bp.MANIFEST_HIST = hist

    def tearDown(self):
        bp.MANIFEST, bp.MANIFEST_HIST = self._orig_m, self._orig_h
        self._restore_bp_base()
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_bind_refuses_when_no_identical_commit(self):
        reg = {"models": {"nohistory.pkl": {
            "path": "data/models/nohistory.pkl", "sha256": "ab" * 32,
            "registered_at": "2026-09-18T00:00:00"}}}
        orig_load, orig_save = model_registry.load_registry, model_registry._save_registry
        written = []
        model_registry.load_registry = lambda: json.loads(json.dumps(reg))
        model_registry._save_registry = lambda r: written.append(r) or True
        import io
        import contextlib
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = bp.bind(["nohistory.pkl"], dry_run=False)
        finally:
            model_registry.load_registry = orig_load
            model_registry._save_registry = orig_save
        out = buf.getvalue()
        self.assertEqual(rc, 1, f"无血统锚点必须退出码 1，输出：{out}")
        self.assertIn("git_commit 不绑", out)
        self.assertIn("拒绝写入", out)
        self.assertEqual(written, [], "拒绑路径一字不得写入 registry")


class TestFullLengthShaIdentity(unittest.TestCase):
    """D3-06（2026-09-29 面 3 审查）：pkl 字节身份比对升全长，manifest 身份保留 16 位。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prefix16_collision_no_longer_attests(self):
        """前 16 位相同、后 48 位不同的伪造字节 **不得** 冒充在场（旧截断口径会放行）。"""
        real = "ab" * 32
        forged = "ab" * 8 + "ff" * 24                    # 前 16 hex 完全相同
        self.assertEqual(real[:16], forged[:16], "前置：前 16 位必须相同才有对抗意义")
        self.assertNotEqual(real, forged)
        p = self.tmp / "m.json"
        p.write_text(json.dumps({"generated_at": "2026-08-31T00:00:00", "n_files": 1,
                                 "files": {"data/models/x.pkl": {"sha256": forged}}}),
                     encoding="utf-8")
        snap = {"path": p, "payload": json.loads(p.read_text(encoding="utf-8")),
                "generated_at": "2026-08-31T00:00:00"}
        self.assertIsNone(bp.attest_snapshot([snap], "x.pkl", real),
                          "全长比对下伪造前缀必须被拒")
        # 对照：真值仍可通过（证明拒绝来自全长比对而非其他原因）
        snap["payload"]["files"]["data/models/x.pkl"]["sha256"] = real
        self.assertIsNotNone(bp.attest_snapshot([snap], "x.pkl", real))

    def test_manifest_file_identity_keeps_sha16_convention(self):
        """口径注释同步：sha256_16 仍用于 manifest **文件**身份（三处锁死不动）。"""
        p = self.tmp / "manifest.json"
        p.write_text('{"generated_at": "2026-09-18T15:29:26"}', encoding="utf-8")
        got = bp.sha256_16(p)
        self.assertEqual(len(got), 16)
        self.assertEqual(got, hashlib.sha256(p.read_bytes()).hexdigest()[:16])
        self.assertEqual(bp.SHA16, 16)

    def test_attest_accepts_uppercase_and_whitespace_variants(self):
        """全长比对做了 strip/lower 归一：大小写与首尾空白不得造成假失配。"""
        p = self.tmp / "m.json"
        p.write_text(json.dumps({"generated_at": "2026-08-31T00:00:00", "n_files": 1,
                                 "files": {"data/models/x.pkl": {"sha256": ("AB" * 32)}}}),
                     encoding="utf-8")
        snap = {"path": p, "payload": json.loads(p.read_text(encoding="utf-8")),
                "generated_at": "2026-08-31T00:00:00"}
        self.assertIsNotNone(bp.attest_snapshot([snap], "x.pkl", "ab" * 32))


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
