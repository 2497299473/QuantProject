import json
from pathlib import Path

base = Path(__file__).resolve().parent

print("=== [1] data/ 顶层内容 ===")
for p in sorted(base.glob("data/*")):
    if p.is_file():
        print("  file:", p.name)
    elif p.is_dir():
        print("  dir :", p.name + "/")

print("\n=== [2] 净值缓存目录 data/klines ===")
kd = base / "data" / "klines"
if kd.exists():
    for p in sorted(kd.glob("*.json"))[:8]:
        print("  ", p.name)
    alln = sorted(kd.glob("*.json"))
    if alln:
        p = alln[-1]
        d = json.loads(p.read_text(encoding="utf-8"))
        navs = d.get("navs")
        if navs:
            print(f"  样本 {p.name}: n={len(navs)} last={navs[-1][0]}")
else:
    print("  无 data/klines 目录")

print("\n=== [3] 从 load_samples 拿真实样本日期范围 ===")
import sys
sys.path.insert(0, str(base))
from backtest_spread import load_samples
s = load_samples()
dts = sorted({r["date"] for r in s})
print(f"  总样本行 {len(s)}  min {dts[0]}  max {dts[-1]}")
print(f"  最近 8 个样本日: {dts[-8:]}")

print("\n=== [4] holdings.as_of_date ===")
print("  ", json.loads((base / "holdings.json").read_text(encoding="utf-8")).get("as_of_date"))