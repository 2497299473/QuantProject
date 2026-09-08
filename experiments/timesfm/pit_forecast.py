r"""P3 · TimesFM-3 零样本 point-in-time 滚动回测（旁路实验，不接主线）。

纪律：
- 严格 PIT：origin i 只喂 navs[:i+1]（截断至最后 CTX 个点），预测 T+1..T+5；
  真实值只用于离线评价，绝不回流进上下文（防前视）。
- 只读 data/klines/，只写 output/timesfm/（铁律4）。
- 运行环境：专用 venv（勿用项目 venv，torch 不进主环境）
    $env:HF_HUB_DISABLE_SYMLINKS_WARNING='1'
    $env:HF_HUB_DISABLE_XET='1'; $env:HF_ENDPOINT='https://hf-mirror.com'
    D:\PythonProject\QuantV1-tfm\Scripts\python.exe -X utf8 `
      experiments\timesfm\pit_forecast.py
- 权重 google/timesfm-3.0-pytorch 为非商业许可（timesfm-non-commercial-license-v1.0），
  仅限研究/PoC，不得进生产。

输出：
  output/timesfm/pit_forecast_{code}.tsv   每行 = (origin_date, horizon, q10,q50,q90, realized)
  output/timesfm/pit_eval_{date}.json      汇总指标（方向命中/IC/覆盖率/带宽，cluster bootstrap CI）
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parents[2]
OUT = BASE / "output" / "timesfm"
CTX = 512
HORIZON = 5
HORIZON = 5
Q_IDX = {0.10: 0, 0.50: 4, 0.90: 8}


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    def rank(x):
        order = np.argsort(x, kind="stable")
        r = np.empty(len(x), dtype=float)
        r[order] = np.arange(len(x))
        return r
    ra, rb = rank(a), rank(b)
    if ra.std() == 0 or rb.std() == 0:
        return float("nan")
    return float(np.corrcoef(ra, rb)[0, 1])


def cluster_bootstrap_ci(vals: np.ndarray, block: int = 20, n_boot: int = 1000,
                         seed: int = 7) -> tuple[float, float]:
    vals = vals[np.isfinite(vals)]
    if len(vals) < block * 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(len(vals) / block))
    starts_max = len(vals) - block
    means = np.empty(n_boot)
    for i in range(n_boot):
        starts = rng.integers(0, starts_max + 1, size=n_blocks)
        sample = np.concatenate([vals[s:s + block] for s in starts])
        means[i] = sample[: len(vals)].mean()
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--funds", default="002112,002207")
    ap.add_argument("--device", default=None, help="cpu/cuda；默认自动")
    ap.add_argument("--limit", type=int, default=0, help="只跑最近 N 个 origin（冒烟测试用，0=全量）")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    import torch
    import timesfm

    dev = args.device or ("cuda" if torch.cuda.is_available() else "cpu")
    t0 = time.time()
    model = timesfm.TimesFM3Forecaster.from_pretrained(
        "google/timesfm-3.0-pytorch",
        device=dev,
        cache_dir=r"D:/PythonProject/QuantV1-tfm/hf-cache")
    print(f"[load] {time.time()-t0:.1f}s device={dev}")

    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "ctx": CTX, "horizon": HORIZON, "model": "timesfm-3.0-pytorch",
              "funds": {}}

    for code in args.funds.split(","):
        p = BASE / "data" / "klines" / f"{code}.json"
        if not p.exists():
            print(f"[skip] no cache {code}")
            continue
        js = json.loads(p.read_text(encoding="utf-8"))
        dates = [d for d, _ in js["navs"]]
        navs = np.array([v for _, v in js["navs"]], dtype=np.float64)
        n = len(navs)
        n_orig = n - HORIZON - CTX + 1  # origin i 取 [CTX-1 .. n-HORIZON-1]
        if n_orig <= 0:
            print(f"[skip] {code} history too short ({n})")
            continue
        print(f"[run ] {code} origins={n_orig} ({dates[CTX-1]} .. {dates[n-HORIZON-1]})")
        origins = list(range(CTX - 1, n - HORIZON))
        if args.limit and args.limit < n_orig:
            origins = origins[-args.limit:]
            n_orig = len(origins)
            print(f"[smoke] 截断至最近 {n_orig} 个 origin")

        # 批量构造上下文（统一长度 CTX，分批推理）
        t0 = time.time()
        B = 256
        all_q = np.empty((n_orig, HORIZON, 9), dtype=np.float32)
        ctxs = [navs[i - CTX + 1: i + 1].astype(np.float32) for i in origins]
        for b0 in range(0, n_orig, B):
            batch = [c.astype(np.float32) for c in ctxs[b0:b0 + B]]
            for j, fc in enumerate(model.predict_batch(batch, horizon=HORIZON,
                                                       return_quantiles=True)):
                all_q[b0 + j] = np.asarray(fc.quantiles, dtype=np.float32)
            print(f"  batch {b0+len(batch)}/{n_orig} {time.time()-t0:.0f}s", flush=True)

        # 组装 TSV + 离线真实值
        ev = {h: {"pred_ret": [], "real_ret": [], "cov90": [], "band_w": []}
              for h in (1, 3, 5)}
        tsv_path = OUT / f"pit_forecast_{code}.tsv"
        with tsv_path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write("origin_date\thorizon\tq10\tq50\tq90\trealized_nav\tlast_ctx_nav\n")
            for k, i in enumerate(origins):
                last = navs[i]
                # horizon 索引：预测步 h 对应 all_q[k, h-1]
                for h in (1, 3, 5):
                    a10 = float(all_q[k, h - 1, Q_IDX[0.10]])
                    a50 = float(all_q[k, h - 1, Q_IDX[0.50]])
                    a90 = float(all_q[k, h - 1, Q_IDX[0.90]])
                    real = float(navs[i + h])
                    fh.write(f"{dates[i]}\t{h}\t{a10:.6f}\t{a50:.6f}\t{a90:.6f}\t{real:.6f}\t{last:.6f}\n")
                    pr = a50 / last - 1.0
                    rr = real / last - 1.0
                    ev[h]["pred_ret"].append(pr)
                    ev[h]["real_ret"].append(rr)
                    ev[h]["cov90"].append(1.0 if a10 <= real <= a90 else 0.0)
                    ev[h]["band_w"].append((a90 - a10) / last)
        print(f"[ok  ] {code} -> {tsv_path.name}  ({time.time()-t0:.0f}s)")

        # 指标
        fm = {}
        for h, d in ev.items():
            pr = np.array(d["pred_ret"]); rr = np.array(d["real_ret"])
            ic = spearman(pr, rr)
            ic_lo, ic_hi = cluster_bootstrap_ic(pr, rr)
            hit = np.sign(pr) == np.sign(rr)
            cov = np.array(d["cov90"]); bw = np.array(d["band_w"])
            cov_lo, cov_hi = cluster_bootstrap_ci(cov)
            fm[h] = {
                "n": int(len(rr)),
                "rank_ic": round(ic, 4),
                "rank_ic_ci95": [round(ic_lo, 4), round(ic_hi, 4)],
                "dir_hit": round(float(hit.mean()), 4),
                "coverage_q10_q90": round(float(cov.mean()), 4),
                "coverage_ci95": [round(cov_lo, 4), round(cov_hi, 4)],
                "mean_band_width": round(float(bw.mean()), 5),
                "ic_note": "standalone block bootstrap；正式判读以 backtest_forecast 口径复算为准",
            }
        report["funds"][code] = fm
        print(f"[stat] {code}: " + json.dumps(fm, ensure_ascii=False))

    out_json = OUT / f"pit_eval_{time.strftime('%Y%m%d')}.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] eval -> {out_json}")
    return 0


def cluster_bootstrap_ic(pr, rr, block=20, n_boot=500, seed=11):
    """block bootstrap of RankIC（standalone 版，正式判读时以 backtest_forecast 口径为准）。"""
    n = len(pr)
    rng = np.random.default_rng(seed)
    ics = []
    for _ in range(n_boot):
        idx = []
        while len(idx) < n:
            s = rng.integers(0, max(1, n - block))
            idx.extend(range(s, s + block))
        idx = np.array(idx[:n])
        ics.append(spearman(pr[idx], rr[idx]))
    ics = np.array([x for x in ics if np.isfinite(x)])
    if len(ics) == 0:
        return (float("nan"), float("nan"))
    return (float(np.percentile(ics, 2.5)), float(np.percentile(ics, 97.5)))


if __name__ == "__main__":
    sys.exit(main())
