"""冻结输入「三件套」校验工具（2026-09-18，C2 特征非不变性 P0 的收口件）。

背景（`output/forecast_lab_prereg_C2prod_decomp_20260918.md` + 笔记
《C2生产轨三拆与特征非不变性-20260918》§3.2）：

同一份 `samples_frozen_20260910.jsonl`、同一份代码，T+5 WF pooled 由 09-08 的
+0.089 翻为 −0.079；D1 对照实验（worktree 检出修复前代码）已排除代码因素，
根因是**特征不是时点不变的**：标签 0% 漂移，而依赖穿透个股 K 线的特征
（`est_chg` 81.7% / `concentration` 81.6%）大幅漂移，源头是个股 K 线缓存
TTL 重取后**追溯改写历史 close**（240 只里 186 只 sha 变了）。

⇒ 本项目任何「重跑历史回测对数字」的动作，必须同时锁
   ① 代码 commit ② 样本冻结件 sha256 ③ K 线聚合指纹
三件才可复现。本工具把这三件的校验**工具化**，避免定时任务里由 agent
自由发挥（09-17/09-18 两次事故的共同教训）。

## 两道闸门（刻意分开，勿合并）

- **G-A 内部完整性（硬中止，exit 3）**：`samples_frozen_*.jsonl` 的 sha256
  必须等于其 meta 侧车记录的 `sha256`。不等 ⇒ 样本被改过，**立即中止**，
  不得继续跑任何复算。
- **G-B 跨版本可比性（软标记，不改 exit 码）**：把**当前** K 线缓存重算指纹，
  与留档的 `kline_fingerprint_*.json` 聚合 sha 比。不一致**不是**运行错误——
  它是「本轮数字与锚定历史轮次不可比」的**结论**，必须原样写进报告，
  且该轮不得再引用历史绝对值作门槛。

为什么分开：把 G-B 做成硬中止会让任务在正常漂移下彻底跑不动；把它忽略
（旧 `run_m0_power.py` 的 `仅记录，不作前提`）则等于让不可比数字蒙混过关。
本工具取中间：**中止 vs 标记**，由闸门性质决定。

## 换行陷阱（必读）

`freeze_samples.py` 用 `Path.write_text` 落盘，Windows 下把 `\n` 翻成 CRLF，
但它记录的 sha256 是对 **LF 归一后**内容算的。所以：

- 校验一律用 **LF 归一**内容哈希（本工具默认行为）；
- **不要**用 `Get-FileHash` / `certutil` 直接校——那会得到 CRLF 原始字节哈希
  （实测 09-10 件：原始 `0d59f663…` vs meta `be8e55f0…`），产生**假失败**
  并误中止整个重估任务。
- 原始字节哈希仅作记录输出（`raw_bytes_sha256`），不参与判定。

## V4.3 扩展（2026-09-20，GPT 评审 P0-2/P0-3）

- **G-A 增 stock_data_failures 闸门**：meta 记录的 stock K-line 失败非空 ⇒
  判失败。冻结侧数据质量门禁保证该字段要么缺（旧件）要么为空；旧件
  （无字段）= 不判，字段存在且非空 = 失败。
- **G-B scope-aware 重算**：留档 KFP 带 stock_codes_scope/cutoff（V4.3 冻结
  件）时按同口径重算；09-20 前锚点件（无 scope 字段）仍无参全量口径。
  混口径会改变 canonical 串 ⇒ 假漂移。
- **判词**：G-A 失败区分 MISMATCH（sha/行数/降级征兆被改）与
  SNAPSHOT_INVALID（结构性缺陷：stock_data_failures）；均 exit 3，必须中止。

## V4.3.1 加固（2026-09-21，外部复审 ①/②）

- **KFP 留档解析单一真源**（`load_kfp_record`）：CLI（cmd_verify）与库
  （frozen_dataset.verify_comparability）共用同一份侧车发现 + meta 回退逻辑，
  不再两处各写一遍（上轮两处 fallback 已经漂移：库带 cutoff/scope=None，
  CLI 连 cutoff 都不带 ⇒ 同件两判）。
- **scoped 侧车断链 fail-closed 到 UNKNOWN**：meta.kline_fingerprint_scope
  表明是 V4.3 scoped 件而 `kline_fingerprint_*.json` 侧车缺失 ⇒ 拒算。
  meta 只记 stock_n/cutoff，恢复不了 universe 码表；混口径重算要么假
  DRIFTED、要么掩盖真漂移（正是 09-18 病灶形态），宁可 UNKNOWN。
- **重算 unreadable 非空 ⇒ INCOMPLETE**：当前缓存缺码/坏码（unreadable）
  ⇒ 聚合指纹有空洞，比对**不成立**：既不能声称 SAME 也不能归因 DRIFTED。
  INCOMPLETE 与 DRIFTED 同级软标记（不改 exit 码），但必须原样写进报告。

## 用法

  # 单件校验（默认：LF 归一 sha + 同目录兄弟 meta/指纹回退）
  python -X utf8 freeze_verify_tool.py verify \
      --jsonl forecast_outputs/samples_frozen_20260910.jsonl

  # 显式指定侧车与锚点指纹，并落一份三件套 JSON 供报告引用
  python -X utf8 freeze_verify_tool.py verify \
      --jsonl forecast_outputs/samples_frozen_20260910.jsonl \
      --meta  forecast_outputs/samples_frozen_20260910.meta.json \
      --kfp   forecast_outputs/kline_fingerprint_20260910.json \
      --triple-out output/freeze_triple_20260926.json

退出码：0 = PASS（G-A 过）；3 = MISMATCH（G-A 失败，必须中止）；4 = MISSING
（样本或 meta 缺失，无法判定，同样必须中止）。

铁律兼容：零网络；只读 `data/` 与 `forecast_outputs/`，不写任何生产文件
（`--triple-out` 由调用方显式指定路径）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "experiments" / "forecast_lab"))

EXIT_PASS = 0
EXIT_MISMATCH = 3
EXIT_MISSING = 4


# ------------------------------------------------------------------ 基础件
def lf_normalized_sha256(path: Path) -> str:
    """对 LF 归一后的内容算 sha256 —— 与 freeze_samples.py 的落盘口径一致。

    必须按字节做 CRLF→LF，不能先 decode 再 encode：样本行含中文，
    经文本模式往返会引入平台相关的隐式转换。
    """
    raw = path.read_bytes()
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def raw_bytes_sha256(path: Path) -> str:
    """文件原始字节 sha256（仅记录，不参与判定；见模块 docstring 换行陷阱）。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_sidecar(jsonl: Path, explicit: Path | None, suffix: str) -> Path | None:
    """找侧车文件：显式路径优先，否则按样本名推 `<stem>.meta.json` / 兄弟指纹。

    09-10 那份 meta 生成于 09:52，而 K 线指纹接入的提交是 09-10 13:13，
    所以它**没有** `kline_fingerprint_*` 字段 —— 这是历史断链，不是损坏。
    因此指纹文件一律按日期标签回退到同目录兄弟文件，不依赖 meta 字段。
    """
    if explicit is not None:
        return explicit if explicit.exists() else None
    if suffix == "meta":
        cand = jsonl.with_suffix(".meta.json")
        return cand if cand.exists() else None
    # suffix == "kfp"：samples_frozen_YYYYMMDD.jsonl -> kline_fingerprint_YYYYMMDD.json
    tag = jsonl.stem.replace("samples_frozen_", "")
    cand = jsonl.parent / f"kline_fingerprint_{tag}.json"
    return cand if cand.exists() else None


# ------------------------------------------------------------------ KFP 留档解析
def load_kfp_record(jsonl: Path,
                    meta: dict | None,
                    explicit: Path | None = None) -> tuple[dict, Path | None, str | None]:
    """解析冻结件的 KFP 留档记录（V4.3.1 单一真源：库与 CLI 共用）。

    返回 (kfp_rec, kfp_path, reject_reason)；reject_reason 非 None ⇒ 该件
    不可比、判 UNKNOWN（fail-closed），调用方不得再拿 kfp_rec 去重算比对。

    正常路径：侧车 `kline_fingerprint_<tag>.json` 存在 ⇒ 直接消费（含
    stock_codes_scope/cutoff 的 as-of + universe 口径字段）。

    断链回退（09-10 实况：meta 生成早于指纹接入）：侧车缺失时退 meta 的
    kline_fingerprint_sha256 / kline_fingerprint_file 字段。但 meta 的
    kline_fingerprint_scope（V4.3 起冻结件才有）只能恢复 cutoff、**恢复不了
    universe 码表**——scoped 件缺侧车却按退化口径重算，改变 canonical 串 ⇒
    要么假 DRIFTED、要么掩盖真漂移。故 scoped 件缺侧车 ⇒ reject（fail-closed
    到 UNKNOWN），不猜口径；未 scoped 的旧件（全量口径）仍按原路回退。
    """
    kfp_p = resolve_sidecar(jsonl, explicit, "kfp")
    if kfp_p is not None:
        return json.loads(kfp_p.read_text(encoding="utf-8")), kfp_p, None
    meta = meta or {}
    if meta.get("kline_fingerprint_scope"):
        return ({"aggregate_sha256": None, "file": None}, None,
                "V4.3 scoped 件缺 kline_fingerprint_*.json 侧车（meta 无法恢复 "
                "universe 码表，混口径重算不可信）——fail-closed 判 UNKNOWN，"
                "请补回侧车或重新冻结")
    return ({"aggregate_sha256": meta.get("kline_fingerprint_sha256"),
             "file": meta.get("kline_fingerprint_file"),
             "cutoff": (meta.get("kline_fingerprint_scope") or {}).get("cutoff"),
             "stock_codes_scope": None}, None, None)


def comparability_state(recorded: dict | None,
                        current: dict | None) -> tuple[str, str]:
    """G-B 状态判据（V4.3.1 单一真源）。返回 (state, detail)，
    state ∈ SAME / DRIFTED / INCOMPLETE / UNKNOWN。

    - UNKNOWN：任一侧缺聚合 sha（无留档 / 重算失败 / 留档被 reject）。
    - INCOMPLETE：重算侧 unreadable 非空——当前缓存缺码/坏码，聚合有空洞，
      与留档**不具可比性**：SAME 不可声称，DRIFTED 也不可归因（差值可能
      全部来自缓存空洞而非数据改写）。软标记，不拦截。
    - 其余按 sha 相等性判 SAME / DRIFTED。

    留档侧 unreadable 不参与判定：冻结侧门禁（P0-3）已保证 canonical 件的
    unreadable 为空（非空即 SNAPSHOT_INVALID 不发布），读到非空必是 quarantine
    留证件，不该走到这里。
    """
    rec_sha = str((recorded or {}).get("aggregate_sha256") or "")
    now = current or {}
    now_sha = str(now.get("aggregate_sha256") or "")
    if not rec_sha or not now_sha:
        return "UNKNOWN", "缺留档或重算聚合 sha"
    unreadable = now.get("unreadable") or []
    if unreadable:
        return ("INCOMPLETE",
                f"当前缓存缺/坏码 {len(unreadable)} 项，聚合指纹有空洞，比对不成立: "
                f"{unreadable[:3]}")
    if rec_sha == now_sha:
        return "SAME", ""
    return "DRIFTED", ""


def git_commit() -> str | None:
    """当前代码 commit（三件套第①件）。取不到就返回 None，不阻塞判定。"""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(BASE_DIR),
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def current_kline_fingerprint(recorded: dict | None = None) -> dict | None:
    """重算当前缓存指纹（V4.3 P0-2：scope-aware，与留档同口径）。

    旧口径不带参数，与 freeze_samples.py 逐字一致。V4.3 冻结件改用
    as-of + universe 口径（stock_codes + cutoff），混口径会改变 canonical
    串 ⇒ 假漂移。故：留档指纹带 scope 字段则按同口径重算；09-20 前锚点件
    （无 scope 字段）仍无参全量口径。include_sector 仍刻意不传（板块是
    面板口径，与冻结件不可比）。
    """
    try:
        from kline_fingerprint import build_fingerprint   # noqa: PLC0415
        if recorded:
            scope = recorded.get("stock_codes_scope")
            cutoff = recorded.get("cutoff")
            if scope and scope != "all":
                return build_fingerprint(stock_codes=list(scope), cutoff=cutoff)
            if cutoff:
                return build_fingerprint(cutoff=cutoff)
        return build_fingerprint()
    except Exception as exc:                              # noqa: BLE001
        print(f"[freeze] WARN: 当前 K 线指纹重算失败（G-B 跳过）：{exc}")
        return None


# ------------------------------------------------------------------ 主命令
def cmd_verify(args: argparse.Namespace) -> int:
    jsonl = Path(args.jsonl)
    if not jsonl.is_absolute() and not jsonl.exists():
        jsonl = BASE_DIR / jsonl
    print(f"[freeze] samples  : {jsonl}")

    if not jsonl.exists():
        # 刻意**不做**「同名文件兜底」：静默换成另一个文件校验会产出假 PASS，
        # 与 09-26 重估「钉死输入」的目的直接冲突。缺失即 fail-closed。
        print("[freeze] VERDICT  : MISSING —— 样本文件不存在（拒绝回退到同名文件）："
              f"{jsonl}")
        return EXIT_MISSING

    meta_p = resolve_sidecar(jsonl, Path(args.meta) if args.meta else None, "meta")
    explicit_kfp = Path(args.kfp) if args.kfp else None

    # ---- G-A：样本内部完整性（硬中止）----
    sha_lf = lf_normalized_sha256(jsonl)
    sha_raw = raw_bytes_sha256(jsonl)
    n_rows = sum(1 for ln in jsonl.read_text(encoding="utf-8").splitlines() if ln.strip())
    print(f"[freeze] rows     : {n_rows}")
    print(f"[freeze] sha256   : {sha_lf}  (LF 归一，判定口径)")
    print(f"[freeze] raw-bytes: {sha_raw}  (仅记录；CRLF 差异不判失败)")

    meta: dict | None = None
    if meta_p is None:
        print("[freeze] meta     : 缺失 —— 无法做 G-A 内部完整性判定")
        print("[freeze] VERDICT  : MISSING（按 fail-closed 处理，必须中止）")
        return EXIT_MISSING
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    rec = str(meta.get("sha256", ""))
    ga_ok = (rec == sha_lf)
    print(f"[freeze] meta     : {meta_p.name}  记录 sha256={rec or '(空)'}")
    print(f"[freeze] G-A 样本 sha 一致 : {'OK' if ga_ok else 'MISMATCH'}")
    if meta.get("n_samples") is not None and int(meta["n_samples"]) != n_rows:
        print(f"[freeze] G-A 行数 不一致 : meta={meta['n_samples']} 实际={n_rows}")
        ga_ok = False
    if meta.get("degraded_or_ratelimit_flags"):
        print(f"[freeze] G-A 冻结时带降级/频控征兆 : {meta['degraded_or_ratelimit_flags']}")
        ga_ok = False
    # V4.3 P0-3：冻结时记录的个股K线失败 ⇒ 快照结构性无效（数据质量门禁）。
    # 旧件无此字段 = 不判；字段存在且非空 = 失败。
    sdfs = meta.get("stock_data_failures")
    struct_invalid = bool(sdfs)
    if sdfs:
        print(f"[freeze] G-A 冻结时带个股K线失败（{len(sdfs)} 码，数据质量门禁）: {sdfs[:3]}")
        ga_ok = False

    # ---- G-B：跨版本可比性（软标记；V4.3.1 与库共用同一判据）----
    kfp_rec, kfp_p, reject = load_kfp_record(jsonl, meta, explicit_kfp)
    kfp_rec_sha = str(kfp_rec.get("aggregate_sha256") or "")
    if reject is not None:
        kfp_now = None
        print(f"[freeze] kfp 留档 : REJECT —— {reject}")
    else:
        kfp_now = current_kline_fingerprint(kfp_rec)   # V4.3 P0-2：与留档同口径重算
    kfp_now_sha = str((kfp_now or {}).get("aggregate_sha256") or "")
    gb_state, gb_detail = comparability_state(kfp_rec, kfp_now)
    print(f"[freeze] kfp 留档 : {kfp_rec_sha or '(无)'}  <- "
          f"{kfp_p.name if kfp_p else meta.get('kline_fingerprint_file') or '(无)'}")
    print(f"[freeze] kfp 当前 : {kfp_now_sha or '(未取到)'}")
    print(f"[freeze] G-B K线指纹 : {gb_state}"
          + (f"（{gb_detail}）" if gb_detail else "")
          + ("（不一致 ⇒ 本轮数字与锚定历史轮次**不可比**，"
             "报告必须原样记录且不得引用历史绝对值作门槛）"
             if gb_state in ("DRIFTED", "INCOMPLETE") else ""))

    triple = {
        "code_commit": git_commit(),
        "samples_file": jsonl.name,
        "samples_sha256_lf": sha_lf,
        "samples_sha256_raw_bytes": sha_raw,
        "samples_rows": n_rows,
        "meta_file": meta_p.name,
        "meta_sha256_recorded": rec,
        "kline_fingerprint_file": kfp_p.name if kfp_p else None,
        "kline_fingerprint_recorded": kfp_rec_sha or None,
        "kline_fingerprint_current": kfp_now_sha or None,
        "kline_fingerprint_state": gb_state,
        "kline_fingerprint_state_detail": gb_detail or None,
        "gate_internal": "PASS" if ga_ok else "MISMATCH",
        "gate_comparability": gb_state,
    }
    print(f"[freeze] triple   : code={triple['code_commit'] or 'unknown'} "
          f"samples_sha={sha_lf[:12]}… kfp={kfp_rec_sha[:12] or 'n/a'}…")
    if args.triple_out:
        out = Path(args.triple_out)
        if not out.is_absolute():
            out = BASE_DIR / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(triple, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[freeze] 三件套已落盘 : {out}")

    if not ga_ok:
        verdict = "SNAPSHOT_INVALID" if struct_invalid else "MISMATCH"
        print(f"[freeze] VERDICT  : {verdict} —— G-A 失败，必须中止，不得继续复算")
        return EXIT_MISMATCH
    print(f"[freeze] VERDICT  : PASS（G-A 过；可比性 = {gb_state}）")
    return EXIT_PASS


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("verify", help="校验冻结样本三件套（零网络、只读）")
    p.add_argument("--jsonl", required=True, help="samples_frozen_*.jsonl")
    p.add_argument("--meta", default=None, help="meta 侧车（默认同目录自动发现）")
    p.add_argument("--kfp", default=None, help="留档 K 线指纹（默认按日期标签发现）")
    p.add_argument("--triple-out", default=None, help="把三件套 JSON 写到该路径")
    p.set_defaults(fn=cmd_verify)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
