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
    "test_lookthrough_pit.py": FAST,       # 2026-09-23：报告侧取生效期（PIT 一行修复），全 mock 零网络
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
    "test_action_bootstrap.py": FAST,      # B 契约 §15-B1：action CI 统一聚类 bootstrap，合成数据零网络
    "test_b3_sample_retention.py": FAST,   # B 契约 §15-B3：样本构建/target 可用性分离，纯函数+源码断言零网络
    "test_feature_protocol_strict.py": FAST,  # B 契约 §15-B2：协议 exact 校验，独立条目进出真实 registry 后复原（同 test_model_registry 惯例）
    "test_validation_schema.py": FAST,     # B++-1：validation 证据 schema v2 纯函数 + fail-closed 校验器；仅 numpy，零网络，不碰真实 data/（契约文本只读，缺席自动 skip）
    "test_forecast_evidence.py": FAST,     # B++-2：验证器产出 schema v2 的纯函数层（evidence_node/pooled_node/assemble），合成数据零网络，不跑 main
    "test_promotion_rule_v2.py": FAST,     # B++-3：derive_promotion rule v2 五门判据纯函数 + 契约 B 反例矩阵；registry 触点 tempdir 隔离
    "test_evidence_lineage.py": FAST,      # R2-1/R2-4/R1-4 修复验收：三向血缘 + evidence_sha256 带外入档 + 占位-OK 授权层拦截；tempdir 隔离零网络
    "test_metric_status.py": FAST,         # B++-5：指标不足/不可算与真实 0 分离（status 核心 + 遗留外壳哨兵兼容），纯函数零网络
    "test_diag_entry_unify.py": FAST,      # B++-6：诊断入口统一（四脚本 require_fwds=() + policy 复用 cluster_bootstrap_ci），源码断言 + 合成数据零网络
    "test_stock_data_cache.py": FAST,      # P1-B：stock 缓存原子写 + 坏件回退 provider 链，tempdir + stub 零网络
    # ---- slow：重依赖（sklearn/scipy）或真实数据 / 输出 / 网络耦合 ----
    "test_batch_a_hardening.py": SLOW,   # A 批加固（2026-09-24）：血统解耦/完整性门/覆写闸门；触真实 registry（备份/恢复）+ backtest_forecast 导入链，稳妥归 slow
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
