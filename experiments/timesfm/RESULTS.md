# TimesFM-3 零样本 PIT 回测 · 结论（2026-09-08）

## 判读：**不通过准入门，P5（接入主线）取消**

三条准出判据的实测结果（CTX=512，全量滚动 origin，standalone block bootstrap）：

| 判据 | 002112 | 002207 | 裁决 |
|---|---|---|---|
| ① pooled RankIC ≥ 0 | T+1 -0.018 CI[-0.066,+0.029]；**T+3 -0.084 CI[-0.147,-0.010] 显著负**；**T+5 -0.099 CI[-0.176,-0.015] 显著负** | T+1 +0.022；T+3 +0.044；T+5 +0.046（CI 均跨零，不显著） | ❌ 未过 |
| ② 与主模型误差去相关 | 未测——判据①已挂，不再投入 | 同左 | — |
| ③ 裸 q10–q90 coverage 80–90% 且宽于现 CQR 带宽可接受 | 74.3–77.2%（欠覆盖），带宽 3.4–7.6% | 79.2–80.4%，带宽 4.7–10.5% | ⚠️ 勉强，但无增量意义 |

方向命中率 47.9–51.3% ≈ 抛硬币。两只基金 RankIC 符号相反（002112 显著负 / 002207 弱正）——**外部零样本先验对 A 股主动型基金净值不具备稳定方向信息**，与此前预判一致：预训练语料以零售/交通/气象类"强结构"序列为主，金融 NAV 属外生冲击驱动型，是其公认弱项。

## 保留价值（已拿到，无需再接入）

1. **外部基线排除**：`google/timesfm-3.0` 零样本在 fund NAV 上无 alpha，README 的 Forecast 证据链可引用本报告作"外部强基线已试"记录。
2. **coverage 参考**：002207 上裸分位数覆盖 ~80%，接近名义 80%，说明 TimesFM 不确定性输出对"平滑衍生序列"整体欠自信、对高波动序列更差——反证你们 CQR 校准的必要性（002112 近期剧烈行情下欠覆盖 74%）。
3. **零训练成本约束证伪**：regime shift 时它同样失效（002112 显著负 IC 主要来自近端窗口），"免重训"不等于"抗 regime 漂移"。

## 复现

```powershell
# 依赖：D:\PythonProject\QuantV1-tfm（python 3.12 + timesfm 3.0.1 + torch 2.14 CPU）
# 权重：google/timesfm-3.0-pytorch（非商业许可），已缓存 D:\PythonProject\QuantV1-tfm\hf-cache
$env:HF_HUB_DISABLE_SYMLINKS_WARNING='1'; $env:HF_HUB_DISABLE_XET='1'; $env:HF_ENDPOINT='https://hf-mirror.com'
Set-Location D:\PythonProject\QuantV1
& D:\PythonProject\QuantV1-tfm\Scripts\python.exe -X utf8 experiments\timesfm\pit_forecast.py --funds '002112,002207'
```

全量 CPU 2×~130s；产物 `output/timesfm/pit_forecast_*.tsv`、`pit_eval_*.json`。
注意：`--funds` 参数在 PowerShell 下**必须加引号**，否则逗号被解析为数组且前导零被剥（002112→2112）。

## 纪律确认

- 未改 `core/`、未写 `data/`、未动 registry/config/cron；纯旁路实验。
- 权重许可 = timesfm-non-commercial-license-v1.0，本实验属研究用途；生产禁用。
