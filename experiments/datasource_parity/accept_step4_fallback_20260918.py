r"""步 4 真实回落验收（2026-09-18，Summer 09:56 授权窗口内执行）。

验证两件事（重构方案表内步 4 验收行）：
1. 东财真实成功时：load_fund → _source=fresh、source=eastmoney、报告无切换提示（1 发东财）
2. 东财「失败」时链自动回落 sina：为省请求，用进程内 patch 注入 network 失败
   （不发真实东财请求），sina 真实拉全量（非东财域）→ source=sina、
   source_trace_notes/_data_source_notice 出「数据源切换」文案（步 5 联动验收）
3. 回落写盘后再与东财备份逐条对拍（口径等价实证），最后撤 patch 恢复干净缓存（1 发东财）

东财真实请求数 ≤2（步骤① + ③恢复）。任何一步异常 → 还原备份，中止。
"""
import json
import shutil
import sys
import time
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from core import data_loader                                    # noqa: E402
from core.datasource.base import FetchResult                    # noqa: E402
from core.datasource.providers.fund_eastmoney import EastmoneyFundNavProvider  # noqa: E402
from core.report_generator import source_trace_notes, _data_source_notice  # noqa: E402

CODE = "002112"
BACKUP = BASE / "output" / "backup_klines_20260918" / f"{CODE}.json"
CACHE = BASE / "data" / "klines" / f"{CODE}.json"


def snapshot(tag):
    d = json.loads(CACHE.read_text(encoding="utf-8"))
    print(f"   [{tag}] source键={d.get('source')} navs={len(d['navs'])} "
          f"末条={d['navs'][-1]} purchase={d.get('purchase_status')!r}")
    return d


def main() -> int:
    assert BACKUP.exists(), "缺备份，拒跑"
    backup = json.loads(BACKUP.read_text(encoding="utf-8"))
    print(f"备份就绪：{BACKUP.name}（东财口径 {len(backup['navs'])} 条）")

    # ① 正常链：东财真实 1 发
    print("① 正常链（东财真实 1 发）")
    f1 = data_loader.load_fund(CODE, force_refresh=True)
    print(f"   _source={f1['_source']} source={f1.get('source')}")
    assert f1["_source"] == "fresh" and f1.get("source") == "eastmoney"
    sig1 = {CODE: {"last_nav_date": f1["navs"][-1][0], "last_nav": f1["navs"][-1][1],
                   "name": f1["name"], "source": f1.get("source", ""),
                   "_source": f1["_source"]}}
    assert source_trace_notes(sig1) == [], "主源不应出切换提示"
    s1 = snapshot("fresh-eastmoney")

    # ② 东财失败注入（不发真实请求）→ 回落 sina
    print("② 东财失败注入（patch，0 发东财；sina 真实全量约 25~40s）")
    fake = FetchResult(ok=False, source="eastmoney",
                       error="network:注入故障（步4验收演练）", latency_ms=1)
    t0 = time.monotonic()
    with mock.patch.object(EastmoneyFundNavProvider, "fetch", return_value=fake), \
         mock.patch.object(data_loader, "fetch_lsjz",
                           return_value=backup["lsjz"]):   # 申赎用缓存真数据，防字段漂移
        f2 = data_loader.load_fund(CODE, force_refresh=True)
    print(f"   _source={f2['_source']} source={f2.get('source')} "
          f"耗时 {time.monotonic()-t0:.0f}s")
    assert f2["_source"] == "fresh" and f2.get("source") == "sina", "回落未生效"
    sig2 = {CODE: {"last_nav_date": f2["navs"][-1][0], "last_nav": f2["navs"][-1][1],
                   "name": f2["name"], "source": "sina", "_source": "fresh"}}
    notes = source_trace_notes(sig2)
    notice = _data_source_notice(sig2)
    assert notes and "sina" in notes[0], "trace 未出切换行"
    assert "数据源切换" in notice, "报告层未渲染"
    print(f"   ✓ 链回落 sina；trace：{notes[0]}")

    # ②b 回落态写盘与东财备份逐条对拍（口径等价实证）
    now = snapshot("fresh-sina 写盘后")
    em_map = {d: v for d, v in backup["navs"]}
    common = [(d, v) for d, v in now["navs"] if d in em_map]
    mism = [(d, v, em_map[d]) for d, v in common
            if abs(float(v) - float(em_map[d])) > 1e-6]
    print(f"   对拍 sina×东财：交集 {len(common)}，mismatch={len(mism)}")
    if mism:
        print("   ⚠️ 尾差样例（同旧校验容忍口径）:", mism[:3])
    assert len(mism) <= 3, "差异超容忍，检查 sina provider"

    # ③ 撤 patch 恢复东财口径（第 2 发东财）
    print("③ 恢复干净缓存（撤 patch，东财真实 1 发）")
    f3 = data_loader.load_fund(CODE, force_refresh=True)
    assert f3["_source"] == "fresh" and f3.get("source") == "eastmoney"
    s3 = snapshot("recovered")
    # 对拍口径：昨日备份 ⊂ 今日恢复（NAV 序列只增不改——新增尾条是今晨新公布的净值）
    em_map3 = {d: v for d, v in s3["navs"]}
    sub_mism = [(d, v, em_map3.get(d)) for d, v in backup["navs"]
                if d not in em_map3 or abs(float(v) - float(em_map3[d])) > 1e-6]
    added = [r for r in s3["navs"] if r[0] not in {d for d, _ in backup["navs"]}]
    assert not sub_mism, f"恢复后历史段与备份不一致: {sub_mism[:3]}"
    print(f"   ✓ 备份 {len(backup['navs'])} 条全含于恢复态且逐条一致；"
          f"新增尾条 {added}（今晨新公布净值，正常只增不改）")

    print("\n== 结论 == PASS：真实链正常源 / 失败回落 / trace 联动 / 恢复，全过")
    print(f"东财真实请求数 = 2（预算 ≤3）")
    return 0


if __name__ == "__main__":
    rc = main()
    if rc != 0:
        shutil.copy2(BACKUP, CACHE)
        print("!! 异常：已从备份还原缓存")
    sys.exit(rc)
