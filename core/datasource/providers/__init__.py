"""providers/：一个源一个文件（迁移步 2~4 起逐个落地）。

已落地：
- stock_tencent.py / stock_eastmoney.py / stock_tushare.py   ← 步 2（2026-09-17）

待落地：
- realtime_tencent.py                                         ← 步 3 迁自 real_time.py
- fund_eastmoney.py / fund_sina.py                            ← 步 4 迁自 data_loader.py
  与 experiments/sina_nav_redundant/pull_sina_nav.py

约定：
- provider 一律返回 FetchResult，不抛异常；网络类错误文本必须以 `network:` 前缀
  （health.py 据此区分「网络抖动」与「确定性失败」，只把前者计入连续失败）。
- provider **不自动注册自己**（无 import 副作用）；登记发生在装配点。
- providers 自身**不直接 import 网络库**（urllib/requests/socket/curl_cffi 一律禁止），
  取数只能经 `...netutil`（传输层的 IP failover / 指纹兜底 / 重试全在那里）。
- 本包（`core/datasource/`）不 import providers——登记发生在装配点
  （当前为 `core/stock_data.py` 的 `_registry()`），避免 import 副作用与循环依赖。
"""
