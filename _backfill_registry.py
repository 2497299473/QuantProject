"""一次性脚本：把存量 forecast_v1.pkl（2026-08-28）补登进 registry。

背景：registry 机制 2026-08-29（P1）才落地，存量 pkl 无注册条目。
本脚本从 pkl 自身 payload 恢复元数据（trained_at/oos_start）并登记 sha256，
保持 load_models 的 hash 校验可用。重训一次后本脚本不再需要（save_models 自动登记）。
"""
import pickle
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from core import model_registry, forecast_engine

path = forecast_engine.MODELS_DIR / f"forecast_v{forecast_engine.MODEL_VERSION}.pkl"
if not path.exists():
    print(f"[skip] {path} 不存在，无需补登")
    sys.exit(0)

ok, reason = model_registry.verify_model(path)
if ok:
    print(f"[skip] 已登记且 hash 一致：{path.name}")
    sys.exit(0)

with open(path, "rb") as fh:
    payload = pickle.load(fh)
digest = model_registry.register_model(path, meta={
    "model_version": forecast_engine.MODEL_VERSION,
    "feature_keys": payload.get("feature_keys"),
    "horizons": payload.get("horizons"),
    "flat_margin": payload.get("flat_margin"),
    "trained_at": payload.get("trained_at"),
    "oos_start": payload.get("oos_start"),
    "n_train": None,   # 存量 pkl 未留档 n_train（08-28 训练，registry 落地前）
    "note": "legacy backfill 2026-08-29（registry 机制上线前训练）",
})
if digest:
    print(f"[ok] 补登完成：{path.name} sha256={digest[:16]}…")
    ok2, reason2 = model_registry.verify_model(path)
    print(f"[verify] {ok2} {reason2}")
    sys.exit(0)
else:
    print("[fail] 补登失败（registry 写入错误）")
    sys.exit(1)
