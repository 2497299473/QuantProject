#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V4 项目审计契约（2026-09-16）。

把「证据链是否完整」从文档里的承诺变成可执行检查：

    python3 audit_project.py
    python3 audit_project.py --json output/audit_report.json

纪律
----
- **零网络**：不发起任何网络请求。
- **默认留档**：默认写 ``output/audit_current.json``（当前指针）并向
  ``output/audit_history/`` 追加一份带时间戳的快照；**不删不改历史**。
  需要纯只读复核时加 ``--no-write``。
- **可重复**：同一工作区两次运行结论一致（时间敏感项除外）。
- **机器可读**：每条给出 PASS / FAIL / WARN，末段给 RESULT 汇总与四层状态
  （audit_health / model_promotion / action_enable / production_status）。
  以后无论谁来复审，只读这一份机器事实，不再基于报告做二次解释。

状态机（V4-A，2026-09-17）
------------------------
四层状态彼此独立，禁止互相替代：

    audit_health        PASS / PASS_WITH_WARNINGS / FAIL   ← 仅由本次检查计数决定
    model_promotion     APPROVED / BLOCKED                 ← config.model_ready + registry
    action_enable       ENABLED / HOLD_ONLY / BLOCKED      ← config.decision.gates.history_validated
    production_status   READY / BLOCKED                    ← 三者同时达标才 READY

历史教训：旧版把「0 FAIL」直接打印成 ``PRODUCTION: NOT BLOCKED``，而当前
``model_ready=false`` / ``history_validated=false`` 恰恰是靠这两项为假才判 PASS，
于是审计读到的其实是「配置与未获批状态一致」，却被表述成「生产可用」。
假语句退出码（exit=0）≠ 准生证。

退出码
------
0 = 无 FAIL（审计健康为 PASS / PASS_WITH_WARNINGS）
2 = 存在 FAIL（审计健康为 FAIL）
1 = 审计脚本自身异常

注意：退出码只表达 **审计健康**，不表达生产资格。当前 ``production_status`` 为
BLOCKED（模型未获批、动作层锁死），审计健康良好时退出码仍为 0。
"""
from __future__ import annotations

import argparse
import ast
import bisect
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA = BASE_DIR / "data"
OUTPUT = BASE_DIR / "output"

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
REGISTRY = DATA / "model_registry" / "registry.json"
MANIFEST = DATA / "manifest.json"
MANIFEST_HIST = DATA / "manifest_history"
ACTIVE_JOURNAL = OUTPUT / "shadow_actions.jsonl"
LEGACY_ARCHIVE = OUTPUT / "shadow_actions_legacy_invalid.jsonl"
RUN_MANIFEST_DIR = OUTPUT / "run_manifest"
HOLDINGS = BASE_DIR / "holdings.json"      # 发布资格的持仓范围（本地私有，不入库）
AUDIT_CURRENT = OUTPUT / "audit_current.json"
AUDIT_HISTORY = OUTPUT / "audit_history"
AUDIT_SELF = Path(__file__).resolve()      # 排除本脚本自身字面量，避免自指误报

EXPECTED_CHANNELS = ("approved_full", "prereg_degraded", "legacy_invalid")
AXIS_ORDER = {"P0-研究有效性": 0, "P1-证据链": 1, "P2-工程卫生": 2}


@dataclass
class Check:
    cid: str
    axis: str
    title: str
    status: str
    detail: str = ""


@dataclass
class Audit:
    checks: list = field(default_factory=list)
    # 检查侧算出的结构化事实（如 publish_gate），payload 复用、避免双算
    meta: dict = field(default_factory=dict)

    def add(self, cid: str, axis: str, title: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(cid, axis, title, status, detail))

    def counts(self) -> dict:
        out = {PASS: 0, FAIL: 0, WARN: 0}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    def sorted_checks(self) -> list:
        return sorted(self.checks, key=lambda c: (AXIS_ORDER.get(c.axis, 9), c.cid))


# ---------------------------------------------------------------- 基础工具

def sha256_file(p: Path) -> str | None:
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def git_lines(*args: str):
    """返回 (lines, err)；lines 为 None 表示 git 不可用或失败。"""
    try:
        r = subprocess.run(["git", *args], cwd=str(BASE_DIR),
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, type(exc).__name__
    if r.returncode != 0:
        return None, (r.stderr or "").strip()[:200]
    return [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()], ""


def git_worktree_clean() -> tuple[bool | None, str]:
    """工作区是否干净（V4.5，2026-09-23）：返回 ``(clean, detail)``。

    ``clean=None`` 表示**判不了**（git 不可用 / 非仓库 / 超时）——调用方须据此
    判 WARN 而非 PASS：把「查不到」当成「干净」是 fail-open，与项目
    「宁可 FAIL 也不假 PASS」的纪律相反。

    用途：registry 里的 ``git_commit`` 是**权重字节**的锚点提交；若工作区带着
    未提交改动，读侧就无法确定「当前跑的是不是那个已绑定版本」。本函数把这一
    不确定性显式化。``git_lines`` 是既有 git 单一入口，此处复用不另起炉灶。
    """
    lines, err = git_lines("status", "--porcelain")
    if lines is None:
        return None, f"git 不可用或非仓库（{err}）"
    if not lines:
        return True, "工作区干净"
    preview = "；".join(lines[:5])
    return False, (f"{len(lines)} 项未提交改动（前 5：{preview}）"
                   "——registry 绑定的 git_commit 未必等于当前运行代码")


def classify(rec: dict) -> str:
    """镜像 shadow_policy.record_channel 的极小判定。

    不直接 import shadow_policy，避免审计脚本拉起 forecast_engine 等重依赖。
    shadow_policy 侧的通道定义存在性由 P1-5 单独检查，防止两边悄悄漂移。
    """
    if rec.get("evidence_validity") == "legacy_invalid":
        return "legacy_invalid"
    mode = (rec.get("contract") or {}).get("promotion_mode")
    if mode in ("approved_full", "prereg_degraded"):
        return mode
    return "unclassified"


def iter_journal(path: Path):
    if not path.is_file():
        return
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            yield json.loads(ln)
        except json.JSONDecodeError:
            continue


def _registry() -> dict:
    return load_json(REGISTRY) or {}


def _models() -> dict:
    return (_registry().get("models") or {})


# ---------------------------------------------------------------- P0 研究有效性

# --- PIT 前视防护（语义判据，2026-09-17 由字面量匹配改写）-----------------------
# 原实现只抠源码字面量（有没有 `_QTR_LAG =` 与「公告日」字样）：既会把改名误判成
# PASS，也验证不了真正要紧的安全属性。现核验两条语义属性：
#   ① 安全：生效日滞后 ≥ 法定披露时限折算下界（低于即对「卡时限才披露」的基金存在前视）；
#   ② 诚实：源码如实声明该滞后是「法定时限下界」，不冒充真实公告日
#      （东财 F10 无公告日字段，2026-09-17 实测；真实公告日无零网络来源）。
# 法定披露时限（公开规则）：季报 15 个交易日 / 半年报 60 自然日 / 年报 90 自然日。
_LEGAL_QTR_WORKING_DAYS = 15
_LEGAL_HALF_CAL_DAYS = 60
_LEGAL_YEAR_CAL_DAYS = 90
PIT_SRC = BASE_DIR / "core" / "lookthrough.py"
PIT_CALENDAR = DATA / "stock_klines" / "510300.json"


def pit_trading_days() -> list[str] | None:
    """本地交易日历（面板 K 线日期列）；不可得返回 None（判据降级 WARN，不臆断）。"""
    raw = load_json(PIT_CALENDAR)
    kl = raw.get("klines") if isinstance(raw, dict) else raw
    if not isinstance(kl, list):
        return None
    out: list[str] = []
    for r in kl:
        if isinstance(r, (list, tuple)) and r and isinstance(r[0], str):
            out.append(r[0][:10])
        elif isinstance(r, dict):
            d = r.get("date") or r.get("day") or r.get("datetime")
            if isinstance(d, str):
                out.append(d[:10])
    return sorted(set(out)) or None


def pit_lag_floor(mmdd: str, tdays: list[str], years: list[int],
                  today: str) -> int | None:
    """某季末口径的滞后下界（天）= max(法定最晚披露日 − 报告期)，按真实交易日历。"""
    floors: list[int] = []
    for y in years:
        p = f"{y}-{mmdd}"
        if p > today:
            continue
        if mmdd in ("03-31", "09-30"):                       # 15 个交易日
            j = bisect.bisect_right(tdays, p) + _LEGAL_QTR_WORKING_DAYS - 1
            if j >= len(tdays):
                continue
            dl = tdays[j]
        else:                                                # 60 / 90 个自然日
            n = _LEGAL_HALF_CAL_DAYS if mmdd == "06-30" else _LEGAL_YEAR_CAL_DAYS
            dl = (datetime.strptime(p, "%Y-%m-%d")
                  + timedelta(days=n)).strftime("%Y-%m-%d")
        floors.append((datetime.strptime(dl, "%Y-%m-%d")
                       - datetime.strptime(p, "%Y-%m-%d")).days)
    return max(floors) if floors else None


def check_pit(a: Audit) -> None:
    """PIT 前视防护：滞后下界 ≥ 法定时限（安全）+ 如实声明（诚实）。"""
    cid, axis = "P0-1", "P0-研究有效性"
    title = "PIT 无前视（生效日滞后 ≥ 法定披露时限）"
    txt = PIT_SRC.read_text(encoding="utf-8") if PIT_SRC.is_file() else ""
    if not txt:
        a.add(cid, axis, "PIT 前视防护", WARN, "core/lookthrough.py 未找到")
        return

    m = re.search(r"^_QTR_LAG\s*=\s*(\{[^}]*\})", txt, re.M)
    if not m:
        a.add(cid, axis, title, FAIL, "core/lookthrough.py 未声明生效日滞后表 _QTR_LAG")
        return
    try:
        lag = ast.literal_eval(m.group(1))
    except (ValueError, SyntaxError):
        a.add(cid, axis, title, FAIL, "滞后表 _QTR_LAG 无法解析")
        return
    if not isinstance(lag, dict) or not lag:
        a.add(cid, axis, title, FAIL, "滞后表 _QTR_LAG 非非空映射")
        return

    # 诚实性：如实声明为「法定时限下界」，不冒充真实公告日
    declared = bool(re.search(
        r"法定.{0,8}(时限|下界)|披露.{0,6}(时限|下界)|非.{0,8}公告日", txt))

    tdays = pit_trading_days()
    years = (load_json(BASE_DIR / "config.json") or {}).get(
        "lookthrough", {}).get("history_years") or []
    if tdays is None or not years:
        a.add(cid, axis, title, WARN,
              f"本地交易日历或 history_years 不可得，下界无法核验"
              f"（已声明下界={declared}，滞后={lag}）")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    floors, bad = {}, []
    for mmdd in ("03-31", "06-30", "09-30", "12-31"):
        fl = pit_lag_floor(mmdd, tdays, [int(y) for y in years], today)
        if fl is None:
            continue
        floors[mmdd] = fl
        cur = lag.get(mmdd)
        if not isinstance(cur, int) or cur < fl:
            bad.append(f"{mmdd}: {cur} < {fl}")

    if bad:
        a.add(cid, axis, title, FAIL,
              "生效日滞后低于法定披露下界，存在前视：" + "；".join(bad))
    elif not declared:
        a.add(cid, axis, title, WARN,
              f"滞后均达标（下界 {floors}，当前 {lag}），但源码未如实声明为"
              "「法定时限下界」（勿冒充真实公告日）")
    else:
        a.add(cid, axis, title, PASS,
              f"下界 {floors}；当前 {lag} 均达标；已声明为法定时限下界（非真实公告日）")


def check_holdings_privacy(a: Audit) -> None:
    tracked, err = git_lines("ls-files")
    if tracked is None:
        a.add("P0-2", "P0-研究有效性", "持仓文件未进入版本库", WARN, f"git 不可用：{err}")
        return
    hits = [t for t in tracked if t.endswith("holdings.json")]
    a.add("P0-2", "P0-研究有效性", "持仓文件未进入版本库",
          PASS if not hits else FAIL,
          "；".join(hits) if hits else "未被跟踪")


def check_manifest_resolvable(a: Audit) -> None:
    if not MANIFEST.is_file():
        a.add("P0-3", "P0-研究有效性", "数据快照可回溯（内嵌 hash 可解析）", FAIL,
              "data/manifest.json 缺失")
        return
    allowed = {sha256_file(MANIFEST)[:16]}
    hist = sorted(MANIFEST_HIST.glob("manifest_*.json")) if MANIFEST_HIST.is_dir() else []
    for h in hist:
        digest = sha256_file(h)
        if digest:
            allowed.add(digest[:16])
    embedded: dict = {}
    for rec in iter_journal(ACTIVE_JOURNAL) or ():
        h = (rec.get("contract") or {}).get("data_manifest_sha256")
        if h:
            embedded[h] = embedded.get(h, 0) + 1
    unresolved = {h: n for h, n in embedded.items() if h not in allowed}
    detail = (f"当前 {sha256_file(MANIFEST)[:16]}；归档 {len(hist)} 版；"
              f"journal 内嵌 {len(embedded)} 个 hash（{sum(embedded.values())} 条记录）")
    if unresolved:
        a.add("P0-3", "P0-研究有效性", "数据快照可回溯（内嵌 hash 可解析）", FAIL,
              detail + f"；无法解析：{unresolved}")
    else:
        a.add("P0-3", "P0-研究有效性", "数据快照可回溯（内嵌 hash 可解析）", PASS, detail)


def check_manifest_freshness(a: Audit) -> None:
    if not MANIFEST.is_file():
        a.add("P0-4", "P0-研究有效性", "数据快照新鲜度", FAIL, "data/manifest.json 缺失")
        return
    age = (datetime.now() - datetime.fromtimestamp(MANIFEST.stat().st_mtime)).days
    payload = load_json(MANIFEST) or {}
    detail = (f"generated_at={payload.get('generated_at')} · n_files={payload.get('n_files')} · "
              f"年龄 {age} 天")
    a.add("P0-4", "P0-研究有效性", "数据快照新鲜度",
          WARN if age > 7 else PASS, detail + ("（>7 天，建议重生成）" if age > 7 else ""))


def check_manifest_scope_acyclic(a: Audit) -> None:
    """数据快照清单不得包含 provenance 文件（V4.1 ①，2026-09-18）。

    生产事实的常驻守护：`model_registry.capture_provenance()` 把本清单文件的
    sha256 写进 registry，若 registry / 模型权重又出现在清单里，就构成自指环——
    重生成即永远不一致，Evidence Contract 失去严格意义。data_fingerprint 侧有
    生成时硬校验，但**已落盘的文件**还得靠审计在每次运行时复核（防止有人手工
    回填、或用旧版本脚本重新生成）。

    同时复算 snapshot_id：落盘值必须能由 files 重算得出，否则说明清单被改写过。
    """
    if not MANIFEST.is_file():
        a.add("P0-5", "P0-研究有效性", "数据快照 scope 无自指环", FAIL,
              "data/manifest.json 缺失")
        return
    payload = load_json(MANIFEST) or {}
    files = {str(k).replace("\\", "/"): v for k, v in (payload.get("files") or {}).items()}
    if not files:
        a.add("P0-5", "P0-研究有效性", "数据快照 scope 无自指环", FAIL,
              "清单 files 为空——无可核验内容")
        return
    # 与 data_fingerprint.PROVENANCE_DIRS 同口径；不 import 以免审计拉起额外模块
    provenance = ("model_registry", "models")
    offenders = sorted(k for k in files
                       if any(part in provenance for part in k.split("/")))
    scope = payload.get("scope")
    if offenders:
        a.add("P0-5", "P0-研究有效性", "数据快照 scope 无自指环", FAIL,
              f"{len(offenders)} 项 provenance 文件入清单（环复现）：{offenders[:3]}"
              "；须用 data_fingerprint.py 重生成")
        return
    if scope != "dataset_inputs":
        a.add("P0-5", "P0-研究有效性", "数据快照 scope 无自指环", WARN,
              f"scope={scope!r}（期望 'dataset_inputs'）——无环但语义未标注，"
              "建议重生成")
        return
    import hashlib
    h = hashlib.sha256()
    h.update(b"dataset_inputs")
    for rel in sorted(files):
        h.update(f"{rel}\0{(files[rel] or {}).get('sha256', '')}\n".encode("utf-8"))
    sid = payload.get("snapshot_id")
    if sid != h.hexdigest()[:16]:
        a.add("P0-5", "P0-研究有效性", "数据快照 scope 无自指环", FAIL,
              f"snapshot_id={sid} 与 files 复算值 {h.hexdigest()[:16]} 不符"
              "——清单内容与其 ID 不一致（疑似手工改写）")
        return
    a.add("P0-5", "P0-研究有效性", "数据快照 scope 无自指环", PASS,
          f"scope=dataset_inputs · snapshot_id={sid} · {len(files)} 项均为数据输入"
          "（registry/models 已出清单）")


# ---------------------------------------------------------------- P1 证据链

def check_registry_hash(a: Audit) -> None:
    models = _models()
    if not models:
        a.add("P1-1", "P1-证据链", "模型权重 sha256 与注册表一致", FAIL,
              "registry.json 缺失或无 models")
        return
    bad = []
    for name, entry in models.items():
        p = DATA / "models" / Path(str(entry.get("path") or name)).name
        got = sha256_file(p)
        if got is None:
            bad.append(f"{name} 权重缺失")
        elif got != entry.get("sha256"):
            bad.append(f"{name} sha256 不符")
    a.add("P1-1", "P1-证据链", "模型权重 sha256 与注册表一致",
          PASS if not bad else FAIL,
          f"{len(models)} 个模型" + ("；" + "；".join(bad) if bad else ""))


def check_registry_paths(a: Audit) -> None:
    """registry 的 ``path`` 字段必须可判读（V4.5，2026-09-23）。

    实况：两个条目都存着 WSL 时代的绝对路径
    ``/home/summer/QuantV1/data/models/forecast_v2.pkl`` —— Windows 迁移后该路径
    永远指不到文件。``check_registry_hash`` 靠 ``Path(...).name`` 兜底，所以
    **哈希校验照样 PASS**，坏字段一路静默；只有人肉读 registry 才会发现。

    判据（逐条，任一不满足即 WARN —— 档案事实，不阻断当前生产）：
    ① 不是绝对路径（Unix ``/`` 开头 或 Windows 盘符）；
    ② 归一到正斜杠后与 ``provenance.git_tracked_path`` 一致（有该字段时）。
    两条合起来才说明「这个 path 描述的是仓库内位置」，而非某台机器的临时布局。
    """
    models = _models()
    if not models:
        a.add("P1-10", "P1-证据链", "registry 路径字段可判读", WARN,
              "registry.json 缺失或无 models")
        return
    bad: list[str] = []
    for name, entry in models.items():
        raw = str(entry.get("path") or "")
        reasons = []
        if not raw:
            reasons.append("缺失")
        elif raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
            reasons.append(f"绝对路径（{raw[:40]}…）" if len(raw) > 40 else f"绝对路径（{raw}）")
        tracked = (entry.get("provenance") or {}).get("git_tracked_path")
        if tracked and raw and raw.replace("\\", "/") != str(tracked).replace("\\", "/"):
            reasons.append(f"与 git_tracked_path（{tracked}）不一致")
        if reasons:
            bad.append(f"{name}：" + "、".join(reasons))
    a.add("P1-10", "P1-证据链", "registry 路径字段可判读",
          PASS if not bad else WARN,
          f"{len(models)} 个模型路径均为仓库相对路径" if not bad
          else "；".join(bad) + "（重跑 train_forecast_model 登记时自动归一）")


def check_validation_binding(a: Audit) -> None:
    """验证报告绑定核验（V4.1 ⑥ 收紧：active model 缺绑定即 FAIL）。

    旧实现 `if not rf: continue` —— 某模型**根本没写** report_file 时被静默跳过，
    既不计入 n 也不进 bad，于是「无绑定」与「绑定正确」同样得到 PASS。这与
    model_registry.verify_validation_report() 的严格语义相反（那边缺字段直接判不过）。

    现按 active / historical 分档（与 ② 同一套设计原则）：
      - active model 缺 report_file / report_sha256 ⇒ FAIL（生产资格证据必须齐）；
      - 历史模型缺绑定                    ⇒ WARN（档案事实，不阻断当前）。
    """
    models = _models()
    fc = (_config().get("forecast") or {})
    active = str(fc.get("active_model") or "").strip()
    bad, warn, n = [], [], 0
    for name, entry in models.items():
        v = entry.get("validation") or {}
        rf = v.get("report_file")
        if not rf or not v.get("report_sha256"):
            miss = "report_file+report_sha256" if not rf and not v.get("report_sha256") \
                else ("report_file" if not rf else "report_sha256")
            if name == active:
                bad.append(f"{name}（active）缺 {miss}")
            else:
                warn.append(f"{name} 缺 {miss}（档案）")
            continue
        n += 1
        got = sha256_file(BASE_DIR / rf)
        if got is None:
            bad.append(f"{name} 报告缺失 {rf}")
        elif got != v.get("report_sha256"):
            bad.append(f"{name} 报告 sha256 不符")
    status = FAIL if bad else (WARN if warn else PASS)
    detail = f"{n} 份报告已核验；active={active or '未声明'}"
    parts = bad + warn
    a.add("P1-2", "P1-证据链", "验证报告 sha256 与注册表绑定一致", status,
          detail + ("；" + "；".join(parts) if parts else ""))


def check_promotion_consistency(a: Audit) -> None:
    """config.model_ready 与 **active model** 的 promotion 是否自洽（V4.1 ② 收紧）。

    旧实现拿「registry 中全部 blocked 模型」当否决条件，因此一旦 model_ready
    置 true，历史失败记录（v2 / v3 永久 blocked）就把这条检查永久钉死在 FAIL——
    等于审计在逼人删除历史证据。现只看 active model；历史 blocked 转为可见信息。
    """
    models = _models()
    fc = (_config().get("forecast") or {})
    ready = bool(fc.get("model_ready", False))
    promotion, basis = resolve_model_promotion(fc.get("active_model"), models, ready)
    active, hist = basis["active_model"], basis["historical_blocked"]

    if ready and basis["reason"] != "ok":
        detail = (f"model_ready=true 但 active={active or '未声明'} 判据="
                  f"{basis['reason']}（active promotion={basis['active_promotion']}）")
        a.add("P1-3", "P1-证据链", "promotion 与 config.model_ready 一致", FAIL, detail)
    else:
        note = f"model_ready={ready}；active={active or '未声明'}"
        if hist:
            note += f"；历史 blocked {len(hist)} 个（不参与当前裁决）"
        a.add("P1-3", "P1-证据链", "promotion 与 config.model_ready 一致", PASS, note)


def check_history_validated(a: Audit) -> None:
    cfg = load_json(BASE_DIR / "config.json") or {}
    gates = (cfg.get("decision") or {}).get("gates") or {}
    if "history_validated" not in gates:
        a.add("P1-4", "P1-证据链", "动作层历史验证硬门禁", FAIL,
              "config.decision.gates.history_validated 缺失")
        return
    hv = gates.get("history_validated")
    a.add("P1-4", "P1-证据链", "动作层历史验证硬门禁",
          PASS if hv is False else FAIL,
          f"history_validated={hv}" + ("" if hv is False else "（须有 backtest_action 稳定超额证据才可置 true）"))


def check_approval_binding(a: Audit) -> None:
    models = _models()
    if not models:
        a.add("P1-8", "P1-证据链", "审批绑定数据快照 + 代码版本", FAIL, "registry 缺失")
        return
    missing = []
    for name, entry in models.items():
        v = entry.get("validation") or {}
        has_snap = bool(entry.get("data_manifest_sha256") or v.get("data_manifest_sha256"))
        has_commit = bool(entry.get("git_commit") or v.get("git_commit"))
        lack = []
        if not has_snap:
            lack.append("data_manifest_sha256")
        if not has_commit:
            lack.append("git_commit")
        if lack:
            missing.append(f"{name} 缺 {'/'.join(lack)}")
    a.add("P1-8", "P1-证据链", "审批绑定数据快照 + 代码版本",
          PASS if not missing else FAIL, "；".join(missing))


def check_ssot_git_tip(a: Audit) -> None:
    """release code 指针校验（P1-12）。

    ``evidence/release_code_tip.txt`` 记录「release 收口时的代码 tip」；该指针文件本身
    会形成下一次 commit，因此正常闭合状态是 pointer == HEAD^。
    若 HEAD 在 SSOT 收口后继续前进，则明确标 WARN，而不是把查不到/不一致当 PASS。
    """
    pointer = BASE_DIR / "evidence" / "ssot_git_tip.txt"
    if not pointer.is_file():
        a.add("P1-12", "P1-证据链", "release code tip 对账", WARN,
              "缺失 evidence/release_code_tip.txt（无法判断 SSOT 是否落后当前代码）")
        return
    try:
        declared = pointer.read_text(encoding="utf-8").strip()
        current = subprocess.run(
            ["git", "-C", str(BASE_DIR), "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()
        parent = subprocess.run(
            ["git", "-C", str(BASE_DIR), "rev-parse", "HEAD^"],
            capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        a.add("P1-12", "P1-证据链", "release code tip 对账", WARN,
              f"无法读取 Git tip：{type(exc).__name__}")
        return
    if declared == parent:
        a.add("P1-12", "P1-证据链", "release code tip 对账", PASS,
              f"release code tip={declared[:12]}…；当前闭合提交={current[:12]}…")
    else:
        short_declared = declared[:12] if declared else "空"
        a.add("P1-12", "P1-证据链", "release code tip 对账", WARN,
              f"release code tip={short_declared}；当前 HEAD^={parent[:12]}… ⇒ release code tip 可能落后当前代码")

def check_worktree_clean(a: Audit) -> None:
    """工作区干净度（V4.5，2026-09-23）：registry 绑的 commit 是否就是当前代码。

    ``git_commit`` 只在**登记那一刻**写入。若工作区带着未提交改动，读侧就无法
    确定「当前跑的是不是那个已绑定版本」——本检查把这一不确定性显式化。

    分级（刻意保守，避免自锁）：
      - 干净              ⇒ PASS；
      - 有未提交改动      ⇒ **WARN**（本地开发常态：跑审计时几乎总有在改的文件；
        判 FAIL 会让 audit_health 常年 FAIL、并触发「审计 FAIL ⇒ 资格层 FAIL」的
        自锁，与本项目 P1-9 的取舍同理）；
      - git 不可用/非仓库 ⇒ **WARN**（不是「干净」——把查不到当干净是 fail-open）。
    """
    clean, detail = git_worktree_clean()
    if clean is None:
        a.add("P1-11", "P1-证据链", "工作区干净度（代码锚点可比）", WARN,
              detail + "——无法确认当前代码与 registry 绑定版本一致")
    else:
        a.add("P1-11", "P1-证据链", "工作区干净度（代码锚点可比）",
              PASS if clean else WARN, detail)


def check_shadow_channels(a: Audit) -> None:
    src = BASE_DIR / "shadow_policy.py"
    txt = src.read_text(encoding="utf-8") if src.is_file() else ""
    if not txt:
        a.add("P1-5", "P1-证据链", "影子证据通道隔离", WARN, "shadow_policy.py 未找到")
        return
    ok = all(c in txt for c in EXPECTED_CHANNELS) and "load_records_by_channel" in txt
    a.add("P1-5", "P1-证据链", "影子证据通道隔离",
          PASS if ok else FAIL,
          "" if ok else "未见 CHANNELS / load_records_by_channel")


def check_legacy_archived(a: Audit) -> None:
    if not ACTIVE_JOURNAL.is_file():
        a.add("P1-6", "P1-证据链", "legacy_invalid 已移出活跃流", WARN,
              "活跃 journal 不存在（尚无影子记录）")
        return
    buckets: dict = {}
    for rec in iter_journal(ACTIVE_JOURNAL) or ():
        ch = classify(rec)
        buckets[ch] = buckets.get(ch, 0) + 1
    n_legacy = buckets.get("legacy_invalid", 0)
    a.add("P1-6", "P1-证据链", "legacy_invalid 已移出活跃流",
          PASS if n_legacy == 0 else FAIL,
          f"活跃流分布 {buckets}"
          + ("" if n_legacy == 0 else f"；仍有 {n_legacy} 条 legacy 混在活跃流（可 --arch 归档）"))


MANIFEST_REQUIRED_KEYS = ("run_id", "slot", "status", "data", "lookthrough",
                          "realtime", "degraded_reasons")
"""最新清单必须齐备的键——缺任一即「监控瞎眼」，属契约破坏（FAIL）。"""

MANIFEST_STATUSES = ("SUCCESS", "DEGRADED", "FAILED")
"""清单 status 词表 = run.py 三态退出码（0 / 2 / 1）的镜像（V4.5 P0，2026-09-23）。

旧词表只有 SUCCESS / DEGRADED——与 README「0=SUCCESS / 2=DEGRADED / 1=FAILED」
自相矛盾：run.py 一旦补上 FAILED 兜底清单，审计会把一份**合格**的失败证据判成
「status 非三态词表」。三态在此显式收口，SUCCESS/DEGRADED 之外的**非法值**仍 FAIL。
"""

MID_CUTOFF = (11, 30)
"""午盘时点：早于此点，当日尚无「本应产出」的清单（11:30 mid 任务）。"""


def _holidays() -> set[str]:
    """data/holidays.json → 法定休市日集合（读不到 ⇒ 空集，退化为「只看周末」）。"""
    try:
        data = json.loads((DATA / "holidays.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    out: set[str] = set()
    for _year, days in (data.get("years") or {}).items():
        for dates in (days or {}).values():
            out.update(dates or [])
    return out


def is_trading_day(d: datetime, holidays: set[str] | None = None) -> bool:
    """周一~五且非法定休市日（审计侧独立实现：本脚本 stdlib-only，不 import run）。"""
    if d.weekday() >= 5:
        return False
    return d.strftime("%Y-%m-%d") not in (_holidays() if holidays is None else holidays)


def last_expected_run_date(now: datetime | None = None) -> str:
    """最近一个「本应已产出运行清单」的交易日（V4.2 P1-7）。

    规则：当天是交易日且已过 11:30 ⇒ 今天；否则回溯到上一个交易日。
    于是周一 09:00 不会把周五的清单误判过期，周一 15:00 还停在周五就判过期。
    """
    now = now or datetime.now()
    holidays = _holidays()
    if is_trading_day(now, holidays) and (now.hour, now.minute) >= MID_CUTOFF:
        return now.strftime("%Y-%m-%d")
    d = now - timedelta(days=1)
    while not is_trading_day(d, holidays):
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def check_run_manifest(a: Audit, now: datetime | None = None) -> None:
    """P1-7（V4.2 收紧，2026-09-18）：证明「当前运行的证据完整」，而非「曾经跑过」。

    旧实现只要求 output/run_manifest/ 里存在任意一个 JSON 就 PASS 且取 files[-1]：
    于是「今天没正常运行、昨天留了一份 SUCCESS」照样 PASS。现按四问核验最新清单：

    ① 自洽：文件名与 ``run_id`` 一致、run_id 日期可解析；
    ② 齐备：``MANIFEST_REQUIRED_KEYS`` 全在，且 ``status`` 属于三态词表；
    ③ 新鲜度：清单日期不得早于 ``last_expected_run_date()``（午盘时点判定，周末/假期不误报）；
    ④ 掩码：逐基金字段不得残留 6 位代码（V4.1 ④ 契约；残留 ⇒ FAIL）。

    结论写进 ``a.meta["run_manifest"]``，供 ``audit_payload`` 落 ``evidence_as_of`` /
    ``evidence_stale``——「current 指针」自己得说清它描述的证据截至哪一天。
    """
    txt = (BASE_DIR / "run.py").read_text(encoding="utf-8") if (BASE_DIR / "run.py").is_file() else ""
    tri = "DEGRADED" in txt and "return 2" in txt
    files = sorted(RUN_MANIFEST_DIR.glob("run_manifest_*.json")) if RUN_MANIFEST_DIR.is_dir() else []
    now = now or datetime.now()
    expect = last_expected_run_date(now)
    if not files:
        a.meta["run_manifest"] = {"latest": None, "as_of": None,
                                  "expected_as_of": expect, "stale": True, "n_files": 0}
        a.add("P1-7", "P1-证据链", "运行清单已实际产出",
              WARN if tri else FAIL,
              "三态退出码已实现但 output/run_manifest/ 无产物（证据链尚未兑现）")
        return

    latest = files[-1]
    payload = load_json(latest) or {}
    rid = latest.name[len("run_manifest_"):-len(".json")]
    problems: list[str] = []
    if payload.get("run_id") != rid:
        problems.append(f"run_id（{payload.get('run_id')}）与文件名不一致")
    missing = [k for k in MANIFEST_REQUIRED_KEYS if k not in payload]
    if missing:
        problems.append(f"缺关键字段 {missing}（监控会瞎眼）")
    if payload.get("status") not in MANIFEST_STATUSES:
        problems.append(f"status 非三态词表（{payload.get('status')!r}）")
    try:
        assert_no_fund_codes(payload)
    except ValueError as exc:
        problems.append(str(exc))
    day = None
    try:
        day = datetime.strptime(rid[:8], "%Y%m%d")
    except (ValueError, IndexError):
        problems.append(f"文件名日期不可解析（{rid}）")

    as_of = day.strftime("%Y-%m-%d") if day else None
    stale = bool(as_of) and as_of < expect
    a.meta["run_manifest"] = {"latest": latest.name, "run_id": payload.get("run_id"),
                              "status": payload.get("status"), "as_of": as_of,
                              "expected_as_of": expect, "stale": stale,
                              "n_files": len(files)}
    if problems:
        a.add("P1-7", "P1-证据链", "运行清单已实际产出", FAIL,
              f"最新清单 {latest.name} 不合格：" + "；".join(problems))
    elif payload.get("status") == "FAILED":
        # V4.5 P0（2026-09-23）：FAILED 兜底清单是**合格证据**（三态之一、掩码合规），
        # 但它如实报告「本次运行崩了」——不得当作 SUCCESS 放行。判 FAIL 让失败可
        # 见（旧实况：09-23 post 轮崩了却无任何清单，审计只能看到「没有清单」）。
        a.add("P1-7", "P1-证据链", "运行清单已实际产出", FAIL,
              f"最新清单 {latest.name} status=FAILED"
              f"（reason={payload.get('failure_reason')} · "
              f"stage={payload.get('failure_stage')} · "
              f"detail={payload.get('failure_detail') or '—'}）；"
              f"异常原文见 output/logs/（本地，不入库）")
    elif stale:
        a.add("P1-7", "P1-证据链", "运行清单已实际产出", WARN,
              f"{len(files)} 份，但最新 {latest.name} 停在 {as_of}，"
              f"早于最近应产出交易日 {expect}（{now:%Y-%m-%d %H:%M} 时点判定）")
    else:
        a.add("P1-7", "P1-证据链", "运行清单已实际产出", PASS,
              f"{len(files)} 份；最新 {latest.name} status={payload.get('status')}"
              f" · 证据截至 {as_of}")


# ---------------------------------------------------------- P1-9 发布资格门禁

GATE_EXCLUDED_REASONS = frozenset({"feishu_push_failed"})
"""防自锁白名单：推送成败是被门禁**控制**的对象，不得作为门禁输入——
否则「今日推送失败 ⇒ 今日清单带 feishu_push_failed ⇒ 明日门禁永不可过」。"""

GATE_FUND_KEYED = {
    "fund_data_partial": ("data", "failed"),
    "fund_data_fallback": ("data", "fallback"),   # V4.1 ③：全链失败退旧缓存
    "lookthrough_missing": ("lookthrough", "missing"),
    "realtime_failed": ("realtime", "failed"),
    "realtime_degraded": ("realtime", "degraded"),
}
"""逐基金降级项前缀 → 清单结构化字段 (节, 键)。归因优先读结构化字段，
前缀后的裸代码串仅作兜底；归因不成立即升运行级（fail-closed，宁严勿漏）。"""

_CODE_RE = re.compile(r"\d{6}")


def held_fund_codes() -> list[str]:
    """holdings.json → shares>0 的基金代码；文件缺失/不可读返回空表（调用方兜底）。"""
    h = load_json(HOLDINGS) or {}
    funds = h.get("funds") or {}
    return [str(c) for c, v in funds.items() if (v or {}).get("shares")]


def attribute_degradations(reasons, structured, universe, pool=None):
    """降级项分解 → (run_level 列表, per_fund 字典)。

    - 前缀在 GATE_FUND_KEYED 且代码可归因（结构化字段 ∪ 后缀裸代码/别名）⇒ 记到具体基金；
    - 其余（market_context_unavailable / shadow_failed:* / intraday_features_empty …）
      以及归因漂移（代码不在论域内、字段与后缀对不上）⇒ 运行级 ⇒ 污染全部基金。

    V4.1 ④（2026-09-18）：清单的逐基金字段与降级项后缀已改写成位置别名（``F1``…），
    本函数按同一张基准表反查（``resolve_fund_refs``，``pool`` 缺省取 config.fund_pool）。
    反查不出来的（基准表缺失、指纹不一致、F 号越界）**不推断**，一律落到
    「代码不在论域」的 fail-closed 分支。
    """
    run_level: list[str] = []
    per_fund: dict[str, list[str]] = {}
    structured = structured if isinstance(structured, dict) else {}
    pool = fund_pool() if pool is None else pool
    fp = (structured.get("fund_refs") or {}).get("pool_sha256_8")
    for raw in reasons:
        r = str(raw)
        prefix, _, suffix = r.partition(":")
        codes: set[str] = set()
        if prefix in GATE_FUND_KEYED:
            section, key = GATE_FUND_KEYED[prefix]
            sec = structured.get(section) or {}
            listed = sec.get(key) if isinstance(sec, dict) else None
            toks = [str(x) for x in (listed or [])]
            toks += [t for t in suffix.split(",")
                     if t and (_CODE_RE.fullmatch(t) or FUND_REF_RE.fullmatch(t))]
            codes |= set(resolve_fund_refs(toks, pool, fp))
            if codes and codes <= universe:
                for c in sorted(codes):
                    per_fund.setdefault(c, []).append(r)
                continue
        run_level.append(r)
    return run_level, per_fund


def fund_evidence_complete(m: dict) -> bool:
    """本轮清单是否对论域做过完整逐基金评估（last-run-wins 的「可作准」条件）。

    四个逐基金字段的 ok/failed 必须全部在场且 data.ok=True（基金数据拉了但
    部分失败 ⇒ 评估残缺）。**故意不看** lookthrough.ok / market_context.ok：
    这些是运行级问题，由 run_level 全天粘性另行拦截，不得让它们在「覆盖」时
    顺便把逐基金 last-run-wins 也否掉（那是双重惩罚，也非本函数职责）。
    """
    def sect(name):
        return m.get(name) if isinstance(m.get(name), dict) else {}

    data = sect("data")
    if data.get("ok") is not True or "failed" not in data:
        return False
    for name, keys in (("lookthrough", ("ok", "missing")),
                       ("realtime", ("ok", "failed", "degraded"))):
        sec = sect(name)
        if any(k not in sec for k in keys):
            return False
    return True


def publish_gate(now: datetime | None = None, universe=None, extra=()) -> dict:
    """飞书发布资格门禁（V4-A，2026-09-17；方案 A 整批裁决，审计侧仅 WARN 呈现）。

    回答的问题：**当日逐基金证据链此刻是否干净**。任一只带降级项 ⇒ 整批不推
    （缺一只的报告本身就是残缺，推半份更危险）。

    - 证据 = 当日已落盘 run_manifest（只取当日；剔除 ``notification`` 节与
      GATE_EXCLUDED_REASONS）+ ``extra``（本次运行的内存证据：post 推送时本次
      清单尚未落盘，不带上它就只审了上午的旧账）；
    论域 = holdings.json 中 shares>0 的基金（清单**不得**写代码：run_manifest
    目录会被跟踪，写进去等于泄露持仓，违反 P0-2）；
    - 无当日证据 ⇒ ok=False（fail-closed；run.py 恒传本次 extra，正常链路不触发）；
    - 本函数**只回答不执行**：拦截在 run.py 推送前置，呈现位 check_publish_gate。
    """
    now = now or datetime.now()
    files = sorted(RUN_MANIFEST_DIR.glob(f"run_manifest_{now:%Y%m%d}_*.json")) \
        if RUN_MANIFEST_DIR.is_dir() else []
    runs: list[tuple[list[str], dict]] = []
    for p in files:
        m = load_json(p)
        if not m:
            continue
        m = {k: v for k, v in m.items() if k != "notification"}
        runs.append(([str(x) for x in (m.get("degraded_reasons") or [])], m))
    for reasons, structured in (extra or ()): 
        runs.append(([str(x) for x in (reasons or [])], dict(structured or {})))

    uni: set[str] = {str(c) for c in (universe or ())} | set(held_fund_codes())
    pool = fund_pool()      # V4.1 ④：清单别名基准表（反查一次，全程复用）

    run_level: list[str] = []
    per_fund_union: dict[str, list[str]] = {}
    last_assessed: dict[str, list[str]] | None = None
    n_assess_runs = 0
    for reasons, m in runs:
        kept = [r for r in reasons
                if str(r).partition(":")[0] not in GATE_EXCLUDED_REASONS]
        rl, pf = attribute_degradations(kept, m, uni, pool)
        run_level += rl
        for c, rs in pf.items():
            per_fund_union.setdefault(c, []).extend(rs)
        if fund_evidence_complete(m):
            n_assess_runs += 1
            last_assessed = pf          # 逐基金 last-run-wins（覆盖上一轮可恢复问题）
    # 运行级（市场背景/盘点互除等）全天粘性；逐基金若从未有完整评估轮⇒安并集
    per_fund = last_assessed if last_assessed is not None else per_fund_union
    superseded = {c: sorted(set(per_fund_union[c]) - set(per_fund.get(c, ())))
                  for c in per_fund_union if per_fund is not per_fund_union}
    superseded = {c: v for c, v in superseded.items() if v}

    contaminated = {}
    for c in sorted(uni):
        hits = sorted(set(run_level) | set(per_fund.get(c, ())))
        if hits:
            contaminated[c] = hits
    eligible = [c for c in sorted(uni) if c not in contaminated]
    ok = bool(uni) and bool(runs) and not contaminated

    if not uni:
        reason = "empty_universe"
    elif not runs:
        reason = "no_evidence_today"
    elif contaminated:
        reason = "contaminated_funds"
    else:
        reason = None
    if ok:
        detail = (f"当日 {len(runs)} 次证据运行（{n_assess_runs} 次完整评估），"
                  f"逐基金以最后一次为准 → {len(eligible)}/{len(uni)} 只干净 → 可发布")
    else:
        parts = []
        if run_level:
            # 报告含全池轮动/市场广度，池内任一全局证据缺位 ⇒ 整份残缺
            parts.append(f"全局证据缺位 {len(set(run_level))} 项"
                         f"（{sorted(set(run_level))[0][:44]}）")
        self_dirty = {c: sorted(set(per_fund.get(c, ())))
                      for c in contaminated if per_fund.get(c)}
        if self_dirty:
            worst = "; ".join(f"{c} 自身 {','.join(v)[:40]}"
                              for c, v in sorted(self_dirty.items())[:3])
            more = f" 等{len(self_dirty)}只" if len(self_dirty) > 3 else ""
            parts.append(f"持仓基金不干净：{worst}{more}")
        if not uni:
            parts.append("论域为空（holdings 不可读或无 shares>0 持仓）")
        elif not runs:
            parts.append("当日无任何证据运行（无清单且无内存证据）")
        detail = f"拦截({reason}) " + "；".join(parts)

    return {"ok": ok, "reason": reason, "date": f"{now:%Y-%m-%d}",
            "n_runs": len(runs), "universe": sorted(uni), "eligible": eligible,
            "contaminated": {c: contaminated[c] for c in sorted(contaminated)},
            "run_level": sorted(set(run_level)),
            "superseded": superseded,      # 早轮脏、末轮已恢复（仅观测，不影响裁决）
            "n_assess_runs": n_assess_runs,
            "excluded": sorted(GATE_EXCLUDED_REASONS) + ["manifest.notification"],
            "detail": detail}


def fund_alias(base) -> dict:
    """基准表 → {代码: 位置别名 F1/F2…}（按代码升序编号，位置即身份）。"""
    return {c: f"F{i + 1}" for i, c in enumerate(sorted({str(x) for x in (base or [])}))}


def mask_fund_codes(codes, base=None):
    """基金代码 → 位置别名（F1/F2…）。

    动因：``audit_current.json`` 是入库产物，而发布论域由 holdings.json 的
    shares>0 推出——直接写代码等于泄露「实际持有哪几只」，违反 P0-2 的
    持仓隔离口径。别名保留污染结构（哪只脏、脏在哪）却不暴露身份；
    真实明细只进控制台（本地）。

    ``base``（V4.1 ④ 加）：别名基准表。给定时按**基准表升序位**编号——同一代码在
    任何字段、任何清单里都是同一个 F 号，且可由基准表反查（运行清单掩码走这条）；
    不给定时退化为「按本列表内位置编号」的旧口径（audit_current.json 单列表沿用）。
    """
    if base is None:
        return fund_alias(codes)
    full = fund_alias(base)
    return {str(c): full[str(c)] for c in codes if str(c) in full}


def fund_pool() -> list[str]:
    """``config.fund_pool``——运行清单别名基准表（缺失/不可读 ⇒ 空表）。"""
    return [str(x) for x in (_config().get("fund_pool") or [])]


def fund_pool_fingerprint(pool) -> str:
    """基准表指纹（8 位 hex）：别名只对同一张基准表有意义，落盘留指纹供复核。"""
    joined = ",".join(sorted(str(x) for x in (pool or [])))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:8]


FUND_REF_RE = re.compile(r"F\d+\Z")
"""位置别名词法（``F1``/``F12``）。与 ``_CODE_RE`` 一起界定「基金 token」。"""


def _mask_detail(detail: str, alias: dict) -> str:
    for code, name in sorted(alias.items(), key=lambda kv: -len(kv[0])):
        detail = detail.replace(code, name)
    return detail


def mask_fund_tokens(tokens, alias) -> list[str]:
    """逐 token 掩码（token 级，不做子串替换——避免误伤时间戳等其它数字）。"""
    return [alias.get(str(t), str(t)) for t in (tokens or [])]


def mask_fund_text(text, alias) -> str:
    """``prefix:code,code`` 的代码段掩码；前缀原样保留（门禁靠前缀归类）。"""
    prefix, sep, suffix = str(text).partition(":")
    if not sep:
        return str(text)
    return prefix + sep + ",".join(alias.get(t, t) for t in suffix.split(","))


MANIFEST_FUND_FIELDS = tuple(GATE_FUND_KEYED.values()) + (("data", "status_unknown"),)
"""清单里一切**逐基金代码列表**字段（掩码与泄漏自检的唯一清单）。

= ``GATE_FUND_KEYED`` 覆盖的五处 + ``data.status_unknown``（V4.2：申购/赎回状态未知的
基金；它不是降级项、不进发布门禁，但同样是逐基金代码列表，落盘必须掩码）。
与 ``GATE_FUND_KEYED`` 分工：那张表管「降级项能否逐基金归因」，本表管「哪些字段带代码」。
"""


def mask_manifest_funds(payload: dict, pool) -> dict:
    """运行清单 → 掩码副本：逐基金字段与降级项代码段换成位置别名（V4.1 ④）。

    动因：``output/run_manifest/`` 随仓库跟踪，而逐基金降级码 ⊆ ``config.fund_pool``；
    ``run.py`` 的注释写着「清单不得写代码」，但 ``data.failed`` / ``data.fallback``
    一直是真实 6 位码——契约与产物自相矛盾。掩码基准固定为基金池升序位，门禁侧
    用同一张表反查（``resolve_fund_refs``），逐基金归因能力不变。
    """
    alias = fund_alias(pool)
    out = copy.deepcopy(payload)
    for section, key in MANIFEST_FUND_FIELDS:
        sec = out.get(section)
        if isinstance(sec, dict) and isinstance(sec.get(key), list):
            sec[key] = mask_fund_tokens(sec[key], alias)
    reasons = out.get("degraded_reasons")
    if isinstance(reasons, list):
        out["degraded_reasons"] = [
            mask_fund_text(r, alias) if str(r).partition(":")[0] in GATE_FUND_KEYED else r
            for r in reasons]
    out["fund_refs"] = {"scheme": "F<n> = config.fund_pool 升序第 n 位",
                        "n_pool": len(alias),
                        "pool_sha256_8": fund_pool_fingerprint(pool)}
    return out


def assert_no_fund_codes(payload) -> None:
    """掩码后硬校验：逐基金字段与降级项后缀不得残留 6 位代码。

    残留意味着基准表与本次运行不一致（例如出现池外代码）。此处**宁可拒绝落盘**
    （run.py 转 DEGRADED，exit=2，监控看得见）也不写出泄露持仓的清单——与
    ``data_fingerprint._assert_acyclic`` 同一取舍：宁可炸，不产出坏证据。
    """
    bad: list[str] = []
    for section, key in MANIFEST_FUND_FIELDS:
        sec = payload.get(section) if isinstance(payload, dict) else None
        if isinstance(sec, dict) and isinstance(sec.get(key), list):
            bad += [str(t) for t in sec[key] if _CODE_RE.fullmatch(str(t))]
    for r in ((payload or {}).get("degraded_reasons") or []):
        prefix, sep, suffix = str(r).partition(":")
        if sep and prefix in GATE_FUND_KEYED:
            bad += [t for t in suffix.split(",") if _CODE_RE.fullmatch(t)]
    if bad:
        raise ValueError(f"清单掩码后仍残留基金代码 {sorted(set(bad))}"
                         "（P0-2：run_manifest 随仓库跟踪，不得写代码）")


def resolve_fund_refs(tokens, pool=None, fingerprint=None) -> list[str]:
    """清单侧 ``F<n>`` 别名 → 真实代码（``mask_manifest_funds`` 的逆，V4.1 ④）。

    - 6 位代码原样通过（09-18 及更早的旧清单是在档证据，不追改）；
    - ``F<n>`` 按基准表升序位反查（指纹不一致即视为不可解析）；
    - 解析不出来的 token 原样返回 ⇒ 由调用方的「不在论域」fail-closed 分支接住，
      不会把无法判读的别名悄悄当成干净。
    """
    inv: dict[str, str] = {}
    if pool and (fingerprint is None or fund_pool_fingerprint(pool) == fingerprint):
        inv = {n: c for c, n in fund_alias(pool).items()}
    return [inv.get(str(t), str(t)) for t in (tokens or [])]


def check_publish_gate(a: Audit, now: datetime | None = None) -> None:
    """P1-9（2026-09-17）：发布门禁在审计侧的呈现位——**只告警不裁决**。

    刻意不计入 FAIL：① 防自锁——门禁输入含推送排除项所防不住「审计 FAIL ⇒
    资格层 FAIL ⇒ 永远推不出去」的循环，WARN 保持 audit_health 只度量静态审计；
    ② 保住「0 FAIL」对账口径。真正拦截在执行侧（run.py 推送前置查 ok）。

    入库侧只留掩码后的结构（见 mask_fund_codes），真实代码只进控制台。

    `now`（2026-09-18 加）：透传给 publish_gate 供测例注入固定日；生产不传 = 墙钟。
    原缺失此参数导致测试与墙钟耦合——跨零点后「当日证据」glob 失配、
    clean-day 测例误判 WARN（2026-09-18 fast 全量实测抓到）。
    """
    g = publish_gate(now=now)
    # V4.1 ④：checks[] 会整段写进 audit_current.json（入库），故此处也只用掩码措辞；
    # 真实代码经 detail_real 走控制台（main 里的 PUBLISH_GATE 段）。
    masked = _mask_detail(g["detail"], mask_fund_codes(g["universe"]))
    a.meta["publish_gate"] = {"ok": g["ok"], "reason": g["reason"],
                              "n_universe": len(g["universe"]),
                              "eligible": len(g["eligible"]),
                              "contaminated": len(g["contaminated"]),
                              "detail": masked,
                              # 仅内存使用（控制台打印），不进 payload
                              "detail_real": g["detail"]}
    a.add("P1-9", "P1-证据链", "飞书发布资格（当日逐基金证据干净）",
          PASS if g["ok"] else WARN, masked)


# ---------------------------------------------------------------- P2 工程卫生

def check_atomic_write(a: Audit) -> None:
    src = BASE_DIR / "data_fingerprint.py"
    txt = src.read_text(encoding="utf-8") if src.is_file() else ""
    ok = bool(txt) and ".replace(" in txt and ".tmp" in txt
    a.add("P2-1", "P2-工程卫生", "manifest 原子写", PASS if ok else WARN,
          "" if ok else "未见 tmp + replace 原子写模式")


def check_tushare_https(a: Audit) -> None:
    hits = []
    for p in BASE_DIR.rglob("*.py"):
        if any(part in (".venv", ".venv-lab", "__pycache__", ".git", "backups")
               for part in p.parts):
            continue
        if p.resolve() == AUDIT_SELF:       # 本脚本含该字面量做检查，不自指误报
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for i, ln in enumerate(txt.splitlines(), 1):
            if "http://api.tushare.pro" in ln:
                hits.append(f"{p.relative_to(BASE_DIR)}:L{i}")
    a.add("P2-2", "P2-工程卫生", "Tushare 走 HTTPS", PASS if not hits else WARN,
          "；".join(hits) if hits else "")


def check_test_layering(a: Audit) -> None:
    cfg_present = any((BASE_DIR / n).is_file()
                      for n in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"))
    n_tests = len(list((BASE_DIR / "tests").glob("test_*.py"))) if (BASE_DIR / "tests").is_dir() else 0
    markers = False
    for n in ("pytest.ini", "pyproject.toml", "setup.cfg"):
        p = BASE_DIR / n
        if p.is_file():
            try:
                if "markers" in p.read_text(encoding="utf-8"):
                    markers = True
            except (OSError, UnicodeDecodeError):
                pass
    a.add("P2-3", "P2-工程卫生", "测试分层（fast/slow）",
          PASS if (cfg_present and markers) else WARN,
          f"测试文件 {n_tests} 个；pytest 配置 {'有' if cfg_present else '无'}；"
          f"markers {'有' if markers else '无'}")


def check_env_untracked(a: Audit) -> None:
    tracked, err = git_lines("ls-files", ".env")
    if tracked is None:
        a.add("P2-4", "P2-工程卫生", "密钥文件未入库", WARN, f"git 不可用：{err}")
        return
    hits = [t for t in tracked if t == ".env"]
    a.add("P2-4", "P2-工程卫生", "密钥文件未入库",
          PASS if not hits else FAIL, "；".join(hits))


# ---------------------------------------------------------------- 四层状态

def _config() -> dict:
    """读 config.json（独立成函数，便于测试注入，不与检查项耦合）。"""
    return load_json(BASE_DIR / "config.json") or {}


def resolve_model_promotion(active_model, models: dict, model_ready: bool) -> tuple[str, dict]:
    """生产资格里的 model_promotion：**只看 active model**（V4.1 ②，2026-09-18）。

    纯函数：不读盘、不看时间，(active_model, models, model_ready) 决定输出，可单测。

    修的死锁：旧实现把「registry 中所有 blocked 模型」当否决权，于是历史失败记录
    （v2 / v3）会永久阻止未来模型晋升——forecast_v4 即便 approved，只要 v2 还在
    registry，结论仍是 BLOCKED。历史 promotion 是**档案事实**，不是当前生产的否决权。

    fail-closed 三态：active_model 未声明 / 未登记 / promotion 非 approved
    （含 pending / blocked / 缺字段）⇒ 一律 BLOCKED。宁可错杀，不可放行。

    返回 (status, basis)：basis 同时给出 active 与 historical 两组事实，供审计与
    报告分别呈现——历史 blocked 不再参与裁决，但必须可见，不得借修复之名抹掉痕迹。
    """
    active = str(active_model or "").strip()
    blocked_all = sorted(n for n, e in (models or {}).items()
                         if (e.get("promotion") or {}).get("status") == "blocked")
    historical_blocked = [n for n in blocked_all if n != active]

    if not active:
        status, reason, active_status = "BLOCKED", "active_model_missing", None
    elif active not in (models or {}):
        status, reason, active_status = "BLOCKED", "active_model_not_registered", None
    else:
        active_status = ((models[active].get("promotion") or {})
                         .get("status"))
        if not model_ready:
            status, reason = "BLOCKED", "model_ready_false"
        elif active_status == "approved":
            status, reason = "APPROVED", "ok"
        else:
            status, reason = "BLOCKED", f"active_promotion_{active_status or 'missing'}"
    return status, {
        "active_model": active or None,
        "active_promotion": active_status,
        "reason": reason,
        "historical_blocked": historical_blocked,
        "all_blocked": blocked_all,      # 旧字段兼容：全量 blocked 仍可得
    }


def derive_states(a: Audit) -> dict:
    """把「审计健康」与「生产资格」拆成四层独立状态（V4-A，2026-09-17）。

    关键不变式：``production_status == READY`` 要求 model_ready、history_validated
    与审计健康**同时**达标；任一不满足即 BLOCKED。因此「0 FAIL」永远不能单独
    推出「生产可用」——这正是旧版 ``PRODUCTION: NOT BLOCKED`` 的错误来源。
    """
    n = a.counts()
    if n[FAIL]:
        audit_health = "FAIL"
    elif n[WARN]:
        audit_health = "PASS_WITH_WARNINGS"
    else:
        audit_health = "PASS"

    cfg = _config()
    fc = cfg.get("forecast") or {}
    model_ready = bool(fc.get("model_ready", False))
    gates = (cfg.get("decision") or {}).get("gates") or {}
    hv = gates.get("history_validated")

    # V4.1 ②（2026-09-18）：生产资格只看 **active model**，不看历史模型。
    # 旧实现取「registry 里所有 blocked 的模型」，于是 v2/v3 这类失败的历史记录
    # 会永久阻止未来任何模型晋升——forecast_v4 即便 approved，只要 v2 还留在
    # registry，model_promotion 仍是 BLOCKED。历史模型的 promotion 是档案事实，
    # 不是当前生产的否决权。
    # active 由 config.forecast.active_model 显式声明（不靠 MODEL_VERSION 字符串
    # 推断：audit 侧保持 stdlib-only，不 import core.forecast_engine）。
    promotion, basis = resolve_model_promotion(fc.get("active_model"),
                                               _models(), model_ready)
    model_promotion = "APPROVED" if promotion == "APPROVED" else "BLOCKED"

    if hv is True:
        action_enable = "ENABLED"
    elif hv is False:
        action_enable = "HOLD_ONLY"      # 门禁存在且明确锁死 ⇒ 只出倾向，动作恒 HOLD
    else:
        action_enable = "BLOCKED"        # 门禁缺失/不可判 ⇒ 更严

    if (model_promotion == "APPROVED" and hv is True and audit_health != "FAIL"):
        production_status = "READY"
    else:
        production_status = "BLOCKED"

    return {
        "audit_health": audit_health,
        "model_promotion": model_promotion,
        "action_enable": action_enable,
        "production_status": production_status,
        "inputs": {"model_ready": model_ready, "history_validated": hv,
                   # 兼容旧字段：blocked_models 仍是「全量 blocked」（语义不变），
                   # 新增字段才表达 active/historical 拆分后的裁决依据。
                   "blocked_models": basis["all_blocked"],
                   "active_model": basis["active_model"],
                   "active_promotion": basis["active_promotion"],
                   "promotion_basis": basis["reason"],
                   "historical_blocked_models": basis["historical_blocked"]},
    }


# ---------------------------------------------------------------- 审计留档

def audit_payload(a: Audit, n: dict, st: dict) -> dict:
    """审计 JSON 正文（单一来源，current 与 history 两份内容完全一致）。"""
    rm = a.meta.get("run_manifest") or {}
    return {
        "schema_version": "2.0",
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "project": str(BASE_DIR),
        "summary": n,
        "audit_health": st["audit_health"],
        "model_promotion": st["model_promotion"],
        "action_enable": st["action_enable"],
        "production_status": st["production_status"],
        "inputs": st["inputs"],
        # 发布资格（P1-9）：check_publish_gate 算一次，这里复用不双算；入库只存
        # 计数与掩码措辞（真实持仓代码不得进仓库，见 mask_fund_codes）。
        "publish_gate": ({k: v for k, v in a.meta["publish_gate"].items()
                         if k != "detail_real"} if a.meta.get("publish_gate") else None),
        # V4.2（2026-09-18）：current 指针必须自证「描述的证据截至哪一天」。
        # 旧版 generated_at 只说明本文件何时生成——文件可能天天重写而证据停在上一交易日。
        "evidence_as_of": rm.get("as_of"),
        "evidence_expected_as_of": rm.get("expected_as_of"),
        "evidence_stale": bool(rm.get("stale")),
        "evidence_latest_run": rm.get("latest"),
        # 兼容层（V4 不做 breaking change）：旧字段语义原样保留——只反映
        # 审计 FAIL 数，**不表达生产资格**；新的生产资格请读 production_status。
        "production": "BLOCKED" if n[FAIL] else "NOT BLOCKED",
        "legacy_note": "'production' 为旧字段（仅反映审计 FAIL 数，不代表生产"
                       "资格）；请以 production_status 为准。",
        "checks": [c.__dict__ for c in a.sorted_checks()],
    }


def write_audit_json(a: Audit, n: dict, st: dict, out: Path | None = None,
                     history_dir: Path | None = None) -> tuple[Path, Path]:
    """写「当前状态指针」+ 「只增不删的历史证据」（V4-A，2026-09-17）。

    目录约定：

        output/audit_current.json               ← 唯一当前状态（所有人只看这份）
        output/audit_history/audit_<ts>.json    ← 历史证据，不删不改

    动因：旧仓只有 ``output/audit_report_20260916.json`` 一份历史报告，与新代码
    的当前结论不一致，却没有任何指针说明谁是「现在」。容易出现「模型 A 看旧
    报告、模型 B 跑 audit、模型 C 看 README」三份状态并存。
    """
    out = out or AUDIT_CURRENT
    hist = history_dir or (out.parent / "audit_history")
    payload = audit_payload(a, n, st)
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, out)

    hist.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    snap = hist / f"audit_{stamp}.json"
    snap.write_text(text, encoding="utf-8")
    return out, snap


def refresh_current() -> tuple[Path, Path]:
    """重跑审计并刷新「当前指针」（V4.2，2026-09-18）。

    动因：README 写「所有人只看 audit_current.json」，但该文件只在人手动跑审计时才
    更新——实况里它曾停在上一交易日，与「current」这个名字直接矛盾。run.py 在 post
    收尾调用本函数，于是指针的时效由证据产出动作自己维持。

    返回（current, history 快照）路径；调用方负责决定失败如何处理（run.py 只记
    ``audit_refresh`` 不据此降级：指针是派生视图，过期与否已由 ``evidence_stale``
    自证，不需要把生产运行拖成 DEGRADED）。
    """
    a = run_audit()
    return write_audit_json(a, a.counts(), derive_states(a))


# ---------------------------------------------------------------- 主流程

def run_audit() -> Audit:
    a = Audit()
    check_pit(a)
    check_holdings_privacy(a)
    check_manifest_resolvable(a)
    check_manifest_freshness(a)
    check_manifest_scope_acyclic(a)
    check_registry_hash(a)
    check_registry_paths(a)
    check_validation_binding(a)
    check_promotion_consistency(a)
    check_history_validated(a)
    check_shadow_channels(a)
    check_legacy_archived(a)
    check_run_manifest(a)
    check_publish_gate(a)
    check_approval_binding(a)
    check_worktree_clean(a)
    check_ssot_git_tip(a)
    check_atomic_write(a)
    check_tushare_https(a)
    check_test_layering(a)
    check_env_untracked(a)
    return a


def main() -> int:
    ap = argparse.ArgumentParser(description="V4 项目审计契约（只读 / 零网络）")
    ap.add_argument("--json", default=str(AUDIT_CURRENT.relative_to(BASE_DIR)),
                    help="当前状态 JSON 路径（相对项目根；同时向 output/audit_history/ 留档）")
    ap.add_argument("--no-write", action="store_true",
                    help="纯只读模式：不写 current / history（供只读复核）")
    args = ap.parse_args()

    try:
        a = run_audit()
    except Exception as exc:                       # noqa: BLE001
        print(f"[error] 审计脚本异常：{type(exc).__name__}: {exc}")
        return 1

    print("== V4 审计契约 ==")
    print(f"项目：{BASE_DIR}")
    axis_now = None
    for c in a.sorted_checks():
        if c.axis != axis_now:
            axis_now = c.axis
            print(f"\n-- {axis_now} --")
        line = f"[{c.status:4}] {c.cid} {c.title}"
        if c.detail:
            line += f" —— {c.detail}"
        print(line)

    n = a.counts()
    st = derive_states(a)
    print(f"\nRESULT: {n[PASS]} PASS / {n[FAIL]} FAIL / {n[WARN]} WARN")
    print(f"AUDIT_HEALTH:      {st['audit_health']}")
    print(f"MODEL_PROMOTION:   {st['model_promotion']}"
          f"（model_ready={st['inputs']['model_ready']}）")
    print(f"ACTION_ENABLE:     {st['action_enable']}"
          f"（history_validated={st['inputs']['history_validated']}）")
    print(f"PRODUCTION_STATUS: {st['production_status']}")
    if st["production_status"] == "BLOCKED":
        print("  ↳ 审计健康与生产资格是两回事：本次审计可读、可信，"
              "但模型未获批/动作层锁死 ⇒ 不得据此启用动作。")
    g = a.meta.get("publish_gate") or {}
    print(f"PUBLISH_GATE:      {'PASS' if g.get('ok') else 'BLOCKED'}"
          f"（clean {g.get('eligible', 0)}/{g.get('n_universe', 0)} 只；"
          f"拦截器在 run.py 推送前置，此处仅呈现）")
    if not g.get("ok"):
        # 控制台是本地输出，可给真实代码；入库侧 audit_*.json 只存计数/掩码
        print(f"  ↳ {g.get('detail_real') or g.get('detail', '')}")

    if args.json and not args.no_write:
        out, snap = write_audit_json(a, n, st, BASE_DIR / args.json)
        print(f"[json] current → {out}")
        print(f"[json] history → {snap}")
    elif args.no_write:
        print("[json] --no-write：未落盘（本次仅内存结论）")

    return 2 if n[FAIL] else 0


if __name__ == "__main__":
    raise SystemExit(main())
