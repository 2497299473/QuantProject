"""跨 provider 共用的符号/市场映射（V4 步 2 起单一来源）。

此前 `_MKT`（core/stock_data.py、core/real_time.py）与 `_EM_SECID`（core/stock_data.py）
各自维护，迁 providers 时抽到这里，避免三份副本漂移。

市场前缀口径（东财约定）：'0' = 深 / '1' = 沪。
"""
from __future__ import annotations

TENCENT_PREFIX = {"0": "sz", "1": "sh"}      # → 腾讯符号前缀（web.ifzq.gtimg.cn / qt.gtimg.cn）
EM_SECID_PREFIX = {"0": "0", "1": "1"}       # → 东财 secid 前缀（深 0 / 沪 1）


def tencent_symbol(code: str, market: str) -> str:
    """深 000001 → 'sz000001'；沪 600519 → 'sh600519'。"""
    return f"{TENCENT_PREFIX[market]}{code}"


def eastmoney_secid(code: str, market: str) -> str:
    """深 000001 → '0.000001'；沪 600519 → '1.600519'。"""
    return f"{EM_SECID_PREFIX[market]}.{code}"


def tushare_code(code: str, market: str) -> str:
    """深 000001 → '000001.SZ'；沪 600519 → '600519.SH'。"""
    return f"{code}.{'SZ' if market == '0' else 'SH'}"
