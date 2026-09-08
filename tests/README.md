# 测试运行指南（2026-08-31，A6 分层）

项目测试为 unittest 风格（无需 pytest；pip 受 PEP 668 限制时用标准库跑法）。

## 快路径（开发回归，秒级~十几秒）
```bash
cd /home/summer/QuantV1
python3 -m unittest discover tests
```
重型测试（真实数据/网络依赖）默认跳过：
- `test_holdings_visibility.py`（约 76s，持仓历史网络可见性）

## 全量（含重型，CI/发版前）
```bash
RUN_SLOW_TESTS=1 python3 -m unittest discover tests
```

## 单文件
```bash
python3 -m unittest tests.test_forecast_split -v
```

## 分层约定（对齐 GPT 四审建议的 unit/integration/e2e）
- 快路径 ≈ unit + 轻量集成（不依赖真实网络/重型数据）
- `RUN_SLOW_TESTS=1` ≈ 完整回归（真实数据、重型计算）
- 新增重型用例时：文件级加
  `@unittest.skipUnless(os.environ.get("RUN_SLOW_TESTS") == "1", "重型：RUN_SLOW_TESTS=1 才运行")`
