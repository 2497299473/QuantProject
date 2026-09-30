# -*- coding: utf-8 -*-
"""D3 批次（面 3 · 证据与血缘）自测收集器——沿用 audit_d04_d08_selftest 格式。

跑全部自测命令，stdout/stderr/returncode 逐条落盘 evidence/probes/。
零网络（铁律 7/8）：所有命令只读 data/ forecast_outputs/ 与 tempdir。
"""
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent   # evidence/probes/ -> 仓库根
PY = str(BASE / ".venv" / "Scripts" / "python.exe")
OUT = BASE / "evidence" / "probes"
LOG = OUT / "d3_batch_selftest_20260930.log"
META = OUT / "d3_batch_selftest_20260930.meta.json"

CMDS = [
    ("git rev-parse HEAD（本轮基准：D3-07/08 批 = 09b52fe 之后的工作区）",
     ["git", "rev-parse", "HEAD"]),
    ("git status --porcelain（本轮改动文件）",
     ["git", "status", "--porcelain"]),
    ("py_compile 全部涉改源码（含 D3-07 的 audit_project.py）",
     [PY, "-X", "utf8", "-m", "py_compile",
      "core/model_registry.py", "core/validation_schema.py",
      "bind_provenance.py", "bind_validation_evidence.py",
      "backtest_forecast.py", "evidence_recompute.py",
      "frozen_dataset.py", "freeze_verify_tool.py", "audit_project.py"]),
    ("D3-04/05：frozen_dataset + freeze_verify_tool 成对负向测例",
     [PY, "-X", "utf8", "-m", "unittest",
      "tests.test_frozen_dataset", "tests.test_freeze_verify_tool", "-v"]),
    ("D3-07：P1-2 archived 分档测例（11 条，含 active 打标红线与类型污染）",
     [PY, "-X", "utf8", "-m", "unittest", "tests.test_audit_hygiene", "-v"]),
    ("D3-02/03/06：provenance_binding（快照可信度 + first_identical_commit + 全长比对）",
     [PY, "-X", "utf8", "-m", "unittest", "tests.test_provenance_binding", "-v"]),
    ("D3-01 门 A：配对锚点 + 复算比对判据（纯函数）",
     [PY, "-X", "utf8", "-m", "unittest", "tests.test_evidence_anchor", "-v"]),
    ("D3-01 绑定层：伪造配对拒绑 + 零残留 + 旧路径死亡",
     [PY, "-X", "utf8", "-m", "unittest",
      "tests.test_evidence_binding_anchor", "-v"]),
    ("D3-01 五门 e2e 同步更新（promotion_rule_v2 + model_registry）",
     [PY, "-X", "utf8", "-m", "unittest",
      "tests.test_promotion_rule_v2", "tests.test_model_registry", "-v"]),
    ("fast 层全量",
     [PY, "-X", "utf8", "run_tests.py", "--layer", "fast"]),
    ("slow 层全量（含门 B 真实冻结件复算管线测例）",
     [PY, "-X", "utf8", "run_tests.py", "--layer", "slow"]),
    ("审计冒烟（--no-write 只读；D3-07 落地后预期：P1-2 转 WARN，"
     "audit_health=PASS_WITH_WARNINGS，exit=0）",
     [PY, "-X", "utf8", "audit_project.py", "--no-write"]),
    ("D3-01 门 B：真实 artifact（forecast_v3.pkl）+ 真实冻结件端到端复算探针",
     [PY, "-X", "utf8", "evidence/probes/d3_recompute_probe.py"]),
    ("D3-02/03/06：真实 registry 回填不变 + 伪造前缀拒绝探针",
     [PY, "-X", "utf8", "evidence/probes/d3_backfill_probe.py"]),
    ("D3-07：registry archived 逐字段 diff + 授权链零扰动实证探针",
     [PY, "-X", "utf8", "evidence/probes/d307_archive_probe.py"]),
    ("bind_provenance --dry-run（真实 registry 全条目仍可判定，D3-02 验收）",
     [PY, "-X", "utf8", "bind_provenance.py", "--dry-run"]),
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lines = []
    rcs = {}
    for title, cmd in CMDS:
        header = ("=" * 78 + "\n" + f"$ {' '.join(str(c) for c in cmd)}\n"
                  + f"  ({title})\n" + "=" * 78)
        t0 = time.time()
        r = subprocess.run(cmd, cwd=str(BASE), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=1800)
        dt = time.time() - t0
        body = (f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}\n"
                f"--- returncode: {r.returncode} --- ({dt:.1f}s)\n")
        lines.append(header + "\n" + body)
        rcs["$ " + " ".join(str(c) for c in cmd)[:70]] = r.returncode
        print(f"[{r.returncode}] {title[:60]} ({dt:.1f}s)")
    text = "\n".join(lines)
    LOG.write_text(text, encoding="utf-8")
    # D3-08（2026-09-30，A 单附带发现）：sha 必须对**落盘字节**算，不能对写盘前的
    # 内存字符串算。旧实现 hashlib.sha256(text.encode("utf-8")) 与 write_text 的
    # 落盘字节不一致——Windows 文本模式把 "\n" 翻成 CRLF，A 侧按文件字节复核会得到
    # 另一个 sha，「证据自证」当场失效（这正是本项目三件套反复踩的换行陷阱：
    # freeze_verify_tool 用 LF 归一口径、meta 记 LF sha，同一类坑）。
    # 修法：写完立刻 read_bytes() 重算，并在 meta 里注明口径，消除歧义。
    raw = LOG.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    meta = {
        "topic": "D3 批次修复自测（面 3 · 证据与血缘，A 单 D3-01~08）",
        "date": "2026-09-30",
        "base_commit": "25d3817a8256f9e7a1ae231228a40be3698035e9",
        "log_sha256_convention": (
            "sha256 over the **on-disk bytes** of the log file (read_bytes() after "
            "write_text()). Not the in-memory string: Windows text mode rewrites "
            "\\n as CRLF, so a string-based digest would not match the file bytes "
            "and external verification would fail (D3-08)."),
        "scope": {
            "D3-01": "report↔evidence 配对锚点（门 A）+ 独立复算门（门 B，含 CI）+ 旧路径死亡（derive 门 0）；附带 D3-01a：产出端 PROVENANCE_JSON 补 validation_mode 键",
            "D3-02": "attest 前快照自洽校验（snapshot_id 复算 + n_files；旧格式如实标注非内容寻址）；n_files 键存在性判定收紧",
            "D3-03": "first_identical_commit 三类测例入库 + HEAD 可达性过滤（merge-base --is-ancestor）",
            "D3-04": "malformed meta JSON 库路径 INVALID / CLI exit 4（成对测例）",
            "D3-05": "n_samples 类型收口（键缺失不判；null/str/bool → INVALID/MISMATCH）",
            "D3-06": "pkl 身份比对升全长 sha256；SHA16 仅保留 manifest 文件身份口径",
            "D3-07": "Summer 裁决选项 2：registry 两条目 validation 块打 archived 标记（含 reason/授权日期/授权人）+ audit P1-2 增 archived 分档（已处置→WARN、未处置仍 FAIL）+ 13 条测例；授权链零扰动已实证（prereg 降级仍 True / verify_validation_report 仍拒 / derive 仍 blocked）",
            "D3-08": "本收集器 log_sha256 改为对落盘字节计算 + meta 注明口径（消除换行陷阱造成的假 sha）",
        },
        "returncodes": rcs,
        "log_sha256": sha,
        "log_bytes": len(raw),
        "network": "零网络（全部命令只读 data/ forecast_outputs/ 与 tempdir；铁律 7/8）",
    }
    META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[ok] log -> {LOG}\n[ok] meta -> {META}\nlog_sha256={sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
