#!/usr/bin/env python3
"""Forecast 模型训练 + 持久化落盘（B 线闭环入口，2026-08-27）。

流程：
    load_samples()（PIT 口径）→ 冻结切分（OOS 段留证不入训，与 backtest_forecast 同口径）
    → ForecastEngine.fit(train) → save_models（含 oos_start 元数据）→ data/models/*.pkl

纪律（对齐项目铁律「有证据才上线」）：
- 本脚本只负责产出权重文件，不改 config.forecast.model_ready；
  能否对外输出真预测仍由 backtest_forecast.py 的 OOS 裁决 + 人工复核决定。
- 权重文件含版本/特征键/horizon 元数据，load_models 校验失败即拒绝（防旧权重混用）。

用法：python3 train_forecast_model.py [--min-samples N]
"""
import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from backtest_spread import load_samples
from frozen_dataset import resolve_samples   # V4.3 P0-1：统一冻结样本入口
from backtest_forecast import split_date_oos
from core import forecast_engine


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-samples", type=int, default=None,
                    help="最低训练样本数（默认读 config.forecast.min_valid_samples）")
    ap.add_argument("--snapshot", default=None,
                    help="冻结样本 jsonl（默认自动选最新 forecast_outputs/samples_frozen_*.jsonl）")
    ap.add_argument("--fresh", action="store_true",
                    help="显式活拉样本（数字与冻结基线不可比）")
    args = ap.parse_args()

    print("== [1] 加载样本（PIT 口径）==")
    # B 契约 §15-B3（2026-09-23）：与 backtest_forecast 同口径——入口保留全部
    # 特征行（require_fwds=()），各 horizon 标签由引擎按 fwd{h} 逐行过滤；
    # 短周期训练样本不再被 fwd20 连坐截断时间末端。
    samples, snap_info = resolve_samples(args.snapshot, args.fresh, BASE_DIR,
                                         lambda: load_samples(require_fwds=()))
    if snap_info["mode"] in ("MISSING", "INVALID"):
        return 4
    # V4.3.1 ④：训练日志首行区必须记录样本快照——模型基于哪份冻结件训练，
    # 与 registry snapshot_provenance（③）互为印证；重训对数时先看这一行。
    print(snap_info["report_line"])
    if not samples:
        print("[fail] 无样本")
        return 1

    # 冻结纪律（2026-08-28 修，GPT P0①）：训练**只用 train 段**，OOS 永不入训。
    train, oos, oos_start = split_date_oos(samples)
    print(f"== [1.5] 冻结切分：train < {oos_start}（{len(train)}）‖ OOS 保留 {len(oos)}（绝不入训）==")
    if any(s["date"] >= oos_start for s in train):
        print("[fail] 训练集混入 OOS 段样本，拒绝训练（冻结纪律）")
        return 1

    eng = forecast_engine.ForecastEngine()
    import json as _json
    fc_cfg = _json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8")).get("forecast", {})
    min_n = args.min_samples or int(fc_cfg.get("min_valid_samples", 200))
    if len(train) < min_n:
        print(f"[fail] 训练样本 {len(train)} < 门槛 {min_n}，拒绝训练")
        return 1

    print(f"== [2] 训练 {len(train)} 条样本（OOS {len(oos)} 段仅留证不参与）==")
    ok = eng.fit(train)
    if not ok:
        print("[fail] fit 未全部成功（某周期模型未训练），不落盘")
        return 1

    eng.trained_oos_start = oos_start
    # V4.3.1 ③：把训练实际消费的冻结件三元组（snapshot_file + samples_sha256_lf
    # + kfp 留档/重算/状态）透传进 registry——回答"这个模型基于哪份冻结样本
    # + 哪套 K 线指纹训练"。FRESH 模式下各键为 None（活拉没有留档，不伪造）。
    path = eng.save_models(snapshot_provenance=snap_info.get("snapshot_provenance"))
    if path is None:
        print("[fail] save_models 返回 None（fit 未全成，或目标 artifact 被 "
              "promotion prereg 钉住拒绝覆盖——见上方 [fail] 行）")
        return 1
    print(f"[ok] 权重已落盘：{path}")
    print(f"     trained_at = {eng.loaded_at} · horizons={eng.horizons}")
    print("提醒：model_ready 门禁不变——真预测输出仍需 backtest_forecast OOS 通过+人工确认。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
