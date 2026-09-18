r"""P1 · 新浪基金净值冗余拉取（旁路实验，不进生产链路）。

背景（2026-09-08）：东财 push2his 于 09-05 起 IP 级频控，主源随时可能断。
本脚本用新浪 openapi 拉同一份场外基金单位净值，落盘 output/sina_nav/，
并与 data/klines/ 东财缓存做逐日交叉校验——只读 data/，绝不写 data/（铁律4）。

数据通路实测（2026-09-08）：
  CaihuiFundInfoService.getNav  num=100&page=N 分页，返回按日期降序；
  002112 total_num=2637 与东财缓存条数完全一致，样本日数值逐位吻合。

运行（需能 import 项目 core，故用项目 venv）：
  .\.venv\Scripts\python.exe -X utf8 experiments\sina_nav_redundant\pull_sina_nav.py
  追加 --force 可重拉当日已存在的缓存。
"""
import argparse
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]  # 项目根
sys.path.insert(0, str(BASE))               # 与 forecast_lab 脚本同约定，使 core 可导入

from core import netutil  # noqa: E402

OUT_DIR = BASE / "output" / "sina_nav"
API = ("https://stock.finance.sina.com.cn/fundInfo/api/openapi.php/"
       "CaihuiFundInfoService.getNav")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
PAGE_NUM = 100
DATEFROM = "20150101"
SLEEP = 0.8          # 页间隔；新浪无东财那种 IP 频控，保守起见仍限速
MAX_PAGES = 60       # 60×100=6000 条，覆盖最长的 002112（2637）绰绰有余
REFERER = "https://finance.sina.com.cn/"


def _http_get_json(url: str) -> dict:
    """传输走 netutil（2026-09-18 改造）。

    旧实现用裸 urllib.request.urlopen，会读 Windows 系统代理：开着 VPN 时
    本脚本会从境外出口取数（实测数值与境内一致，但受 VPN 可用性牵连、且慢）。
    netutil 自建 socket 逐 IP 直连，无视环境/系统代理，与生产 provider 同通道
    （providers/__init__ 亦约定不得直接 import urllib）。

    netutil 的 retries 语义为「首次之外的重试次数」，取 2 → 共 3 次尝试，
    与旧实现的 3 次重试循环等量。
    """
    return netutil.http_get_json(url, headers={"User-Agent": UA, "Referer": REFERER},
                                 timeout=15, retries=2)


def fetch_sina_nav(code: str) -> list[list]:
    """返回升序 [[date, nav], ...]（单位净值）。"""
    rows, page = [], 1
    while page <= MAX_PAGES:
        url = (f"{API}?symbol={code}&datefrom={DATEFROM}"
               f"&dateto={time.strftime('%Y%m%d')}&num={PAGE_NUM}&page={page}&format=json")
        js = _http_get_json(url)
        data = (((js.get("result") or {}).get("data") or {}).get("data") or [])
        if not data:
            break
        for it in data:
            d = str(it.get("fbrq", ""))[:10]
            try:
                nav = float(it.get("jjjz"))
            except (TypeError, ValueError):
                continue
            if len(d) == 10:
                rows.append([d, nav])
        if len(data) < PAGE_NUM:
            break
        page += 1
        time.sleep(SLEEP)
    rows.sort(key=lambda r: r[0])
    # 去重（分页边界偶发重叠）
    dedup = {}
    for d, v in rows:
        dedup[d] = v
    return [[d, dedup[d]] for d in sorted(dedup)]


def load_em_cache(code: str) -> list[list] | None:
    p = BASE / "data" / "klines" / f"{code}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8")).get("navs")


def cross_check(sina: list[list], em: list[list] | None) -> dict:
    s = {d: v for d, v in sina}
    out = {"sina_n": len(s), "em_n": None if em is None else len(em)}
    if em is None:
        out["note"] = "no EM cache to compare"
        return out
    e = {d: v for d, v in em}
    common = sorted(set(s) & set(e))
    out["common_dates"] = len(common)
    out["sina_only"] = sorted(set(s) - set(e))[:10]
    out["em_only"] = sorted(set(e) - set(s))[:10]
    diffs = [(d, s[d], e[d]) for d in common if abs(s[d] - e[d]) > 1e-6]
    out["mismatch_n"] = len(diffs)
    out["mismatch_sample"] = diffs[:10]
    out["max_abs_diff"] = max((abs(a - b) for _, a, b in diffs), default=0.0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略当日已有缓存重拉")
    args = ap.parse_args()

    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    funds = cfg["fund_pool"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = time.strftime("%Y%m%d")
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "funds": {}}
    ok_all = True

    for code in funds:
        cache = OUT_DIR / f"{code}.json"
        if cache.exists() and not args.force:
            age_day = time.strftime("%Y%m%d", time.localtime(cache.stat().st_mtime))
            if age_day == today:
                print(f"[skip] {code} 当日已拉取（幂等）")
                js = json.loads(cache.read_text(encoding="utf-8"))
                report["funds"][code] = js.get("check", {"note": "restored from cache"})
                continue
        try:
            navs = fetch_sina_nav(code)
            if len(navs) < 50:
                raise RuntimeError(f"suspiciously few rows: {len(navs)}")
            check = cross_check(navs, load_em_cache(code))
            rec = {"code": code, "source": "sina", "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "navs": navs, "check": check}
            cache.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            report["funds"][code] = check
            status = "OK" if check.get("mismatch_n", 0) == 0 else "WARN"
            print(f"[{status}] {code} sina={check['sina_n']} em={check['em_n']} "
                  f"common={check['common_dates']} mismatch={check['mismatch_n']} "
                  f"maxdiff={check['max_abs_diff']:.4f}")
            if status == "WARN":
                ok_all = False
        except Exception as e:  # noqa: BLE001
            ok_all = False
            report["funds"][code] = {"error": str(e)}
            print(f"[FAIL] {code}: {e}", file=sys.stderr)
        time.sleep(SLEEP)

    rpt_path = BASE / "output" / f"sina_nav_check_{today}.md"
    lines = ["# 新浪净值冗余校验报告", "", f"生成：{report['generated_at']}", ""]
    for code, chk in report["funds"].items():
        lines.append(f"## {code}")
        lines.append("```json")
        lines.append(json.dumps(chk, ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")
    rpt_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[ok] 报告 -> {rpt_path}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
