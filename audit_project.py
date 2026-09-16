#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V4 项目审计契约（2026-09-16）。

把「证据链是否完整」从文档里的承诺变成可执行检查：

    python3 audit_project.py
    python3 audit_project.py --json output/audit_report.json

纪律
----
- **只读**：不修改任何项目文件（``--json`` 指定的输出文件除外）；**零网络**。
- **可重复**：同一工作区两次运行结论一致（时间敏感项除外）。
- **机器可读**：每条给出 PASS / FAIL / WARN，末行给 RESULT 汇总与生产闸门结论。
  以后无论谁来复审，只读这一份机器事实，不再基于报告做二次解释。

退出码
------
0 = 无 FAIL（可能含 WARN）
2 = 存在 FAIL（生产应视为 BLOCKED）
1 = 审计脚本自身异常
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA = BASE_DIR / "data"
OUTPUT = BASE_DIR / "output"

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
REGISTRY = DATA / "model_registry" / "registry.json"
MANIFEST = DATA / "manifest.json"
MANIFEST_HIST = DATA / "manifest_history"
ACTIVE_JOURNAL = OUTPUT / "shadow_actions.jsonl"
LEGACY_ARCHIVE = OUTPUT / "shadow_actions_legacy_invalid.jsonl"
RUN_MANIFEST_DIR = OUTPUT / "run_manifest"
AUDIT_SELF = Path(__file__).resolve()      # 排除本脚本自身字面量，避免自指误报

EXPECTED_CHANNELS = ("approved_full", "prereg_degraded", "legacy_invalid")
AXIS_ORDER = {"P0-研究有效性": 0, "P1-证据链": 1, "P2-工程卫生": 2}


@dataclass
class Check:
    cid: str
    axis: str
    title: str
    status: str
    detail: str = ""


@dataclass
class Audit:
    checks: list = field(default_factory=list)

    def add(self, cid: str, axis: str, title: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(cid, axis, title, status, detail))

    def counts(self) -> dict:
        out = {PASS: 0, FAIL: 0, WARN: 0}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    def sorted_checks(self) -> list:
        return sorted(self.checks, key=lambda c: (AXIS_ORDER.get(c.axis, 9), c.cid))


# ---------------------------------------------------------------- 基础工具

def sha256_file(p: Path) -> str | None:
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def git_lines(*args: str):
    """返回 (lines, err)；lines 为 None 表示 git 不可用或失败。"""
    try:
        r = subprocess.run(["git", *args], cwd=str(BASE_DIR),
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, type(exc).__name__
    if r.returncode != 0:
        return None, (r.stderr or "").strip()[:200]
    return [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()], ""


def classify(rec: dict) -> str:
    """镜像 shadow_policy.record_channel 的极小判定。

    不直接 import shadow_policy，避免审计脚本拉起 forecast_engine 等重依赖。
    shadow_policy 侧的通道定义存在性由 P1-5 单独检查，防止两边悄悄漂移。
    """
    if rec.get("evidence_validity") == "legacy_invalid":
        return "legacy_invalid"
    mode = (rec.get("contract") or {}).get("promotion_mode")
    if mode in ("approved_full", "prereg_degraded"):
        return mode
    return "unclassified"


def iter_journal(path: Path):
    if not path.is_file():
        return
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            yield json.loads(ln)
        except json.JSONDecodeError:
            continue


def _registry() -> dict:
    return load_json(REGISTRY) or {}


def _models() -> dict:
    return (_registry().get("models") or {})


# ---------------------------------------------------------------- P0 研究有效性

def check_pit(a: Audit) -> None:
    src = BASE_DIR / "core" / "lookthrough.py"
    txt = src.read_text(encoding="utf-8") if src.is_file() else ""
    if not txt:
        a.add("P0-1", "P0-研究有效性", "PIT 使用真实公告日", WARN, "core/lookthrough.py 未找到")
        return
    fixed_lines = [i + 1 for i, ln in enumerate(txt.splitlines()) if "_QTR_LAG =" in ln]
    has_announce = bool(re.search(r"ann_date|announce_date|公告日|披露日", txt))
    if fixed_lines and not has_announce:
        a.add("P0-1", "P0-研究有效性", "PIT 使用真实公告日（非固定滞后）", FAIL,
              "仍用固定滞后 _QTR_LAG（core/lookthrough.py "
              + ",".join(f"L{n}" for n in fixed_lines) + "）；未见公告日字段")
    elif fixed_lines and has_announce:
        a.add("P0-1", "P0-研究有效性", "PIT 使用真实公告日（非固定滞后）", WARN,
              "同时存在固定滞后与公告日逻辑，需人工确认生效路径")
    else:
        a.add("P0-1", "P0-研究有效性", "PIT 使用真实公告日（非固定滞后）", PASS, "")


def check_holdings_privacy(a: Audit) -> None:
    tracked, err = git_lines("ls-files")
    if tracked is None:
        a.add("P0-2", "P0-研究有效性", "持仓文件未进入版本库", WARN, f"git 不可用：{err}")
        return
    hits = [t for t in tracked if t.endswith("holdings.json")]
    a.add("P0-2", "P0-研究有效性", "持仓文件未进入版本库",
          PASS if not hits else FAIL,
          "；".join(hits) if hits else "未被跟踪")


def check_manifest_resolvable(a: Audit) -> None:
    if not MANIFEST.is_file():
        a.add("P0-3", "P0-研究有效性", "数据快照可回溯（内嵌 hash 可解析）", FAIL,
              "data/manifest.json 缺失")
        return
    allowed = {sha256_file(MANIFEST)[:16]}
    hist = sorted(MANIFEST_HIST.glob("manifest_*.json")) if MANIFEST_HIST.is_dir() else []
    for h in hist:
        digest = sha256_file(h)
        if digest:
            allowed.add(digest[:16])
    embedded: dict = {}
    for rec in iter_journal(ACTIVE_JOURNAL) or ():
        h = (rec.get("contract") or {}).get("data_manifest_sha256")
        if h:
            embedded[h] = embedded.get(h, 0) + 1
    unresolved = {h: n for h, n in embedded.items() if h not in allowed}
    detail = (f"当前 {sha256_file(MANIFEST)[:16]}；归档 {len(hist)} 版；"
              f"journal 内嵌 {len(embedded)} 个 hash（{sum(embedded.values())} 条记录）")
    if unresolved:
        a.add("P0-3", "P0-研究有效性", "数据快照可回溯（内嵌 hash 可解析）", FAIL,
              detail + f"；无法解析：{unresolved}")
    else:
        a.add("P0-3", "P0-研究有效性", "数据快照可回溯（内嵌 hash 可解析）", PASS, detail)


def check_manifest_freshness(a: Audit) -> None:
    if not MANIFEST.is_file():
        a.add("P0-4", "P0-研究有效性", "数据快照新鲜度", FAIL, "data/manifest.json 缺失")
        return
    age = (datetime.now() - datetime.fromtimestamp(MANIFEST.stat().st_mtime)).days
    payload = load_json(MANIFEST) or {}
    detail = (f"generated_at={payload.get('generated_at')} · n_files={payload.get('n_files')} · "
              f"年龄 {age} 天")
    a.add("P0-4", "P0-研究有效性", "数据快照新鲜度",
          WARN if age > 7 else PASS, detail + ("（>7 天，建议重生成）" if age > 7 else ""))


# ---------------------------------------------------------------- P1 证据链

def check_registry_hash(a: Audit) -> None:
    models = _models()
    if not models:
        a.add("P1-1", "P1-证据链", "模型权重 sha256 与注册表一致", FAIL,
              "registry.json 缺失或无 models")
        return
    bad = []
    for name, entry in models.items():
        p = DATA / "models" / Path(str(entry.get("path") or name)).name
        got = sha256_file(p)
        if got is None:
            bad.append(f"{name} 权重缺失")
        elif got != entry.get("sha256"):
            bad.append(f"{name} sha256 不符")
    a.add("P1-1", "P1-证据链", "模型权重 sha256 与注册表一致",
          PASS if not bad else FAIL,
          f"{len(models)} 个模型" + ("；" + "；".join(bad) if bad else ""))


def check_validation_binding(a: Audit) -> None:
    models = _models()
    bad, n = [], 0
    for name, entry in models.items():
        v = entry.get("validation") or {}
        rf = v.get("report_file")
        if not rf:
            continue
        n += 1
        got = sha256_file(BASE_DIR / rf)
        if got is None:
            bad.append(f"{name} 报告缺失 {rf}")
        elif got != v.get("report_sha256"):
            bad.append(f"{name} 报告 sha256 不符")
    a.add("P1-2", "P1-证据链", "验证报告 sha256 与注册表绑定一致",
          PASS if not bad else FAIL,
          f"{n} 份报告" + ("；" + "；".join(bad) if bad else ""))


def check_promotion_consistency(a: Audit) -> None:
    models = _models()
    cfg = load_json(BASE_DIR / "config.json") or {}
    ready = bool((cfg.get("forecast") or {}).get("model_ready", False))
    blocked = [n for n, e in models.items()
               if (e.get("promotion") or {}).get("status") == "blocked"]
    if ready and blocked:
        a.add("P1-3", "P1-证据链", "promotion 与 config.model_ready 一致", FAIL,
              f"model_ready=true 但 {len(blocked)} 个模型 promotion=blocked：{'、'.join(blocked)}")
    else:
        a.add("P1-3", "P1-证据链", "promotion 与 config.model_ready 一致", PASS,
              f"model_ready={ready}；blocked {len(blocked)} 个")


def check_history_validated(a: Audit) -> None:
    cfg = load_json(BASE_DIR / "config.json") or {}
    gates = (cfg.get("decision") or {}).get("gates") or {}
    if "history_validated" not in gates:
        a.add("P1-4", "P1-证据链", "动作层历史验证硬门禁", FAIL,
              "config.decision.gates.history_validated 缺失")
        return
    hv = gates.get("history_validated")
    a.add("P1-4", "P1-证据链", "动作层历史验证硬门禁",
          PASS if hv is False else FAIL,
          f"history_validated={hv}" + ("" if hv is False else "（须有 backtest_action 稳定超额证据才可置 true）"))


def check_approval_binding(a: Audit) -> None:
    models = _models()
    if not models:
        a.add("P1-8", "P1-证据链", "审批绑定数据快照 + 代码版本", FAIL, "registry 缺失")
        return
    missing = []
    for name, entry in models.items():
        v = entry.get("validation") or {}
        has_snap = bool(entry.get("data_manifest_sha256") or v.get("data_manifest_sha256"))
        has_commit = bool(entry.get("git_commit") or v.get("git_commit"))
        lack = []
        if not has_snap:
            lack.append("data_manifest_sha256")
        if not has_commit:
            lack.append("git_commit")
        if lack:
            missing.append(f"{name} 缺 {'/'.join(lack)}")
    a.add("P1-8", "P1-证据链", "审批绑定数据快照 + 代码版本",
          PASS if not missing else FAIL, "；".join(missing))


def check_shadow_channels(a: Audit) -> None:
    src = BASE_DIR / "shadow_policy.py"
    txt = src.read_text(encoding="utf-8") if src.is_file() else ""
    if not txt:
        a.add("P1-5", "P1-证据链", "影子证据通道隔离", WARN, "shadow_policy.py 未找到")
        return
    ok = all(c in txt for c in EXPECTED_CHANNELS) and "load_records_by_channel" in txt
    a.add("P1-5", "P1-证据链", "影子证据通道隔离",
          PASS if ok else FAIL,
          "" if ok else "未见 CHANNELS / load_records_by_channel")


def check_legacy_archived(a: Audit) -> None:
    if not ACTIVE_JOURNAL.is_file():
        a.add("P1-6", "P1-证据链", "legacy_invalid 已移出活跃流", WARN,
              "活跃 journal 不存在（尚无影子记录）")
        return
    buckets: dict = {}
    for rec in iter_journal(ACTIVE_JOURNAL) or ():
        ch = classify(rec)
        buckets[ch] = buckets.get(ch, 0) + 1
    n_legacy = buckets.get("legacy_invalid", 0)
    a.add("P1-6", "P1-证据链", "legacy_invalid 已移出活跃流",
          PASS if n_legacy == 0 else FAIL,
          f"活跃流分布 {buckets}"
          + ("" if n_legacy == 0 else f"；仍有 {n_legacy} 条 legacy 混在活跃流（可 --arch 归档）"))


def check_run_manifest(a: Audit) -> None:
    txt = (BASE_DIR / "run.py").read_text(encoding="utf-8") if (BASE_DIR / "run.py").is_file() else ""
    tri = "DEGRADED" in txt and "return 2" in txt
    files = sorted(RUN_MANIFEST_DIR.glob("run_manifest_*.json")) if RUN_MANIFEST_DIR.is_dir() else []
    if not files:
        a.add("P1-7", "P1-证据链", "运行清单已实际产出",
              WARN if tri else FAIL,
              "三态退出码已实现但 output/run_manifest/ 无产物（证据链尚未兑现）")
        return
    payload = load_json(files[-1]) or {}
    a.add("P1-7", "P1-证据链", "运行清单已实际产出", PASS,
          f"{len(files)} 份；最新 {files[-1].name} status={payload.get('status')}")


# ---------------------------------------------------------------- P2 工程卫生

def check_atomic_write(a: Audit) -> None:
    src = BASE_DIR / "data_fingerprint.py"
    txt = src.read_text(encoding="utf-8") if src.is_file() else ""
    ok = bool(txt) and ".replace(" in txt and ".tmp" in txt
    a.add("P2-1", "P2-工程卫生", "manifest 原子写", PASS if ok else WARN,
          "" if ok else "未见 tmp + replace 原子写模式")


def check_tushare_https(a: Audit) -> None:
    hits = []
    for p in BASE_DIR.rglob("*.py"):
        if any(part in (".venv", ".venv-lab", "__pycache__", ".git", "backups")
               for part in p.parts):
            continue
        if p.resolve() == AUDIT_SELF:       # 本脚本含该字面量做检查，不自指误报
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for i, ln in enumerate(txt.splitlines(), 1):
            if "http://api.tushare.pro" in ln:
                hits.append(f"{p.relative_to(BASE_DIR)}:L{i}")
    a.add("P2-2", "P2-工程卫生", "Tushare 走 HTTPS", PASS if not hits else WARN,
          "；".join(hits) if hits else "")


def check_test_layering(a: Audit) -> None:
    cfg_present = any((BASE_DIR / n).is_file()
                      for n in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"))
    n_tests = len(list((BASE_DIR / "tests").glob("test_*.py"))) if (BASE_DIR / "tests").is_dir() else 0
    markers = False
    for n in ("pytest.ini", "pyproject.toml", "setup.cfg"):
        p = BASE_DIR / n
        if p.is_file():
            try:
                if "markers" in p.read_text(encoding="utf-8"):
                    markers = True
            except (OSError, UnicodeDecodeError):
                pass
    a.add("P2-3", "P2-工程卫生", "测试分层（fast/slow）",
          PASS if (cfg_present and markers) else WARN,
          f"测试文件 {n_tests} 个；pytest 配置 {'有' if cfg_present else '无'}；"
          f"markers {'有' if markers else '无'}")


def check_env_untracked(a: Audit) -> None:
    tracked, err = git_lines("ls-files", ".env")
    if tracked is None:
        a.add("P2-4", "P2-工程卫生", "密钥文件未入库", WARN, f"git 不可用：{err}")
        return
    hits = [t for t in tracked if t == ".env"]
    a.add("P2-4", "P2-工程卫生", "密钥文件未入库",
          PASS if not hits else FAIL, "；".join(hits))


# ---------------------------------------------------------------- 主流程

def run_audit() -> Audit:
    a = Audit()
    check_pit(a)
    check_holdings_privacy(a)
    check_manifest_resolvable(a)
    check_manifest_freshness(a)
    check_registry_hash(a)
    check_validation_binding(a)
    check_promotion_consistency(a)
    check_history_validated(a)
    check_shadow_channels(a)
    check_legacy_archived(a)
    check_run_manifest(a)
    check_approval_binding(a)
    check_atomic_write(a)
    check_tushare_https(a)
    check_test_layering(a)
    check_env_untracked(a)
    return a


def main() -> int:
    ap = argparse.ArgumentParser(description="V4 项目审计契约（只读 / 零网络）")
    ap.add_argument("--json", help="把结果写成 JSON（相对项目根）")
    args = ap.parse_args()

    try:
        a = run_audit()
    except Exception as exc:                       # noqa: BLE001
        print(f"[error] 审计脚本异常：{type(exc).__name__}: {exc}")
        return 1

    print("== V4 审计契约 ==")
    print(f"项目：{BASE_DIR}")
    axis_now = None
    for c in a.sorted_checks():
        if c.axis != axis_now:
            axis_now = c.axis
            print(f"\n-- {axis_now} --")
        line = f"[{c.status:4}] {c.cid} {c.title}"
        if c.detail:
            line += f" —— {c.detail}"
        print(line)

    n = a.counts()
    print(f"\nRESULT: {n[PASS]} PASS / {n[FAIL]} FAIL / {n[WARN]} WARN")
    print(f"PRODUCTION: {'BLOCKED' if n[FAIL] else 'NOT BLOCKED'}"
          + (f"（{n[FAIL]} 项 FAIL）" if n[FAIL] else "（无 FAIL，仍含 WARN 项待观察）"))

    if args.json:
        out = BASE_DIR / args.json
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "project": str(BASE_DIR),
            "summary": n,
            "production": "BLOCKED" if n[FAIL] else "NOT BLOCKED",
            "checks": [c.__dict__ for c in a.sorted_checks()],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[json] → {out}")

    return 2 if n[FAIL] else 0


if __name__ == "__main__":
    raise SystemExit(main())
