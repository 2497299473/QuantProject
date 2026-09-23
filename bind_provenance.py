# -*- coding: utf-8 -*-
"""P1-8 审批绑定（2026-09-16）：把模型条目与其**可核验的数据快照 + 代码锚点**绑定。

审计项 P1-8 要求 registry 每个模型条目含：
  - ``data_manifest_sha256``：一份可解析 manifest 文件的 sha256 前 16 位（口径与
    shadow journal 内嵌、audit check_manifest_resolvable 的 allowed 集合一致）
  - ``git_commit``：该模型字节在 Git 历史中的锚点提交

绑定的证据标准（铁律 1「查档求证」/ 铁律 7「坦诚存疑」）：
  * 只绑**快照内逐文件记录了该 pkl、且哈希与当前条目一致**的 manifest——
    即快照独立证明了"这个模型在那份数据状态里就是这个字节"。
  * 有多个候选取 generated_at 最早的一份（最早在场证明；晚生成的快照不冒充
    "登记当时"，绑定时点差异在 provenance.method 里如实注明）。
  * git_commit 取**首次包含逐字节一致该 pkl 的提交**（本仓库历史始于 2026-09-08
    迁移基线，08-30/31 登记时点无提交存在——不伪造"登记时 HEAD"）。
  * 任一证据找不到 ⇒ 该模型拒绝绑定并报错，不写入不可证的值。

**新增**登记（register_model）已在 core/model_registry.py 内自动绑定当次
manifest + HEAD，无需本脚本；本脚本只负责历史条目的证据回填。

用法：
  python bind_provenance.py --dry-run          # 打印将要写入的内容
  python bind_provenance.py                    # 写入 registry（原子写）
  python bind_provenance.py --model forecast_v3.pkl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from core import model_registry as mr  # noqa: E402

MANIFEST = BASE_DIR / "data" / "manifest.json"
MANIFEST_HIST = BASE_DIR / "data" / "manifest_history"
SHA16 = 16  # 与 shadow_policy.py / audit_project.py 同口径


def sha256_16(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:SHA16]


def git(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(BASE_DIR), *args],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{r.stderr.strip()}")
    return r.stdout.strip()


def manifest_snapshots() -> list[dict]:
    """当前 + 归档的全部可解析 manifest（按 generated_at 升序）。"""
    paths: list[Path] = []
    if MANIFEST_HIST.is_dir():
        paths += sorted(MANIFEST_HIST.glob("manifest_*.json"))
    if MANIFEST.is_file():
        paths.append(MANIFEST)
    out = []
    for p in paths:
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({"path": p, "payload": payload,
                    "generated_at": str(payload.get("generated_at", ""))})
    out.sort(key=lambda s: s["generated_at"])
    return out


def attest_snapshot(snapshots: list[dict], pkl_name: str, pkl_sha: str) -> dict | None:
    """找最早**逐文件记录了该 pkl 且哈希一致**的快照（独立在场证明）。

    注意旧 manifest 键用 '/'、新版用 '\\' 作分隔 —— 两种形态都查。
    "最早" 由本函数内部排序保证，不依赖调用方传入顺序（测例 test_picks_earliest_*
    抓到过这个隐性契约，故显式排序）。
    """
    want = (pkl_sha or "")[:SHA16]
    if not want:
        return None
    for s in sorted(snapshots, key=lambda x: x.get("generated_at", "")):
        files = s["payload"].get("files") or {}
        for key in (f"data/models/{pkl_name}", f"data\\models\\{pkl_name}",
                    pkl_name):
            rec = files.get(key)
            if rec and str(rec.get("sha256", ""))[:SHA16] == want:
                return s
    return None


def first_identical_commit(pkl_name: str, pkl_sha: str) -> tuple[str | None, str | None]:
    """Git 历史中首个与该 pkl 逐字节一致的提交（本仓库 09-08 迁移基线起可查）。

    返回 (commit_hash, tracked_path)；找不到返回 (None, None)。
    """
    candidates = []
    for rel in (f"data/models/{pkl_name}",):
        # 只查主干历史里存在过的路径；报错即视为该路径无历史
        try:
            commits = git("log", "--all", "--format=%H", "--", rel).splitlines()
        except RuntimeError:
            commits = []
        for h in commits:  # 新→旧；遍历时保留**最旧**的一致提交
            try:
                raw = subprocess.run(
                    ["git", "-C", str(BASE_DIR), "cat-file", "blob", f"{h}:{rel}"],
                    capture_output=True).stdout
            except OSError:
                continue
            if raw and hashlib.sha256(raw).hexdigest()[:SHA16] == pkl_sha[:SHA16]:
                candidates.append((h, rel))
    if not candidates:
        return None, None
    oldest, rel = candidates[-1]
    return oldest, rel


def bind(model_keys: list[str], dry_run: bool) -> int:
    reg = mr.load_registry()
    snapshots = manifest_snapshots()
    if not snapshots:
        print("[fail] data/ 与 manifest_history/ 下无任何可解析快照 —— 拒绝绑定。")
        return 1
    failed = []
    changed = False
    for name in model_keys:
        entry = reg["models"].get(name)
        if entry is None:
            print(f"[fail] registry 无此条目：{name}")
            failed.append(name)
            continue
        if entry.get("data_manifest_sha256") and entry.get("git_commit"):
            print(f"[skip] {name} 已绑定：manifest={entry['data_manifest_sha256']} "
                  f"commit={str(entry['git_commit'])[:9]}")
            continue

        s = attest_snapshot(snapshots, name, entry.get("sha256", ""))
        if s is None:
            print(f"[fail] {name}：没有任何快照逐文件记录过该 sha256 的这组字节 —— "
                  f"无法诚实绑定（需重训后由 register_model 自动绑定）。")
            failed.append(name)
            continue
        commit, rel = first_identical_commit(name, entry.get("sha256", ""))
        if commit is None:
            print(f"[fail] {name}：Git 历史中找不到逐字节一致的提交 —— git_commit 不绑。")
            failed.append(name)
            continue

        man_sha = sha256_16(s["path"])
        entry["data_manifest_sha256"] = man_sha
        entry["git_commit"] = commit
        entry["provenance"] = {
            "bound_at": datetime.now().isoformat(timespec="seconds"),
            "manifest_file": (s["path"].name if s["path"].parent.name == "manifest_history"
                              else "manifest.json"),
            "manifest_generated_at": s["generated_at"],
            "registration_vs_snapshot": (
                "snapshot_after_registration"
                if str(entry.get("registered_at", "")) < s["generated_at"] else "ok"),
            "method": ("backfill_evidence_attestation："
                       "manifest 侧 = 最早逐文件记录且哈希一致的快照（在场证明，"
                       "非'登记当时'快照）；git_commit = 首个包含逐字节一致 pkl 的提交"),
            "git_tracked_path": rel,
        }
        changed = True
        print(f"[bind] {name}  manifest_sha16={man_sha}（快照 {s['generated_at']}）"
              f"  git_commit={commit[:9]}")

    if failed:
        print(f"== 拒绝写入：{len(failed)} 个条目证据不足 {failed} ==")
        return 1
    if not changed:
        print("== 无需写入（全部已绑定）==")
        return 0
    if dry_run:
        print("[dry-run] 未写入 registry。")
        return 0
    ok = mr._save_registry(reg)
    print("[ok] registry 已原子写入" if ok else "[fail] registry 写入失败")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", action="append", default=None,
                    help="只绑定指定模型键（可重复）；缺省 = registry 全部条目")
    ap.add_argument("--dry-run", action="store_true", help="只打印不写入")
    ap.add_argument("--normalize-paths", action="store_true",
                    help="V4.5：把 registry 里的旧绝对路径归一为仓库相对路径"
                         "（只动 path 字段，不动任何证据字段）；配 --dry-run 只报告")
    args = ap.parse_args()

    if args.normalize_paths:
        changed, details = mr.normalize_registry_paths(dry_run=args.dry_run)
        for d in details:
            print(f"[path] {d}")
        if not details:
            print("== 无需归一（无绝对路径条目）==")
            return 0
        print(f"== {len(details)} 条路径待归一 =="
              + ("（dry-run，未写入）" if args.dry_run else
                 ("，已写入" if changed else "，但写入失败")))
        return 0 if (args.dry_run or changed) else 1

    keys = args.model or sorted(mr.load_registry()["models"])
    return bind(keys, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
