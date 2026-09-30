"""飞书 webhook 推送（蓝色弱参考卡片模板）。

- .env 提供 FEISHU_WEBHOOK（必填）与 FEISHU_SECRET（签名校验，可选）
- 未配置时自动降级为本地落盘（不报错，degrade_to_local=true）
- 卡片恒为蓝色（info 风格）+ 弱参考措辞，与「指令型」红色模板明确区分
"""
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request
from pathlib import Path

from . import netutil

BASE_DIR = Path(__file__).resolve().parent.parent
STANCE_TAG = {"偏多": "🔴 偏多", "偏空": "🟢 偏空", "中性": "⚪ 中性"}


def _load_env() -> dict:
    env_path = BASE_DIR / ".env"
    env = {k: v for k, v in os.environ.items() if k.startswith("FEISHU")}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    return env


def _sign(secret: str, timestamp: int) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def gate_note(gate: dict | None) -> str:
    """发布资格门禁在飞书卡片上的展示行；gate 缺省 ⇒ 空串（旧调用方行为不变）。

    判据与措辞一律取 `publish_gate` 返回的 `detail`（单一真源），本函数只负责呈现。
    注意：run.py 只在 gate.ok=True 的分支才调推送，所以线上这张卡恒为「✅」——
    它的价值是让 Summer 在手机上就能看到「今天这轮是靠哪几条证据放行的」，
    不用回头翻落盘报告。
    """
    if not gate:
        return ""
    ok = bool(gate.get("ok"))
    head = "✅ 可发布" if ok else f"⛔ 不推送（{gate.get('reason') or 'unknown'}）"
    return f"🚦 发布资格：{head} · {gate.get('detail') or '—'}"


def production_note(production: dict | None) -> str:
    """D5-A1（2026-09-30 面 5 审计）：生产资格（production_status）在卡片上的标注行。

    措辞取 `run.production_eligibility` 产出的 note（单一真源，与落盘报告
    production_section 同源），本函数只渲染；production 缺省/note=None（READY）
    ⇒ 空串，旧调用方行为不变。只标注不拦截（BLOCKED 当前是常态）。
    """
    if not production or not production.get("note"):
        return ""
    return str(production["note"])


def _build_card(slot: str, signals: dict, account: dict, realtime: dict | None = None,
                decisions: dict | None = None, gate: dict | None = None,
                production: dict | None = None) -> dict:
    slot_name = {"mid": "⏰ 午盘·实时参考", "post": "🌙 收盘前·最终参考"}.get(
        slot, "☀️ 盘前·今日决策")
    fields = []
    for code, s in signals.items():
        note = "（历史不足）" if s["insufficient_history"] else ""
        fields.append({"is_short": False, "text": {
            "tag": "lark_md",
            "content": f"**{code} {s['name']}**：{STANCE_TAG[s['stance']]}（三因子 {s['score']:+d}）{note} · 净值 {s['last_nav']:.4f}"}})
    acct = (f"市值 {account['total_market_value']:,.2f} 元 · 浮盈 {account['total_pnl_pct']:+.2f}%"
            if account["positions"] else "（无持仓记录）")
    fields.append({"is_short": False, "text": {"tag": "lark_md", "content": f"**账户面**：{acct}"}})
    if realtime:
        for code, r in realtime.items():
            est = r["est_change_pct"]
            top = sorted(r["quotes"].items(), key=lambda kv: -kv[1].get("pct", 0))[:5]
            detail = " / ".join(f"{q['name']}{q['change_pct']:+.1f}%" for _, q in top)
            fields.append({"is_short": False, "text": {
                "tag": "lark_md",
                "content": f"**{code} 当日估算**：{est:+.2f}%（前十大加权）\n{detail}"}})
    if account["abnormal_alerts"]:
        fields.append({"is_short": False, "text": {
            "tag": "lark_md",
            "content": f"⚠️ **异常波动**：{', '.join(account['abnormal_alerts'])} 单日涨跌超 2σ"}})
    # 步 5 source trace：净值实际来源≠东财主源时卡片显式提示（与落盘报告共用判定）
    from .report_generator import source_trace_notes, nav_fallback_notes   # 局部 import：防模块级环
    # D5-D2（2026-09-30 面 5 审计）：cache:fallback 降级告警上卡片——旧实现只在
    # 落盘报告，手机侧（触达面最广）看不到「数据可能是旧缓存」信号。
    # 判定与措辞走 report_generator.nav_fallback_notes（data_loader.is_nav_fallback
    # 单一事实源），不另写 startswith。
    fallbacks = nav_fallback_notes(signals)
    if fallbacks:
        fields.append({"is_short": False, "text": {
            "tag": "lark_md",
            "content": "⚠️ **旧缓存告警**：" + "、".join(fallbacks)
                       + " 本次接口抓取失败已退旧缓存，净值可能非最新。"}})
    switched = source_trace_notes(signals)
    if switched:
        fields.append({"is_short": False, "text": {
            "tag": "lark_md",
            "content": "ℹ️ **数据源切换**：" + "、".join(switched)
                       + "。备源与东财口径逐日同源，披露时点可能略有差异。"}})
    if decisions:
        dec_lines = [f"{code}：倾向{d['score']:+d} 候选{d['candidate']} → **{d['action']}**"
                     for code, d in decisions.items()]
        fields.append({"is_short": False, "text": {
            "tag": "lark_md",
            "content": "**动作倾向（观察层）**：" + "；".join(dec_lines)}})

    elements: list = [{"tag": "div", "fields": fields}]
    note = gate_note(gate)
    if note:
        elements.append({"tag": "hr"})
        elements.append({"tag": "note",
                         "elements": [{"tag": "plain_text", "content": note}]})
    # D5-A1：生产资格标注行（BLOCKED/UNKNOWN 时可见；READY 不加行，旧行为不变）
    prod_note = production_note(production)
    if prod_note:
        elements.append({"tag": "hr"})
        elements.append({"tag": "note",
                         "elements": [{"tag": "plain_text", "content": prod_note}]})
    elements.append({"tag": "hr"})
    elements.append({"tag": "note",
                     "elements": [{"tag": "plain_text",
                                   "content": "v4 决策倾向（五维加权，动作层未验证恒为保持不动）"
                                              "· 不构成买卖指令 · 绝不自动下单"}]})
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue",   # 蓝色 = 弱参考；红色保留给真正的风险告警
                "title": {"tag": "plain_text", "content": f"基金日频参谋 · {slot_name}"},
            },
            "elements": elements,
        },
    }


def push_feishu(slot: str, signals: dict, account: dict, realtime: dict | None = None,
                decisions: dict | None = None, gate: dict | None = None,
                production: dict | None = None) -> dict:
    env = _load_env()
    webhook = env.get("FEISHU_WEBHOOK", "").strip()
    if not webhook or "你的token" in webhook:
        return {"ok": False, "skipped": True, "reason": "未配置 FEISHU_WEBHOOK，已降级为本地落盘"}
    payload = _build_card(slot, signals, account, realtime, decisions, gate=gate,
                          production=production)
    if env.get("FEISHU_SECRET", "").strip():
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"] = _sign(env["FEISHU_SECRET"].strip(), ts)
    try:
        # 2026-09-02: 走 netutil.NO_PROXY_OPENER（推送不重试，失败即降级本地落盘）。
        req = urllib.request.Request(
            webhook, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with netutil.NO_PROXY_OPENER.open(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        ok = result.get("code") == 0 or result.get("StatusCode") == 0
        return {"ok": ok, "response": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}
