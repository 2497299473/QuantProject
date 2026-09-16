"""K 线/净值缓存指纹（2026-09-10，Summer 拍板「决策 2」落地）。

背景（P1 附带发现，报告 output/forecast_lab_p1_20260910.md §六）：
  东财个股 K 线是**前复权**价——缓存 TTL 到期重取时，除权事件会追溯改写整条历史
  序列（09-10 一次冻结就重取了 171/240 只），历史 est_chg 随之漂移，v7 的 WF
  基线从 +0.089（09-08）跳到 -0.0324（09-10，生产函数本体复算）。
  也就是说：**样本行 sha256 相同不代表数据语义相同**，冻结必须连数据指纹一起锁。

本模块回答一个问题：这份冻结样本，是基于哪一套价格序列算出来的？

口径：
- 逐标的提取 klines/navs 的 (date, close) 全序列做 canonical 序列化（sort_keys、
  固定精度），先算单序列 sha256，再把全部 (code, seq_sha) 聚合为总指纹。
- 任何一根历史 K 线被复权改写 → 该序列 sha 变 → 总指纹变；文件 mtime 不参与
  哈希（只作记录），重取但内容不变不算漂移。
- 只读缓存文件，零网络。缺失目录显式报错，不静默出空指纹。

用法：
  python experiments/forecast_lab/kline_fingerprint.py                # 自检
  python experiments/forecast_lab/kline_fingerprint.py --write forecast_outputs/kfp_20260910.json
  # 追加板块指数扫描（面板成员全集，见下）：
  python experiments/forecast_lab/kline_fingerprint.py --write ... --with-sector
  # 作为库（freeze_samples.py 接入）：
  from kline_fingerprint import build_fingerprint

板块扫描（2026-09-16 追加，预注册 §五之三「不足则扩到面板成员全集」）：
  实测原遍历集 = data/stock_klines + data/klines 两目录，**不含 data/sector_klines**，
  故候选 D 面板的板块主代理 BK0457 不在指纹内。扩法为 **opt-in**：
  `include_sector=True` 才追加扫描 sector_klines 全目录（含 fallback 码），
  **默认 False → 09-10 起 freeze_samples / run_m0_power 的既有口径与 aggregate
  逐字节不变**（只扩清单，不改哈希算法与序列化格式）。开启时新增键
  `n_sector` / `sector_sha`、`schema_version` 记为 "2"，aggregate 与旧基线不可直接对比。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]          # QuantV1 根
STOCK_DIR = BASE_DIR / "data" / "stock_klines"
FUND_DIR = BASE_DIR / "data" / "klines"
SECTOR_DIR = BASE_DIR / "data" / "sector_klines"      # 2026-09-16 §五之三 扩清单


def _seq_sha(pairs: list[tuple[str, float]]) -> str:
    """单标的 (date, close) 全序列的 canonical sha256。"""
    body = "\n".join(f"{d},{float(c):.10g}" for d, c in pairs)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _json_series(path: Path, key: str) -> list[tuple[str, float]] | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rows = doc.get(key) or []
    out = []
    for r in rows:
        try:
            out.append((str(r[0]), float(r[2])))     # klines: [date, open, close, ...]
        except (IndexError, TypeError, ValueError):
            return None
    return out


def _nav_series(path: Path) -> list[tuple[str, float]] | None:
    """基金净值：[["YYYY-MM-DD", nav, ...], ...]，取 date+nav。"""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rows = doc.get("navs") or []
    out = []
    for r in rows:
        try:
            out.append((str(r[0]), float(r[1])))
        except (IndexError, TypeError, ValueError):
            return None
    return out


def build_fingerprint(fund_codes: list[str] | None = None,
                      include_sector: bool = False) -> dict:
    """扫描两目录生成指纹。fund_codes=None 时收全部 data/klines/*.json。

    include_sector=False（默认）＝ 09-10 起的原口径，输出键与 aggregate 不变。
    """
    per_stock: dict[str, str] = {}
    bad: list[str] = []
    if STOCK_DIR.is_dir():
        for p in sorted(STOCK_DIR.glob("*.json")):
            s = _json_series(p, "klines")
            if s is None or not s:
                bad.append(p.name)
            else:
                per_stock[p.stem] = _seq_sha(s)
    funds = sorted(fund_codes) if fund_codes else \
        [p.stem for p in sorted(FUND_DIR.glob("*.json"))] if FUND_DIR.is_dir() else []
    per_fund: dict[str, str] = {}
    for c in funds:
        s = _nav_series(FUND_DIR / f"{c}.json")
        if s is None or not s:
            bad.append(f"{c}.json(fund)")
        else:
            per_fund[c] = _seq_sha(s)
    per_sector: dict[str, str] = {}
    if include_sector:
        if not SECTOR_DIR.is_dir():
            raise RuntimeError(
                f"include_sector=True 但 {SECTOR_DIR} 不存在——拒绝生成缺板块的半份指纹")
        for p in sorted(SECTOR_DIR.glob("*.json")):
            s = _json_series(p, "klines")
            if s is None or not s:
                bad.append(p.name)
            else:
                per_sector[p.stem] = _seq_sha(s)
    if not per_stock and not per_fund:
        raise RuntimeError("stock_klines 与 klines 均无可用缓存——拒绝生成空指纹")
    canonical = "\n".join(f"{k},{v}" for k, v in sorted(per_stock.items())) + \
        "\n#fund\n" + "\n".join(f"{k},{v}" for k, v in sorted(per_fund.items()))
    if include_sector:
        canonical += "\n#sector\n" + \
            "\n".join(f"{k},{v}" for k, v in sorted(per_sector.items()))
    fp = {
        "kind": "forecast_lab_kline_fingerprint",
        "schema_version": "2" if include_sector else "1",
        "n_stock": len(per_stock),
        "n_fund": len(per_fund),
        "unreadable": sorted(bad),
        "stock_sha": per_stock,
        "fund_sha": per_fund,
        "aggregate_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }
    if include_sector:
        fp["n_sector"] = len(per_sector)
        fp["sector_sha"] = per_sector
    return fp


# ---------------- 离线自检（零网络，构造临时缓存目录） ----------------
def selftest() -> int:
    import tempfile
    fails = []

    def check(cond, name):
        nonlocal total
        total += 1
        if not cond:
            fails.append(name)

    total = 0

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        stk, fun = root / "stock_klines", root / "klines"
        sec = root / "sector_klines"
        stk.mkdir(); fun.mkdir(); sec.mkdir()
        kline = lambda rows: {"klines": rows}
        nav = lambda rows: {"navs": rows}
        (stk / "000001.json").write_text(json.dumps(kline(
            [["2020-01-01", 1, 10.0], ["2020-01-02", 1, 11.0]])), encoding="utf-8")
        (fun / "002112.json").write_text(json.dumps(nav(
            [["2020-01-01", 1.5], ["2020-01-02", 1.6]])), encoding="utf-8")

        import kline_fingerprint as kf
        kf.STOCK_DIR, kf.FUND_DIR, kf.SECTOR_DIR = stk, fun, sec
        fp1 = kf.build_fingerprint(["002112"])
        check(fp1["n_stock"] == 1 and fp1["n_fund"] == 1, "计数")
        check(len(fp1["aggregate_sha256"]) == 64, "聚合sha形态")
        fp2 = kf.build_fingerprint(["002112"])
        check(fp1["aggregate_sha256"] == fp2["aggregate_sha256"], "确定性")

        # 复权改写历史（open 列变化不算，close 变则变）
        (stk / "000001.json").write_text(json.dumps(kline(
            [["2020-01-01", 9, 10.0], ["2020-01-02", 1, 11.0]])), encoding="utf-8")
        fp3 = kf.build_fingerprint(["002112"])
        check(fp3["aggregate_sha256"] == fp1["aggregate_sha256"], "非收盘列扰动不敏感")
        (stk / "000001.json").write_text(json.dumps(kline(
            [["2020-01-01", 1, 9.0], ["2020-01-02", 1, 11.0]])), encoding="utf-8")
        fp4 = kf.build_fingerprint(["002112"])
        check(fp4["aggregate_sha256"] != fp1["aggregate_sha256"], "历史close改写敏感")

        # 尾部追加新行同样敏感
        (stk / "000001.json").write_text(json.dumps(kline(
            [["2020-01-01", 1, 10.0], ["2020-01-02", 1, 11.0], ["2020-01-03", 1, 12.0]]
        )), encoding="utf-8")
        fp5 = kf.build_fingerprint(["002112"])
        check(fp5["aggregate_sha256"] != fp1["aggregate_sha256"], "追加行敏感")

        # 坏文件进 unreadable，不污染聚合
        (stk / "bad.json").write_text("{not json", encoding="utf-8")
        fp6 = kf.build_fingerprint(["002112"])
        check("bad.json" in fp6["unreadable"], "坏文件显式列名")
        check(fp6["n_stock"] == 1, "坏文件不计入")

        # ---- 2026-09-16 §五之三：板块扫描为 opt-in，默认口径逐字节不变 ----
        # 锚点用 fp6（放入板块文件**之前**、且含全部历史改动与 bad.json 的最后一次默认指纹）；
        # 不可用 fp1——它取出于股票文件被后续测试改写之前，会假失败。
        (sec / "BK0457.json").write_text(json.dumps(kline(
            [["2020-01-01", 1, 3000.0], ["2020-01-02", 1, 3100.0]])), encoding="utf-8")
        fp_no = kf.build_fingerprint(["002112"])
        check(fp_no["aggregate_sha256"] == fp6["aggregate_sha256"], "默认口径不随板块文件变")
        check("n_sector" not in fp_no and "sector_sha" not in fp_no, "默认不新增输出键")
        fp_sec = kf.build_fingerprint(["002112"], include_sector=True)
        check(fp_sec["n_sector"] == 1 and "BK0457" in fp_sec["sector_sha"], "板块纳入计数")
        check(fp_sec["aggregate_sha256"] != fp6["aggregate_sha256"], "板块并入改变聚合")
        check(kf.build_fingerprint(["002112"], include_sector=True)["aggregate_sha256"]
              == fp_sec["aggregate_sha256"], "板块模式确定性")
        (sec / "BK0457.json").write_text(json.dumps(kline(
            [["2020-01-01", 1, 2999.0], ["2020-01-02", 1, 3100.0]])), encoding="utf-8")
        check(kf.build_fingerprint(["002112"], include_sector=True)["aggregate_sha256"]
              != fp_sec["aggregate_sha256"], "板块历史close改写敏感")
    print(f"[kline_fingerprint SELFTEST] {total - len(fails)} passed, {len(fails)} failed"
          + (f" -> {fails}" if fails else ""))
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", default=None, help="指纹 JSON 输出路径")
    ap.add_argument("--funds", default=None, help="逗号分隔基金码（默认全部 klines/*.json）")
    ap.add_argument("--with-sector", action="store_true",
                    help="追加扫描 data/sector_klines/（§五之三 面板成员扩清单；默认关）")
    args = ap.parse_args()
    if not args.write:
        return selftest()
    fp = build_fingerprint(args.funds.split(",") if args.funds else None,
                           include_sector=args.with_sector)
    out = Path(args.write)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fp, ensure_ascii=False, indent=1, sort_keys=True),
                   encoding="utf-8")
    print(f"fingerprint -> {out}")
    print(f"  aggregate sha256 : {fp['aggregate_sha256']}")
    print(f"  stock={fp['n_stock']} fund={fp['n_fund']} unreadable={fp['unreadable'] or '无'}"
          + (f" sector={fp['n_sector']}" if args.with_sector else ""))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
