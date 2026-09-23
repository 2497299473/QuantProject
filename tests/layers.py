"""测试分层清单（V4-A，2026-09-17）——**唯一事实来源**。

分层依据（实测，非印象）：

- 用缺 ``sklearn``/``scipy`` 的解释器跑全量时，恰好这 3 个文件报错：
  ``test_lofo_evidence`` / ``test_forecast`` / ``test_pit`` ⇒ 重依赖项；
- 另有若干文件读写真实 ``data/`` ``output/`` 或走网络层 ⇒ 状态耦合项。

``fast`` = 纯函数 / 契约 / 解析器，无重依赖、不碰真实数据与输出。
``slow`` = 模型 / 回测 / 真实数据 / 网络耦合。

用途：
- ``python run_tests.py --layer fast``（日常开发，unittest 原生，无需 pytest）
- ``tests/conftest.py`` 据此自动打 pytest marker（装了 pytest 时可用
  ``pytest -m fast``）
"""
from __future__ import annotations

FAST = "fast"
SLOW = "slow"
INTEGRATION = "integration"
RESEARCH = "research"

# 文件名（含 .py）→ 层
LAYERS: dict[str, str] = {
    # ---- fast：纯函数 / 契约 / 解析（无重依赖，不碰真实 data/ 与 output/）----
    "test_a158lite.py": FAST,
    "test_audit_states.py": FAST,
    "test_chanlun.py": FAST,
    "test_datasource_chain.py": FAST,   # fake provider 纯内存，零网络不碰 data/
    "test_datasource_providers.py": FAST,  # stub 掉 netutil，零网络不碰 data/
    "test_realtime_datasource.py": FAST,   # 步 3：stub netutil，零网络不碰 data/
    "test_fund_datasource.py": FAST,       # 步 4：stub netutil，零网络不碰 data/
    "test_report_source_trace.py": FAST,   # 步 5：纯内存展示层，零网络
    "test_daily_runs_tool.py": FAST,       # 观察链：临时库/临时目录，零网络
    "test_freeze_verify_tool.py": FAST,    # 冻结三件套校验：临时目录，零网络不碰 data/
    "test_forecast_contract.py": FAST,     # V4.4 重定：Forecast Contract C1-C5 纪律，合成行零网络
    "test_frozen_dataset.py": FAST,        # V4.3 P0-1：统一样本入口契约，tempdir 零网络
    "test_delta_intraday.py": FAST,
    "test_decision_edge.py": FAST,    # P0-1 两轨：decision_edge 审计指标纯函数，合成数据零网络
    "test_drift_monitor.py": FAST,
    "test_dynamics.py": FAST,
    "test_holidays.py": FAST,
    "test_intraday.py": FAST,
    "test_layers.py": FAST,
    "test_model_registry.py": FAST,
    "test_publish_gate.py": FAST,       # 全程 tempdir 造证据，零网络不碰真实 output/
    "test_pit_guard.py": FAST,
    "test_pit1455_contract.py": FAST,   # V4.4 步 1：契约纯函数 + 注入 NAV，零网络不碰 data/
    "test_pit1455_matrix.py": FAST,     # V4.4 步 3：矩阵指标/基线方向纯函数，合成行不碰 data/
    "test_run_manifest.py": FAST,      # 全程 tempdir，不碰真实 data/ 与 output/
    "test_manifest_fund_mask.py": FAST,  # V4.1 ④：别名掩码/反查纯逻辑，tempdir 零网络
    "test_v42_contracts.py": FAST,      # V4.2：审计新鲜度/状态门禁/流水幂等，tempdir 零网络
    "test_quantile_calib.py": FAST,
    "test_state_engine.py": FAST,
    "test_state_lookup.py": FAST,
    "test_t5_scorecard.py": FAST,
    "test_walk_forward.py": FAST,
    # ---- slow：重依赖（sklearn/scipy）或真实数据 / 输出 / 网络耦合 ----
    "test_contracts.py": SLOW,
    "test_data_fingerprint.py": SLOW,
    "test_dataset_snapshot_scope.py": FAST,   # V4.1 ①：环检测纯逻辑；现场只读 manifest
    "test_nav_fallback_degraded.py": FAST,    # V4.1 ③：tempdir + 内存结构，零网络
    "test_forecast.py": SLOW,
    "test_forecast_lifecycle.py": SLOW,
    "test_forecast_split.py": SLOW,
    "test_holdings_tristate.py": SLOW,
    "test_holdings_visibility.py": SLOW,
    "test_lofo_evidence.py": SLOW,
    "test_market_context.py": SLOW,
    "test_netutil.py": SLOW,
    "test_path_and_store.py": SLOW,
    "test_pit.py": SLOW,
    "test_provenance_binding.py": SLOW,
    "test_shadow_policy.py": SLOW,
}

MARKER_DESCRIPTIONS = {
    FAST: "纯函数 / 契约 / 解析器；无重依赖，不碰真实数据与输出",
    SLOW: "模型 / 回测 / 真实数据 / 网络耦合",
    INTEGRATION: "跨模块端到端（当前由 slow 覆盖，预留）",
    RESEARCH: "研究协议 / 预注册判据（当前由 slow 覆盖，预留）",
}


def layer_of(filename: str) -> str:
    """文件名 → 层。未登记时按 ``slow`` 处理（未分类默认不进 fast 快车道）。"""
    return LAYERS.get(filename, SLOW)


def modules_for(layer: str) -> list[str]:
    """层 → ``tests.<module>`` 列表（供 unittest 加载）。"""
    return [f"tests.{n[:-3]}" for n, lyr in sorted(LAYERS.items()) if lyr == layer]
