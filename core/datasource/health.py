"""进程内源健康度：连续失败计数 → 降级到链尾。

口径（重构方案 §五「明确不做」第 4 条）：
- **不落盘**——只记进程内状态，避免新增状态文件污染 `data/`；
- 与 `market_context.py` 的 proxy_switch / degraded 留痕思想同源，但更轻。

规则：
- 连续失败（网络类错误）达到 `threshold` 次 → healthy=False（链执行器默认跳过）；
- 一旦成功立即清零（恢复）；
- **非网络类失败不计入连续失败**——例如数据本身缺失（pingzhongdata 里没有
  netWorthTrend、未知代码、空响应可解析但无行）。这是 08-27 科创板事故的直接
  教训：腾讯对部分 688xxx「稳定失败」，若按次数降权会把一个其实正常的源冤枉
  踢出链。`error` 文本以 `network:` 开头才计失败；其余视为「确定性失败」，
  不影响健康度（provider 实现负责给网络错误加 `network:` 前缀）。
"""
from __future__ import annotations


class HealthTracker:
    NETWORK_PREFIX = "network:"

    def __init__(self, threshold: int = 3) -> None:
        if threshold < 1:
            raise ValueError("threshold 必须 ≥ 1")
        self._threshold = threshold
        self._network_fails: dict[str, int] = {}

    def _is_network_error(self, error: str | None) -> bool:
        return bool(error) and error.startswith(self.NETWORK_PREFIX)

    def record(self, name: str, *, ok: bool, error: str | None = None) -> None:
        if ok or not self._is_network_error(error):
            # 成功 → 清零；确定性失败 → 不动计数（也永不升降级）
            if ok:
                self._network_fails[name] = 0
            return
        self._network_fails[name] = self._network_fails.get(name, 0) + 1

    def healthy(self, name: str) -> bool:
        return self._network_fails.get(name, 0) < self._threshold

    def fail_count(self, name: str) -> int:
        return self._network_fails.get(name, 0)

    def reset(self, name: str | None = None) -> None:
        if name is None:
            self._network_fails.clear()
        else:
            self._network_fails.pop(name, None)
