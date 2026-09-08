# -*- coding: utf-8 -*-
"""只读探针：BK0457 缓存「有效但陈旧」时，v0.1 Data Quality 能否发现。
不写生产目录：夹具落 TEMP，compute(save=False) 且 snap_dir 指向 TEMP。"""
import json
import sys
import tempfile
from pathlib import Path

PROJ = Path(r"D:\PythonProject\QuantV1")
sys.path.insert(0, str(PROJ))

tmp = Path(tempfile.mkdtemp(prefix="qv1_stale_"))
bk_dir = tmp / "bk"
bk_dir.mkdir()

SRC = PROJ / "data" / "sector_klines" / "BK0457.json"
rec = json.loads(SRC.read_text(encoding="utf-8"))
print("[fixture] 原始 last=%s bars=%d" % (rec["last"], len(rec["klines"])))

# 造两个夹具：A=陈旧到 09-02（模拟连续 4 交易日拉取失败）；B=健康（对照）
for tag, cutoff in (("A_stale", "2026-09-02"), ("B_ok", "2099-01-01")):
    d = tmp / tag
    (d / "bk").mkdir(parents=True)
    r2 = json.loads(json.dumps(rec))
    kl = [k for k in r2["klines"] if k[0] <= cutoff]
    r2["klines"] = kl
    r2["bars"] = len(kl)
    r2["last"] = kl[-1][0]
    r2["fetched_at"] = kl[-1][0]
    (d / "bk" / "BK0457.json").write_text(
        json.dumps(r2, ensure_ascii=False), encoding="utf-8")
    print("[fixture %s] last=%s bars=%d  json合法=%s"
          % (tag, r2["last"], r2["bars"],
             bool(json.loads((d / "bk" / "BK0457.json").read_text(encoding="utf-8")))))

from core import market_context as mc  # noqa: E402

for tag, cutoff_expect in (("B_ok", "2026-09-07"), ("A_stale", "2026-09-02")):
    mc._BK_CACHE = tmp / tag / "bk"
    snap = mc.compute(slot="post", save=False, snap_dir=tmp / tag,
                      as_of_date="2026-09-08")
    q = snap.get("market_context_quality", {})
    th = (snap.get("themes") or {}).get("电网设备") or {}
    others = [d.get("as_of") for t, d in (snap.get("themes") or {}).items()
              if t != "电网设备"]
    print("\n===== %s (期望电网设备末根 %s) =====" % (tag, cutoff_expect))
    print("coverage          :", q.get("coverage"))
    print("errors            :", q.get("errors"), snap.get("errors"))
    print("themes_switched   :", q.get("themes_switched"))
    print("usable_for_oos    :", q.get("usable_for_oos"))
    print("oos_quality       :", q.get("oos_quality"))
    print("reason            :", q.get("usable_for_oos_reason"))
    print("电网设备 as_of     :", th.get("as_of"), "bars=", th.get("bars"),
          "r5d=", th.get("r5d"))
    print("其余 12 主题末根   :", sorted(set(others)))
    print("spread_5d         :", snap.get("spread_5d"))
    print("regime            :", snap.get("regime"))
    print("quality 里任何滞后/新鲜度字段:",
          [k for k in q if any(s in k.lower() for s in
                               ("stale", "lag", "age", "fresh", "date", "as_of"))])
