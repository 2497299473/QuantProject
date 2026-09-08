"""日内快照持久化：11:30 存 → 14:55 读，算出「状态变化量」。

背景（GPT 诊断 P0-2）：此前 11:30/14:55 是两次独立运行，变化量无从算起。
本模块把每个时点的基金级特征落盘 data/intraday/{date}_{slot}.json，
post 时点读取同日 mid 快照 → decision_engine 计算 close_phase_change 等变化量。

文件内只存可序列化的标量（不含完整行情），体积小、可留档。
"""
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STORE_DIR = BASE_DIR / "data" / "intraday"


def snapshot_path(date: str, slot: str) -> Path:
    return STORE_DIR / f"{date}_{slot}.json"


def save_snapshot(date: str, slot: str, funds: dict) -> Path:
    """funds: {code: {est_return, covered_pct, breadth, concentration, reliability,
    holdings_age_days, n_up, n_down, close_phase_change?}}。落盘并清理过期文件（保留最近 30 个）。"""
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    path = snapshot_path(date, slot)
    payload = {"date": date, "slot": slot, "funds": funds}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    files = sorted(STORE_DIR.glob("*.json"))
    for old in files[:-30]:
        old.unlink(missing_ok=True)
    return path


def load_snapshot(date: str, slot: str) -> dict | None:
    path = snapshot_path(date, slot)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
