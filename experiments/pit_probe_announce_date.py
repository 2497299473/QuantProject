"""PIT 公告日可行性探测（2026-09-16，为 P0-1 修 PIT 做数据源核验）。

背景：审计 P0-1 FAIL —— core/lookthrough.py 用固定滞后 _QTR_LAG 近似公告日。
要换成真实公告日，先得确认东财 F10 响应里**到底有没有**公告日字段。
本脚本只做一件事：发 1 个请求，把原始响应留档并扫描日期类字段。

⚠️ 铁律 7：本脚本属"发东财请求的作业"，**禁止定时器自动开跑**。
必须由人显式授权后手工执行，且先过 --check-gates（四项闸门只读检查）。

用法：
    python experiments/pit_probe_announce_date.py --check-gates   # 只读，零网络
    python experiments/pit_probe_announce_date.py --run           # 授权后才用这个
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import netutil  # noqa: E402

# 与 core/lookthrough.py:75 同一 URL 口径（查档核实，不臆猜接口）
F10_URL = ("https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
           "?type=jjcc&code={code}&topline=10&year={year}")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PROBE_FUND = "002112"        # 池内基金（config.json fund_pool，已核实存在）
PROBE_YEAR = 2026
OUT_DIR = BASE_DIR / "forecast_outputs" / "pit_probe"
KAMT = "http://push2.eastmoney.com/api/qt/kamt/get"   # 参照组，见 channel_diag

# 时间盒：11:20 起零东财新请求（铁律 7 第 4 项）
GATE_HARD_STOP = datetime.time(11, 20)


# ------------------------------------------------------------ 只读闸门检查

# 同类拉取进程的命令行特征（会打到东财的一律算）：主干 run.py、lookthrough、
# 受控刷新、各探测脚本。OpenSquilla 运行时常驻 5~7 个 python.exe，与本判据无关。
FETCH_MARKERS = ("run.py", "lookthrough", "refresh", "pit_probe", "eastmoney",
                 "netutil", "data_loader", "daily_pull")


def list_fetch_processes() -> list[str] | None:
    """枚举命令行含拉取特征的 python 进程（排除自身）。枚举失败返回 None。"""
    import os
    import subprocess
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe' or "
          "Name='pythonw.exe'\" | ForEach-Object { \\"
          "\"$($_.ProcessId)`t$($_.CommandLine)\\\" }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=25,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    me = os.getpid()
    peers: list[str] = []
    for ln in (r.stdout or "").splitlines():
        pid_s, _, cmd = ln.partition("\t")
        try:
            pid = int(pid_s.strip())
        except ValueError:
            continue
        if pid == me or not cmd:
            continue
        low = cmd.lower()
        if any(m in low for m in FETCH_MARKERS):
            peers.append(f"{pid}:{cmd.strip()[:60]}")
    return peers


def check_gates() -> list[tuple[str, bool, str]]:
    """四项征兆闸门（铁律 7）。全 PASS 才允许 --run。本函数零网络。"""
    out: list[tuple[str, bool, str]] = []
    today = datetime.date.today().isoformat()

    # 1. 上游已跑完：看**当日受控批量刷新**记录 output/ops_runs/{today}-dlite-refresh.md。
    #    2026-09-17 10:32 复核：原判据查 output/daily_runs/{today}.md 指错了目录——该目录
    #    是日终作业产物（历史文件 mtime 全在 22:30~01:14），上午发单永远 FAIL＝坏传感器。
    #    语义仍从严：记录不存在 / 过小 / 非当日写入 / 记了停手，任一即 FAIL。
    dr = BASE_DIR / "output" / "ops_runs" / f"{today}-dlite-refresh.md"
    if dr.is_file() and dr.stat().st_size > 200:
        mday = datetime.date.fromtimestamp(dr.stat().st_mtime).isoformat()
        try:
            body = dr.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            body, mday = "", "unreadable"
            print(f"  [warn] 读上游记录失败：{type(e).__name__}: {e}")
        stopped = ("停手：是" in body) or ("停手: 是" in body)
        ok1 = (mday == today) and not stopped
        detail1 = (f"{dr.name} size={dr.stat().st_size} mtime={mday}"
                   + ("（停手记录）" if stopped else "")
                   + ("" if mday == today else "（非当日写入）"))
    elif dr.is_file():
        ok1 = False
        detail1 = f"{dr.name} 过小({dr.stat().st_size}B)，疑未写完"
    else:
        ok1 = False
        pre_first = datetime.datetime.now().hour < 9   # 晨检是当日第一个上游
        detail1 = f"{dr.name} 不存在" + ("（当日首个上游未到点）" if pre_first
                                       else "（上游未到点或已失败）")
    out.append(("上游已跑完", ok1, detail1))

    # 2. 无频控征兆：这里只做「当日请求计数未超」的静态检查；
    #    kamt 探针需要网络，留给 --run 前置执行（单请求，属授权范围内）
    out.append(("无频控征兆", True, "需 --run 时以 kamt 现场确认（本检查不发包）"))

    # 3. 无并行拉取：查有无其他 python 进程正在跑同类拉取脚本
    # 3. 无并行拉取：只看**同类拉取**进程。按进程名计数会误报——本机常驻
    #    5~7 个 python.exe 全是 OpenSquilla 运行时（2026-09-16 20:48 实测），
    #    必须看命令行关键词，并排除自身 PID。
    peers = list_fetch_processes()
    if peers is None:
        out.append(("无并行拉取", False, "无法枚举进程命令行（判据不可用时从严 FAIL）"))
    else:
        out.append(("无并行拉取", len(peers) == 0,
                    f"同类拉取进程 {len(peers)} 个"
                    + (f"：{peers}" if peers else "")))

    # 4. 在时间盒内
    now = datetime.datetime.now()
    ok4 = now.time() < GATE_HARD_STOP
    out.append(("在 11:20 时间盒内", ok4, f"now={now.strftime('%H:%M')}"))
    return out


def gates_all_pass(checks: list[tuple[str, bool, str]]) -> bool:
    return all(ok for _, ok, _ in checks)


def print_checks(checks: list[tuple[str, bool, str]]) -> None:
    print("== 铁律 7 四项闸门（只读，零网络）==")
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<18} {detail}")


# ------------------------------------------------------------ 探测主体

def find_date_fields(text: str) -> dict:
    """扫描响应里所有日期形态，按标签归类，判断是否存在「公告日」语义字段。"""
    hits: dict[str, list[str]] = {}
    for label in ("公告", "披露", "发布日期", "更新时间", "截止至", "截止"):
        vals = re.findall(label + r"[^0-9]{0,20}(\d{4}-\d{2}-\d{2})", text)
        if vals:
            hits[label] = sorted(set(vals))[:6]
    # 通用：所有裸日期 + 其前 30 字符上下文（供人工判读语义）
    ctx = [{"before": text[max(0, m.start() - 30):m.start()].strip()[-30:],
            "date": m.group(0)}
           for m in re.finditer(r"\d{4}-\d{2}-\d{2}", text)]
    return {"labelled": hits, "n_raw_dates": len(ctx), "context_sample": ctx[:25]}


def run_probe() -> int:
    checks = check_gates()
    print_checks(checks)
    if not gates_all_pass(checks):
        print("\n[abort] 闸门未全 PASS —— 按铁律 7 拒绝发请求。")
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 前置 kamt 探针（授权范围内的第 1、2 个请求：参照组 + 探测目标）
    try:
        kamt = netutil.http_get(KAMT, timeout=10, retries=1)
        print(f"\n[kamt] 长度={len(kamt)} 前 60={kamt[:60]!r}")
    except Exception as e:            # noqa: BLE001 频控征兆即停手不重试
        print(f"\n[abort] kamt 探针异常（疑似频控/封禁征兆）：{type(e).__name__}: {e}")
        return 2

    url = F10_URL.format(code=PROBE_FUND, year=PROBE_YEAR)
    print(f"[get] {url}")
    try:
        text = netutil.http_get(url, headers={"User-Agent": UA,
                                              "Referer": "https://fundf10.eastmoney.com/"},
                                timeout=15, retries=1)
    except Exception as e:            # noqa: BLE001
        print(f"[fail] {type(e).__name__}: {e}")
        return 1

    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    raw = OUT_DIR / f"probe_{PROBE_FUND}_{stamp}.txt"
    raw.write_text(text, encoding="utf-8", errors="replace")

    verdict = find_date_fields(text)
    # 核心问题：除「截止至」（报告期）外，是否有独立的公告日语义字段？
    has_announce = bool(re.search(r"公告|披露|发布日期", text)) and any(
        k in verdict["labelled"] for k in ("公告", "披露", "发布日期"))
    result = {
        "probed_at": stamp,
        "fund": PROBE_FUND, "year": PROBE_YEAR,
        "resp_len": len(text),
        "raw_path": str(raw),
        "n_periods_seen": text.count("<div class='boxitem"),
        "has_announce_date_field": has_announce,
        **verdict,
    }
    (OUT_DIR / f"probe_{PROBE_FUND}_{stamp}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n== 探测结论 ==")
    print(f"  响应长度 {len(text)}，期数 {result['n_periods_seen']}")
    print(f"  带标签日期 {json.dumps(verdict['labelled'], ensure_ascii=False)}")
    print(f"  含公告日语义字段：{'YES' if has_announce else 'NO'}")
    print(f"  留档 {raw.relative_to(BASE_DIR)}")
    if not has_announce:
        print("\n[结论] F10 jjcc 不提供公告日 → P0-1 需换数据源（Tushare 需积分 / 或采保守下界设计）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-gates", action="store_true", help="只读闸门检查（零网络，默认）")
    ap.add_argument("--run", action="store_true", help="人工授权后执行探测")
    args = ap.parse_args()
    if args.run:
        return run_probe()
    print_checks(check_gates())
    print("\n（未发任何请求。要探测请显式加 --run）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
