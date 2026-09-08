#!/usr/bin/env python3
"""数据快照指纹（2026-08-31，GPT 四审 P1）：data/ 关键文件 sha256 manifest。

背景：特征/模型/证据的复现依赖数据文件不被静默替换。本脚本给 data/ 下
（模型权重、registry、intraday、缓存等）生成 sha256 + size + mtime 清单，
供回测/报告复现时对照「数据版本」是否一致。

用法：python3 data_fingerprint.py [--out data/manifest.json]
输出：data/manifest.json（含生成时间 + 文件清单）
"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

EXCLUDE_DIRS = {"__pycache__"}
EXCLUDE_FILES = {"manifest.json"}


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DATA_DIR / "manifest.json"))
    args = ap.parse_args()

    if not DATA_DIR.exists():
        print(f"[fail] {DATA_DIR} 不存在")
        return 1

    files = {}
    for p in sorted(DATA_DIR.rglob("*")):
        if not p.is_file():
            continue
        if p.name in EXCLUDE_FILES:
            continue
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        rel = str(p.relative_to(BASE_DIR))
        files[rel] = {
            "sha256": _sha256(p),
            "size": p.stat().st_size,
            "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(p.stat().st_mtime)),
        }

    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "root": str(DATA_DIR),
        "n_files": len(files),
        "files": files,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] 数据指纹 {len(files)} 个文件 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
