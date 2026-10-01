"""面 6 D6-C 注入复核探针（C-6 隔离验证，2026-10-01）。

目的：隔离 freeze_verify_tool G-A 的 **sha 门**（rec == sha_lf）的唯一贡献。

A 单称「删 sha 比对 → verify 返 PASS → 红」，但实跑注入 ga_ok=True 后
test_freeze_verify_tool 全绿——因为 test_tampered_samples_abort 用的是
「追加一行」篡改，行数门（declared_n != n_rows）冗余兜住了它。

本探针改用「行数不变、内容变」（改 est_chg 值，仍 2 行）的篡改：
- 行数门：2==2 不动 ga_ok（抓不到）
- degraded flags / stock_data_failures：不涉及
- 只有 sha 门能抓

用法（在仓库根，用 .venv python 跑）：
    python evidence/probes/face6_c6_sha_gate_isolation_probe.py
预期：真实代码 → MISMATCH(3)；注入 ga_ok=True → PASS(0)（sha 门删则该攻击逃逸）。
"""
import hashlib
import json
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

import freeze_verify_tool as tool   # noqa: E402


def _write_jsonl(path, rows):
    body = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)
    path.write_bytes(body.replace("\n", "\r\n").encode("utf-8"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def main():
    tmp = Path(tempfile.mkdtemp())
    tool.git_commit = lambda: "deadbeef" * 5
    tool.current_kline_fingerprint = lambda _rec=None: {"aggregate_sha256": "f5a2f607" + "0" * 56}
    jsonl = tmp / "samples_frozen_20260910.jsonl"
    meta = tmp / "samples_frozen_20260910.meta.json"
    kfp = tmp / "kline_fingerprint_20260910.json"

    rows = [{"fund": "002112", "date": "2020-04-27", "est_chg": 0.1},
            {"fund": "002207", "date": "2020-04-28", "est_chg": -0.2}]
    sha = _write_jsonl(jsonl, rows)
    meta.write_text(json.dumps({
        "kind": "forecast_lab_samples_freeze", "sha256": sha, "n_samples": len(rows),
        "jsonl": jsonl.name, "degraded_or_ratelimit_flags": []}), encoding="utf-8")
    kfp.write_text(json.dumps({
        "kind": "forecast_lab_kline_fingerprint",
        "aggregate_sha256": "f5a2f607" + "0" * 56}), encoding="utf-8")

    # 行数不变、内容变的篡改：只改一个 est_chg 值（行数仍 2）
    tampered = [{"fund": "002112", "date": "2020-04-27", "est_chg": 9.99},
                {"fund": "002207", "date": "2020-04-28", "est_chg": -0.2}]
    _write_jsonl(jsonl, tampered)

    rc = tool.main(["verify", "--jsonl", str(jsonl)])
    print(f"\n[PROBE] 行数不变篡改 → exit={rc} "
          f"({'MISMATCH/抓到' if rc == tool.EXIT_MISMATCH else 'PASS/逃逸'})")
    return rc


if __name__ == "__main__":
    raise SystemExit(0 if main() == tool.EXIT_MISMATCH else 1)
