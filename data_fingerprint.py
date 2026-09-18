#!/usr/bin/env python3
"""数据集快照指纹（V4.1 ①，2026-09-18 改造）：data/ 实验输入文件的 sha256 manifest。

背景：特征/模型/证据的复现依赖数据文件不被静默替换。本脚本给 data/ 下的**实验
输入数据**（净值/个股K线/板块K线/日内快照/市场背景）生成 sha256 + size + mtime
清单，供回测/报告复现时对照「数据版本」是否一致。

**scope 硬边界（V4.1 ①：解自指环）**：本清单只收录 dataset 输入，**排除**
`model_registry/` 与 `models/`。原因——`model_registry.capture_provenance()` 会把
本文件的 sha256 写进 `registry.json`，若 registry 自身也在清单里，就形成

    manifest ──hash──▶ registry.json ──内嵌──▶ manifest 的 hash

的循环依赖：任何一次重新生成都会让「manifest 记录的 registry hash」立刻过时，
Evidence Contract 失去严格意义（实测 2026-09-18：manifest 记 registry 为
`2390f0f1…`，registry 实际为 `54619525…`，已不一致）。排除后依赖变成单向：

    Dataset Snapshot ──▶ Model Provenance（registry）──▶ 模型权重 / git commit

用法：python data_fingerprint.py [--out data/manifest.json]
输出：data/manifest.json（scope=dataset_inputs + snapshot_id + 生成时间 + 文件清单）
"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

SNAPSHOT_SCOPE = "dataset_inputs"
"""清单语义：本文件描述的是「实验输入数据快照」，不是「data/ 目录某一时刻的全量状态」。
消费方（shadow 契约 / registry provenance / 审计）据此理解绑定对象的含义。"""

# V4.1 ①（2026-09-18）：model_registry / models 必须排除——前者内嵌本文件的 sha256
# （model_registry.capture_provenance），后者是模型产物而非数据输入。留在清单里
# 即构成自指环，且换模型会无谓地扰动「数据快照」的含义。
EXCLUDE_DIRS = {"__pycache__", "manifest_history", "model_registry", "models"}
EXCLUDE_FILES = {"manifest.json"}
HISTORY_DIRNAME = "manifest_history"
PROVENANCE_DIRS = frozenset({"model_registry", "models"})
"""禁止入清单的「元数据/产物」目录名（环检测判据，见 _assert_acyclic）。"""


def _assert_acyclic(files: dict) -> None:
    """硬环检测：证明清单里没有任何 provenance 文件。

    不只是靠 EXCLUDE_DIRS 的隐式约定——排除集若被误改（如有人为了「更全」加回
    registry），下次生成会静默恢复循环依赖。这里显式抛错，把契约钉在代码里。
    """
    offenders = sorted(k for k in files
                       if any(part in PROVENANCE_DIRS for part in Path(k).parts))
    if offenders:
        raise RuntimeError(
            "manifest 自指环：provenance 文件进入数据快照清单 "
            f"{offenders[:5]}（共 {len(offenders)} 项）。"
            "registry.json 内嵌本清单的 sha256，一旦被清单收录，重生成即永远不一致。")


def _archive_previous(out: Path) -> Path | None:
    """归档上一版 manifest，保证历史记录内嵌的 sha256 永远可解析（2026-09-16）。

    shadow 每条记录内嵌 `data_manifest_sha256`（= 该条记录计算时 manifest 文件的
    sha256 前 16 位）。若直接覆盖，旧版本只剩 git 历史，未提交时则彻底丢失——
    审计时无法把记录对应回当时的数据快照。

    归档名取旧版 `generated_at`（同版本重复归档幂等跳过）；无旧文件返回 None。
    """
    if not out.exists():
        return None
    try:
        prev = json.loads(out.read_text(encoding="utf-8"))
        stamp = str(prev.get("generated_at") or "").replace("-", "").replace(":", "")
    except (OSError, json.JSONDecodeError):
        stamp = ""
    if len(stamp) != 15:                  # 期望 YYYYMMDDTHHMMSS
        stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(out.stat().st_mtime))
    hist = out.parent / HISTORY_DIRNAME
    hist.mkdir(parents=True, exist_ok=True)
    dest = hist / f"manifest_{stamp}.json"
    if not dest.exists():
        dest.write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot_id(files: dict) -> str:
    """内容寻址的快照 ID：sha256(scope + 排序后的 path:sha256 序列)[:16]。

    排除 mtime/size——它们只是副本属性；同一份数据从 Linux 拷到 Windows，
    sha256 不变但 mtime 全变，旧版会把「内容相同、时间不同」的两份清单
    当成两个快照，把「内容被替换、恰好同时刻」的当成同一个。
    """
    h = hashlib.sha256()
    h.update(SNAPSHOT_SCOPE.encode("utf-8"))
    for rel in sorted(files):
        h.update(f"{rel}\0{files[rel]['sha256']}\n".encode("utf-8"))
    return h.hexdigest()[:16]


def scan_files(base_dir: Path, data_dir: Path) -> dict:
    """扫描 data_dir，返回 {相对路径: {sha256,size,mtime}}（V4.1 ①）。

    抽成可注入函数是为测例能在 tempdir 里造一份带 registry.json / models\\*.pkl
    的小目录做**端到端**验证（而不只是断言常量集），同时不产生真实数据 churn。
    """
    files = {}
    for p in sorted(data_dir.rglob("*")):
        if not p.is_file():
            continue
        # 相对路径为准做排除判断：p.parts 含绝对路径祖先目录，若某上级目录恰好
        # 叫 models/ 会被误伤；同理键名也统一相对口径。
        rel = p.relative_to(base_dir)
        if p.name in EXCLUDE_FILES or rel.name in EXCLUDE_FILES:
            continue
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        # 键名用正斜杠：Windows 的 str(relative_to) 产出反斜杠，同一份数据在
        # Windows / WSL 两份 manifest 键不可比，跨环境复现对照失效（2026-09-18 实测）。
        files[rel.as_posix()] = {
            "sha256": _sha256(p),
            "size": p.stat().st_size,
            "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(p.stat().st_mtime)),
        }
    return files


def build_manifest(files: dict, root: Path) -> dict:
    """组装 manifest 正文；写盘前做硬环检测（宁可崩，不产出自指清单）。"""
    _assert_acyclic(files)
    return {
        "schema_version": "2.0",
        "scope": SNAPSHOT_SCOPE,
        # snapshot_id：内容寻址（对 scope + 排序后的 {path:sha256} 取 sha256 前 16 位）。
        # 同数据必同 ID，与生成时刻无关；旧版用「生成时刻」当 ID，重跑一次就多一个
        # 快照名，无法表达「同一份数据的第 N 次描述」。
        "snapshot_id": _snapshot_id(files),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "root": str(root),
        "n_files": len(files),
        "excluded_dirs": sorted(EXCLUDE_DIRS),
        "files": files,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DATA_DIR / "manifest.json"))
    args = ap.parse_args()

    if not DATA_DIR.exists():
        print(f"[fail] {DATA_DIR} 不存在")
        return 1

    files = scan_files(BASE_DIR, DATA_DIR)
    manifest = build_manifest(files, DATA_DIR)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    archived = _archive_previous(out)
    if archived:
        print(f"[hist] 上一版 manifest 已归档 → {archived}")
    tmp = out.with_suffix(".json.tmp")          # 原子写：避免半截文件被当成有效清单
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out)
    print(f"[ok] 数据集快照 scope={manifest['scope']} "
          f"snapshot_id={manifest['snapshot_id']} "
          f"{len(files)} 个文件 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
