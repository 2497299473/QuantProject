"""P1-① 样本级冻结（2026-09-10；Summer 09-09 11:18 拍板把 P1 由 09-11 提前至 09-10）。

用**已合入三态加固的生产路径** `backtest_spread.load_samples()` 跑一遍，逐行写
forecast_outputs/samples_frozen_YYYYMMDD.jsonl + sha256 指纹 + meta 侧车。

本脚本是纯只读包装器：不复制、不修改 load_samples 的任何口径，不改
config.json / model_ready / 生产 .py，不写 data/。

频控纪律（东财 IP 级）：
  holdings_history 无缓存（forecast_outputs/f10_raw 只是降级页留档，不是缓存），
  故本脚本必然产生命中请求：4 基金 × 7 年 = 28 次 fundf10 首发，最坏情况按生产
  既有策略每年代 2 次重试（退避 2s/5s）= 上限 84 次。该重试策略**原样沿用，不放宽、
  不加强、不自行加第三轮**。
  若捕获到 DegradedResponse / 缺年告警等降级或频控征兆 → 以退出码 1 结束并报告，
  由 Summer 决定是否补拉，禁止在本脚本外私自重试。

用法:
  python experiments/forecast_lab/freeze_samples.py
"""
from __future__ import annotations

import hashlib
import json
import sys
import warnings
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]          # QuantV1 根
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "experiments" / "forecast_lab"))

from kline_fingerprint import build_fingerprint   # noqa: E402  数据层指纹（2026-09-10 拍板）

# 降级/频控征兆关键词（来自 core/lookthrough 三态判定的告警文案与异常类名）
RATE_MARKERS = ("DegradedResponse", "SUSPECT_DEGRADED", "PARSE_MISMATCH",
                "持仓拉取失败", "年持仓拉取失败")


def main() -> int:
    outdir = BASE_DIR / "forecast_outputs"
    outdir.mkdir(exist_ok=True)

    # 生产路径，原样调用（含其 print 进度输出，便于后台日志观察是否卡频控）
    from backtest_spread import load_samples

    started = datetime.now()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        samples = load_samples()
    warn_msgs = [str(x.message) for x in caught]
    flags = [m for m in warn_msgs if any(k in m for k in RATE_MARKERS)]

    # ---- 落盘 JSONL（canonical：sort_keys 保证 sha256 可复现）----
    date_tag = started.strftime("%Y%m%d")
    out_jsonl = outdir / f"samples_frozen_{date_tag}.jsonl"
    lines = [json.dumps(s, ensure_ascii=False, sort_keys=True) for s in samples]
    body = "".join(ln + "\n" for ln in lines)
    out_jsonl.write_text(body, encoding="utf-8")
    file_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()

    # ---- 分布与区间 ----
    dist: dict[str, int] = {}
    for s in samples:
        dist[s["fund"]] = dist.get(s["fund"], 0) + 1
    dates = sorted(s["date"] for s in samples) if samples else []

    # ---- 数据层指纹（前复权漂移防护，2026-09-10 Summer 拍板）----
    # 样本行 sha256 只锁行内容；缓存 TTL 到期重取会追溯改写历史 close，
    # 样本没变、语义变了。冻结时必须连 (date, close) 序列指纹一起落盘，
    # 否则两次「同 sha256 冻结」并不等价（P1 附带发现，报告 §六）。
    kfp = build_fingerprint()
    out_kfp = outdir / f"kline_fingerprint_{date_tag}.json"
    out_kfp.write_text(json.dumps(kfp, ensure_ascii=False, indent=1, sort_keys=True),
                       encoding="utf-8")

    meta = {
        "kind": "forecast_lab_samples_freeze",
        "schema_version": "1",
        "producer": "experiments/forecast_lab/freeze_samples.py",
        "code_path": "backtest_spread.load_samples()  # 生产路径，三态加固已合入(0d3cc69)",
        "created_at": started.isoformat(timespec="seconds"),
        "elapsed_sec": round((datetime.now() - started).total_seconds(), 1),
        "n_samples": len(samples),
        "n_fields": len(samples[0]) if samples else 0,
        "fund_distribution": dict(sorted(dist.items())),
        "date_min": dates[0] if dates else None,
        "date_max": dates[-1] if dates else None,
        "jsonl": out_jsonl.name,
        "sha256": file_sha,
        "n_warnings": len(warn_msgs),
        "degraded_or_ratelimit_flags": flags,
        "kline_fingerprint_sha256": kfp["aggregate_sha256"],
        "kline_fingerprint_file": out_kfp.name,
        "kline_fingerprint_counts": {"stock": kfp["n_stock"], "fund": kfp["n_fund"],
                                     "unreadable": kfp["unreadable"]},
    }
    out_meta = outdir / f"samples_frozen_{date_tag}.meta.json"
    out_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n== P1-① 样本冻结结果 ==")
    print(f"  样本数     : {len(samples)}  （预期 ~3343）")
    print(f"  每行字段数 : {meta['n_fields']}")
    print(f"  日期区间   : {meta['date_min']} ~ {meta['date_max']}")
    print(f"  四基金分布 : {meta['fund_distribution']}")
    print(f"  sha256     : {file_sha}")
    print(f"  K线指纹    : {kfp['aggregate_sha256']}（stock={kfp['n_stock']} "
          f"fund={kfp['n_fund']} unreadable={len(kfp['unreadable'])}）-> {out_kfp.name}")
    print(f"  文件       : {out_jsonl}")
    print(f"  meta       : {out_meta}")
    print(f"  告警数     : {len(warn_msgs)}   用时 {meta['elapsed_sec']}s")
    if flags:
        print("  ⚠️ 捕获降级/频控征兆 —— 立即停手，勿重试：")
        for m in flags:
            print(f"    ! {m[:300]}")
        return 1
    for m in warn_msgs:
        print(f"  note: {m[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
