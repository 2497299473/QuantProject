r"""对照实验（非交付脚本，定位用）：生产 WF 原代码路径 × 任意冻结样本文件。

用法:
  .\\.venv-lab\\Scripts\\python.exe -X utf8 experiments\\forecast_lab\\_ctrl_prod_wf_on_frozen.py <jsonl 路径> [标签]

把 load_samples 换成读给定 jsonl（零网络）后调 backtest_walk_forward.main()。
生产脚本会把报告写进 output/backtest_walk_forward_<今日>.md —— 那是**生产证据路径**，
故本脚本跑完立刻把它改名成 output/CTRL_<标签>_<今日>.*，避免对照件冒充生产报告。
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "experiments" / "forecast_lab"))

import run_t3_panel_power as t3                            # noqa: E402
import backtest_spread                                     # noqa: E402
import backtest_walk_forward as wfw                        # noqa: E402


def _loader(path: Path):
    def f(*a, **k):
        raw = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
        return [json.loads(l) for l in raw.splitlines() if l.strip()]
    return f


def main() -> int:
    t3._guard_network()
    src = (BASE / sys.argv[1]) if len(sys.argv) > 1 else \
        BASE / "forecast_outputs" / "samples_frozen_20260910.jsonl"
    label = sys.argv[2] if len(sys.argv) > 2 else src.stem
    print(f"[ctrl] 样本 = {src.name} · sha256(LF) {hashlib.sha256(src.read_bytes()).hexdigest()[:12]}")

    ld = _loader(src)
    backtest_spread.load_samples = ld
    wfw.load_samples = ld                     # 生产脚本是 from-import，需同时替换
    sys.argv = ["backtest_walk_forward.py", "--window-days", "63", "--n-boot", "199"]

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = wfw.main()
    for line in buf.getvalue().splitlines():
        if re.search(r"pooled WF RankIC|判定|冻结切分|总样本", line):
            print(" ", line.strip())

    # ---- 证据隔离：对照件不得占生产报告路径
    stamp = time.strftime("%Y%m%d")
    moved = []
    for ext in ("md", "log"):
        p = BASE / "output" / f"backtest_walk_forward_{stamp}.{ext}"
        if p.exists():
            dst = BASE / "output" / f"CTRL_{label}_{stamp}.{ext}"
            p.rename(dst)
            moved.append(dst.name)
    print(f"[ctrl] exit={rc} · 报告已隔离为 {moved}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
