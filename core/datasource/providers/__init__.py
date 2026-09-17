"""providers/：一个源一个文件（迁移步 2~4 起逐个落地）。

规划（见《V4数据源层重构方案-SourceRegistry-20260917》§二）：
- stock_tencent.py / stock_eastmoney.py / stock_tushare.py   ← 步 2 迁自 stock_data.py
- realtime_tencent.py                                         ← 步 3 迁自 real_time.py
- fund_eastmoney.py / fund_sina.py                            ← 步 4 迁自 data_loader.py
  与 experiments/sina_nav_redundant/pull_sina_nav.py

约定：
- provider 一律返回 FetchResult，不抛异常；网络类错误文本必须以 `network:` 前缀
  （health.py 据此区分「网络抖动」与「确定性失败」，只把前者计入连续失败）。
- provider **不自动注册自己**（无 import 副作用）；登记发生在装配点。
- 本骨架（步 1）不 import 本目录下任何内容。
"""
