"""V4 步 3 真实冒烟：real_time 旧实现 vs 新装配点（腾讯 qt.gtimg.cn，非东财）。

背景：步 3 迁移 `core/real_time.py` → providers/realtime_tencent.py + 装配点。
断网留痕验收已由离线测例钉死（tests/test_realtime_datasource.py，9 测）；
本脚本补**真实拉数逐项一致**一层——qt.gtimg.cn 是腾讯域，不受铁律 7（东财频控）
约束，但同样保持克制：**总共只发 2 个请求（旧 1 + 新 1），单发不重试**，
任一请求失败即停手报告，不做第三次。

标的：600519（沪）/ 000651（深）/ 000858（深）——与步 2 对拍同一组，非科创板。
"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import netutil
from core import real_time as new_mod

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_MKT = {"0": "sz", "1": "sh"}


def old_fetch_realtime(holdings: list[dict]) -> dict:
    """迁移前 core/real_time.py::fetch_realtime 函数体原样（f0668e5 之前版本）。"""
    out: dict[str, dict] = {}
    if not holdings:
        return out
    syms = ",".join(f"{_MKT[h['market']]}{h['code']}" for h in holdings)
    url = f"https://qt.gtimg.cn/q={syms}"
    try:
        text = netutil.http_get_bytes(url, headers={"User-Agent": UA},
                                      timeout=10).decode("gbk", errors="replace")
    except Exception:
        return out
    for line in text.split(";"):
        if "~" not in line:
            continue
        parts = line.strip().split("~")
        if len(parts) < 33 or len(parts[2]) != 6:
            continue
        code = parts[2]
        try:
            price = float(parts[3]) if parts[3] else 0.0
            chg = float(parts[32]) if parts[32] else 0.0
            if price <= 0:
                continue
            out[code] = {"name": parts[1], "price": price,
                         "change_pct": chg, "time": parts[30]}
        except (ValueError, IndexError):
            continue
    for h in holdings:
        q = out.get(h["code"])
        if q:
            q["pct"] = h["pct"]
            q["market"] = h["market"]
    return out


HOLDINGS = [{"market": "1", "code": "600519", "name": "贵州茅台", "pct": 9.0},
            {"market": "0", "code": "000651", "name": "格力电器", "pct": 8.0},
            {"market": "0", "code": "000858", "name": "五粮液", "pct": 7.5}]

FIELDS = ("name", "price", "change_pct", "time", "pct", "market")


def main() -> int:
    old = old_fetch_realtime(HOLDINGS)
    if not old or "_error" in old:
        print("FAIL: 旧实现真实拉数失败（无法建立基线），停手不重试")
        return 1
    new = new_mod.fetch_realtime(HOLDINGS)
    if "_error" in new:
        print(f"FAIL: 新装配点真实拉数失败：{new['_error']}，停手不重试")
        return 1

    bad = 0
    if set(old) != set(new):
        print(f"FAIL: 代码键集合不一致 old={sorted(old)} new={sorted(new)}")
        bad += 1
    for code in sorted(set(old) & set(new)):
        for f in FIELDS:
            if old[code].get(f) != new[code].get(f):
                print(f"FAIL: {code}.{f} old={old[code].get(f)!r} new={new[code].get(f)!r}")
                bad += 1
        else:
            print(f"  [{code}] ✓ {len(FIELDS)} 字段逐项一致 "
                  f"(price={new[code]['price']} chg={new[code]['change_pct']})")

    est_old = new_mod.weighted_estimate(HOLDINGS, old)
    est_new = new_mod.weighted_estimate(HOLDINGS, new)
    if est_old != est_new:
        print(f"FAIL: weighted_estimate 不一致 {est_old} vs {est_new}")
        bad += 1
    else:
        print(f"  [est] ✓ weighted_estimate 一致 (est={est_new['est_change_pct']:.4f}, "
              f"covered={est_new['covered_pct']})")

    if bad:
        print(f"\n== 结论 == FAIL：{bad} 处不一致")
        return 1
    print(f"\n== 结论 == PASS：{len(old)} 只 × {len(FIELDS)} 字段 + 加权估计全部一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
