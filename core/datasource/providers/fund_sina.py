"""新浪基金净值备源（openapi CaihuiFundInfoService.getNav）——升自已验证的实验脚本
`experiments/sina_nav_redundant/pull_sina_nav.py`（V4 步 4，2026-09-17）。

通路实证（2026-09-08）：002112 total_num=2637 与东财缓存条数完全一致、样本日数值
逐位吻合；此后每日 16:00 后置校验持续 4/4 mismatch=0。新浪无东财那种 IP 级频控，
但仍保守限速（页间隔 SLEEP）。

与实验脚本的差异（升级点，均为架构要求非行为变更）：
- 传输改走 `core/netutil`（providers 禁止直接 import urllib，见 providers/__init__ 约定；
  netutil 的 IPv4 优先 + 瞬断重试覆盖旧脚本自带重试的语义）；
- 不落盘、不交叉校验（那是脚本侧编排职责），provider 只负责「取一份升序净值」；
- 失败转 FetchResult：网络故障 `network:` 前缀；拉出 0 条 / 行数可疑（<MIN_ROWS）
  按 `data:` 处理（不计健康度降级）。
- **D8-03（面 8 审计，2026-10-03）**：P0-2 同形态残留修复——分页中途空页旧版被当
  正常终止，截断净值以 fresh 名义静默写缓存。现首页防御式解析 `total_num`，
  循环结束后校验去重行数：明显少于 total_num → `data:行数不符 got=X/total=Y`
  fail-closed（由东财链 fresh 或缓存降级接手）。total_num 解析不到（路径未变/
  上游改版）时退化为 MIN_ROWS 下限防线，不引入新假红。
  容差 max(2, 0.5%)：分页边界重叠去重/拉取期间新数据移位/坏行过滤会让
  去重后行数略少于 total_num；真截断通常缺数百行，不会漏判。

payload 形状与东财源一致：`{"code", "navs": [[date, nav], ...] 升序, "source"}`。
新浪接口无基金名称字段 → payload 不带 `name`，由装配点回退（缓存已有 name 时复用）。
"""
from __future__ import annotations

import json
import time

from ..base import DATA, FetchResult, classify_exc
from ... import netutil

API = ("https://stock.finance.sina.com.cn/fundInfo/api/openapi.php/"
       "CaihuiFundInfoService.getNav")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
REFERER = "https://finance.sina.com.cn/"
PAGE_NUM = 100
DATEFROM = "20150101"
SLEEP = 0.8          # 页间隔；对齐实验脚本实测参数
MAX_PAGES = 60       # 60×100=6000 条，覆盖最长的 002112（2637）绰绰有余
MIN_ROWS = 50        # 对齐实验脚本「suspiciously few rows」防线


def _extract_total_num(js: dict) -> int:
    """防御式读取 total_num（D8-03）。002112 实测 total_num=2637（实验脚本
    docstring），但原始响应未落档、确切路径未核实 → 多候选位置搜索；
    解析不到返回 0（校验退化为 MIN_ROWS 下限，不假红）。"""
    for path in (("result", "data", "total_num"),
                 ("result", "total_num"),
                 ("result", "data", "total"),
                 ("total_num",)):
        node: object = js
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        try:
            n = int(node)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return 0


class SinaFundNavProvider:
    name = "sina"
    category = "fund_nav"
    priority = 1            # 链上第二环：东财失败时接手（本次重构的验收点）
    timeout_s = 15.0

    def fetch(self, *, code: str, **_ignored) -> FetchResult:
        started = time.monotonic()
        rows: dict[str, float] = {}
        page, err = 1, None
        total_num = 0            # D8-03：首页解析，循环后校验行数完整性
        try:
            while page <= MAX_PAGES:
                url = (f"{API}?symbol={code}&datefrom={DATEFROM}"
                       f"&dateto={time.strftime('%Y%m%d')}&num={PAGE_NUM}"
                       f"&page={page}&format=json")
                js = netutil.http_get_json(
                    url, headers={"User-Agent": UA, "Referer": REFERER},
                    timeout=int(self.timeout_s))
                if page == 1:
                    total_num = _extract_total_num(js)
                data = (((js.get("result") or {}).get("data") or {}).get("data") or [])
                if not data:
                    break
                for it in data:
                    d = str(it.get("fbrq", ""))[:10]
                    try:
                        nav = float(it.get("jjjz"))
                    except (TypeError, ValueError):
                        continue
                    if len(d) == 10:
                        rows[d] = nav        # 分页边界重叠天然去重（后页覆盖前页）
                if len(data) < PAGE_NUM:
                    break
                page += 1
                time.sleep(SLEEP)
        except Exception as exc:  # noqa: BLE001 —— 契约：失败转 FetchResult，不上抛
            err = exc
        latency = int((time.monotonic() - started) * 1000)
        if err is not None:
            return FetchResult(ok=False, source=self.name,
                               error=f"{classify_exc(err)}{type(err).__name__}: {err}",
                               latency_ms=latency)
        navs = [[d, rows[d]] for d in sorted(rows)]
        if len(navs) < MIN_ROWS:
            return FetchResult(ok=False, source=self.name,
                               error=f"{DATA}sina.{code}: 行数可疑 {len(navs)}",
                               latency_ms=latency)
        # D8-03 fail-closed：中途空页截断不得以 fresh 名义写缓存。
        # 容差 max(2, 0.5%)：边界去重/新数据移位/坏行过滤的合法少行不误判；
        # 真截断（缺整页百行级）必判 fail，由链上下一环/缓存降级接手。
        if total_num and len(navs) < total_num - max(2, int(total_num * 0.005)):
            return FetchResult(ok=False, source=self.name,
                               error=(f"{DATA}sina.{code}: 行数不符 "
                                      f"got={len(navs)}/total={total_num}（疑分页截断）"),
                               latency_ms=latency)
        return FetchResult(ok=True, source=self.name, latency_ms=latency,
                           payload={"code": code, "navs": navs, "source": self.name})
