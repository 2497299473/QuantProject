"""daily_runs 工具（2026-09-18，V4 观察链修复）——23:00 汇总等定时任务的确定性助手。

杜绝 09-17 复盘定性的两个根因（当时全靠自然语言约束，agent 执行走样）：

1. **时区**：`scheduler_runs.started_at` 存 UTC ISO（如 `2026-09-17T08:00:24+00:00`）。
   按「北京日期字符串」匹配当日 → 16:00 实例永远查不到 → 假警报「未执行」。
   本工具统一 `fromisoformat().astimezone(+8)` 后按 `.date()` 过滤。
2. **追加**：汇总 agent「读整文件→重写」抹掉了当日 [16:00]/[21:30] 小节、
   [23:00] 还写了两份。`append` 子命令只做 `open("a")`，且同名小节已存在即
   拒写（幂等），从代码层面堵死整文件重写路径。

用法：
  python -X utf8 daily_runs_tool.py runs --date 2026-09-18
  python -X utf8 daily_runs_tool.py append --date 2026-09-18 \
      --section "[23:00 日汇总]" --file %TEMP%\\summary.md [--force]

铁律兼容：本工具零网络；runs 以 mode=ro 读调度库，绝不写。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ8 = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path.home() / ".opensquilla" / "state" / "scheduler.db"
RUNS_DIR = BASE_DIR / "output" / "daily_runs"

# job_id 前缀 → 任务名（与 deploy/daily_summary_task.md 的映射保持一致）
JOB_LABELS = {
    "93439da7": "09:30 晨检",
    "fa6d9cdd": "16:00 板块K线",
    "69b20a70": "22:30 Shadow",
    "ed7ae8c8": "23:00 日汇总",
}


def _to_local(iso: str) -> datetime:
    """UTC/带偏移 ISO → +08:00 aware datetime。无偏移的按 UTC 处理。"""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ8)


def runs_for_date(date_str: str) -> list[tuple[str, datetime, datetime | None, str, str]]:
    """返回该北京日期内每个已知 job 的最新一次 run：(job, started, finished, success, error)。

    当天无记录的 job 以 started=None 占位，保证任务行齐全、agent 无自由裁量空间。
    """
    day = datetime.strptime(date_str, "%Y-%m-%d").date()
    if not DB_PATH.exists():
        raise FileNotFoundError(f"调度库不存在：{DB_PATH}")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    try:
        rows = conn.execute(
            "SELECT job_id, started_at, finished_at, success, error "
            "FROM scheduler_runs").fetchall()
    finally:
        conn.close()
    latest: dict[str, tuple] = {}
    for job_id, started, finished, success, error in rows:
        if not started:
            continue
        st = _to_local(started)
        if st.date() != day:
            continue
        prev = latest.get(job_id[:8])
        if prev is None or st > prev[1]:
            latest[job_id[:8]] = (job_id[:8], st,
                                  _to_local(finished) if finished else None,
                                  str(success), (error or "").strip()[:80])
    out = []
    for prefix, label in JOB_LABELS.items():
        hit = latest.get(prefix)
        out.append((label, hit[1], hit[2], hit[3], hit[4]) if hit
                   else (label, None, None, "-", "当日无记录"))
    return out


def cmd_runs(args: argparse.Namespace) -> int:
    for label, st, fin, ok, err in runs_for_date(args.date):
        if st is None:
            print(f"[runs] {label}: {err}")
        else:
            end = f"{fin:%H:%M:%S}" if fin else "未记录结束"
            print(f"[runs] {label}: {st:%H:%M:%S} → {end} success={ok}"
                  + (f" error={err}" if err else ""))
    return 0


def cmd_append(args: argparse.Namespace) -> int:
    path = RUNS_DIR / f"{args.date}.md"
    src = Path(args.file)
    body = src.read_text(encoding="utf-8").rstrip()
    if not body:
        print(f"[append] ERROR: 内容文件为空：{src}", file=sys.stderr)
        return 2
    header = f"## {args.section}"
    path.parent.mkdir(parents=True, exist_ok=True)   # 文档承诺「目录不存在则创建」
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old is not None and header + "\n" in old + "\n" and not args.force:
        print(f"[append] SKIP：{path.name} 已存在小节「{args.section}」，"
              "拒绝重复追加（确要再加用 --force）")
        return 0
    with path.open("a", encoding="utf-8") as f:
        if old is None:
            f.write(f"# {args.date}\n\n")
        elif old and not old.endswith("\n"):
            f.write("\n")
        f.write(f"{header}\n\n{body}\n\n")
    print(f"[append] OK → {path}（仅追加，未触碰既有内容）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("runs", help="按北京日期(+8)过滤调度库 run 记录，逐 job 一行")
    p1.add_argument("--date", required=True, help="YYYY-MM-DD（北京时间日）")
    p1.set_defaults(fn=cmd_runs)
    p2 = sub.add_parser("append", help="幂等追加小节（只 a 不重写）")
    p2.add_argument("--date", required=True)
    p2.add_argument("--section", required=True,
                    help='小节标题（不含 ##），如 "[23:00 日汇总]"')
    p2.add_argument("--file", required=True, help="小节正文来源文件（不含标题行）")
    p2.add_argument("--force", action="store_true", help="允许同名小节再写一次")
    p2.set_defaults(fn=cmd_append)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
