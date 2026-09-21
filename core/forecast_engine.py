"""v5 多周期条件分布预测引擎（Forecast Engine）。

定位（GPT-5.6Luna 评审落地，2026-08-26）：
- 预测是一级公民，不再是「动作的附属验证指标」。
- 给定截至 T 时刻的特征，输出未来 T+1/T+3/T+5 三个周期的：
      P(up) / P(flat) / P(down)    方向三分类概率
      E(return)                    预期收益（平方损失回归，条件均值）
      Q10 / Q50 / Q90              真条件分位（quantile loss HGBR，2026-08-30 v8；
                                     v7 曾以 ±1.282σ 正态近似过渡并如实标注）
      max_dd                       （v7 起移除——P2 真 MDD 标签就绪前不假装建模）
      confidence                    置信度
- 验证纪律：只输出经 backtest_forecast.py（TimeSeriesSplit + Brier/Calibration/RankIC
  + OOS）裁决过的预测。model_ready=false 时恒返回「未验证占位」，不假装预测。

设计原则（对齐项目铁律「有证据才上线」）：
1. 默认 scikit-learn 梯度提升（可选模型，架构上可替换任意学习器）。
2. TimeSeriesSplit 训练/验证，防前视；OOS 段单独留证。
3. 置信度由四部分合成：模型历史稳定性 × 数据完整度 × 持仓新鲜度 × 多周期一致性。
   与 v4 的固定阈值公式不同——v4 confidence 只是分值的单调映射，不表达预测质量。
4. account（仓位）不进市场预测特征——仓位高不该降低「基金上涨概率」，这是策略层(policy)的事。
"""
from __future__ import annotations

import json
import math
import pickle
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent.parent

# ---- 模型持久化闭环（2026-08-27 落地）----
# 背景：此前 run.py 每次 new ForecastEngine() 都是无权重空壳——38 项测试虽全过，
# 但线上推理恒走 _trained=False 占位分支（predict 恒返回全 0）。
# 闭环：train_forecast_model.py（fit+save）→ data/models/*.pkl → run.py 经
#   get_loaded_engine() 单例加载 → config.forecast.model_ready=true 才对外输出。
# 安全约定：只加载本仓库脚本自己 dump 的 pickle 文件，不要指向不受信来源。
MODELS_DIR = BASE_DIR / "data" / "models"
MODEL_VERSION = 3     # 特征集/输出协议结构版本：FEATURE_KEYS、horizon 结构或
                      # 分位数协议变更时 +1，版本不符的旧权重文件将被 load_models 拒绝加载
                      # v2（2026-08-30）：ReturnQuantileModel 换真分位数回归（quantile loss）
                      # + HorizonForecast 恢复真 q50，v1 pkl 全部作废重训

# 可选轻量依赖：sklearn 缺失时退回空预测（诚实占位）
try:
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
    from sklearn.model_selection import TimeSeriesSplit
    _SKLEARN = True
except Exception:  # pragma: no cover
    _SKLEARN = False


# ---- LOFO 跨基金泛化证据（2026-08-30，GPT 四审 P1-⑬）----
# backtest_lofo.py 产出 data/model_registry/lofo_evidence.json：
#   {"funds": {fund_code: {"stability": float, "verdict": str, "horizons": {...}}}}
# confidence 的 stability 因子消费它（逐基金，不再全局 0.85 硬编码）。
LOFO_EVIDENCE_PATH = BASE_DIR / "data" / "model_registry" / "lofo_evidence.json"


def _load_fund_evidence() -> dict:
    """加载 LOFO 证据；缺失/损坏返回空 dict（诚实降级：无证据 → 中性 0.5）。"""
    try:
        if LOFO_EVIDENCE_PATH.exists():
            data = json.loads(LOFO_EVIDENCE_PATH.read_text(encoding="utf-8"))
            return data.get("funds", {}) or {}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


@dataclass
class HorizonForecast:
    """单个周期的预测输出。"""
    horizon: int                     # T+1 / T+3 / T+5
    p_up: float
    p_flat: float
    p_down: float
    e_return: float
    q10: float
    q50: float                       # 真分位（quantile 回归；v7 曾移除假 q50，v8 恢复）
    q90: float
    confidence: float
    model_ready: bool = False        # False = 未经 backtest_forecast 验证，值不可信


@dataclass
class Forecast:
    t1: HorizonForecast
    t3: HorizonForecast
    t5: HorizonForecast
    state: str = ""                  # 轻量状态标签（UP/RANGE/DOWN 等，由状态层合成）
    path: str = ""                   # 最可能路径描述（多周期一致性推导）
    overall_confidence: float = 0.0
    meta: dict = field(default_factory=dict)


FEATURE_KEYS = [
    "est_chg",        # 当日重仓估算涨跌
    "est_sign",       # est_chg 方向 ±1/0
    "breadth",        # 重仓同向一致度 [-1,1]
    "concentration",  # 集中度 [0,1]
    "covered_pct",    # 覆盖度 %
    "composite",      # 缠论 composite
    "score",          # 三因子总分
    # 注（v5.2 实验已回退）：曾加 rel_str20（市场级相对强度，申万行业免费源探测失败退化），
    #   OOS T+1 +0.021→-0.005 翻负、T+3 恶化、T+5 减半 → 判定过拟合，已撤销。
    # 注（v5.1 实验已回退）：曾加入 mom1/mom5/mom20/vol20/dd60 时序特征，OOS 三周期
    # 全部恶化（T+1 -0.024 / T+3 -0.028 / T+5 -0.100）→ 判定为特征过拟合，已撤销。
]
# 实验对照基准（2026-08-29 P2）：experiment_state_feature.py 以此为 A 组基线，
# 变体组在其上追加状态特征；生产 FEATURE_KEYS 变更时同步维护此常量。
BASE_FEATURE_KEYS = list(FEATURE_KEYS)

# B1 缺失掩码布局（2026-08-31）：推理/训练同协议——每特征 (值, missing_mask) 双列，
# 7 逻辑特征 = 14 实际维。与 model_registry.B1_MASKING_PROTOCOL 同构，两处须同步。
MASKING_PROTOCOL = {
    "enabled": True,
    "layout": "value_then_mask",
    "missing_value": 0.0,
    "mask_value": 1.0,
}


def current_feature_protocol() -> dict:
    """当前引擎特征协议（registry 登记/校验共用，避免两处漂移）。"""
    from core import model_registry
    return model_registry.make_feature_protocol(FEATURE_KEYS, masking=MASKING_PROTOCOL)


def _load_cfg() -> dict:
    return json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class DirectionModel:
    """方向三分类器（P(up)/P(flat)/P(down)）。每个 horizon 一个实例。"""

    def __init__(self, horizon: int, flat_margin: float = 0.003):
        self.horizon = horizon
        self.flat_margin = flat_margin
        self.clf = (HistGradientBoostingClassifier(max_iter=200, learning_rate=0.08,
                                                   max_depth=3, early_stopping=True,
                                                   random_state=42)
                    if _SKLEARN else None)
        self._trained = False
        self.brier = None
        self.rank_ic = None
        self.calib = None      # (bin_edges, hit_rates)

    def features_labels(self, samples: list[dict]) -> tuple[np.ndarray, np.ndarray]:
        """从样本构造特征矩阵与三分类标签。返回已按 horizon 掩码过滤。"""
        X, y, _ = [], [], []
        for s in samples:
            fwd = s.get(f"fwd{self.horizon}")
            if fwd is None:
                continue
            # B1（2026-08-31）：缺失不再伪装成 0——每特征输出 (值, missing_mask) 双列，
            # 缺失值填 0 但 mask=1 让模型可区分「真 0」与「缺失」。
            row = []
            for k in FEATURE_KEYS:
                v = s.get(k)
                row.append(0.0 if v is None else float(v))
                row.append(1.0 if v is None else 0.0)
            X.append(row)
            m = self.flat_margin
            if fwd > m:
                y.append(2)       # up
            elif fwd < -m:
                y.append(0)       # down
            else:
                y.append(1)       # flat
            # est_chg 方向桶（供 Rank IC 用独立标签）
        if not X:
            return np.zeros((0, len(FEATURE_KEYS) * 2)), np.zeros(0, dtype=int)
        return np.array(X, dtype=float), np.array(y, dtype=int)

    def fit(self, samples: list[dict]) -> bool:
        X, y = self.features_labels(samples)
        if len(y) < 50 or not _SKLEARN:
            return False
        self.clf.fit(X, y)
        self._trained = True
        return True


class ReturnQuantileModel:
    """收益分布回归（E/Q10/Q50/Q90）。v8（2026-08-30，GPT 四审③④⑤）真分位数回归。

    v7（2026-08-29）曾以「点预测 ± 1.282σ 残差正态近似」过渡并如实标注（假分位）；
    本版起 Q10/Q50/Q90 由 quantile loss 的 HistGradientBoostingRegressor 直接估计，
    是随特征状态变化的真条件分位数；E[return] 仍为平方损失回归（条件均值）。
    交叉兜底：三个分位数预测若违背单调序（q10≤q50≤q90），按排序重排。
    """

    QUANTILES = (0.10, 0.50, 0.90)

    def __init__(self, horizon: int):
        self.horizon = horizon
        if _SKLEARN:
            self.reg_e = HistGradientBoostingRegressor(
                max_iter=200, learning_rate=0.08, max_depth=3,
                early_stopping=True, random_state=42)
            self.reg_q = {q: HistGradientBoostingRegressor(
                              loss="quantile", quantile=q,
                              max_iter=200, learning_rate=0.08, max_depth=3,
                              early_stopping=True, random_state=42)
                          for q in self.QUANTILES}
        else:
            self.reg_e = None
            self.reg_q = {}
        self._trained = False
        self.resid_std = None

    def _xy(self, samples: list[dict]) -> tuple[np.ndarray, np.ndarray]:
        X, y = [], []
        for s in samples:
            fwd = s.get(f"fwd{self.horizon}")
            if fwd is None:
                continue
            # B1（2026-08-31）：同 DirectionModel，值 + missing_mask 双列
            row = []
            for k in FEATURE_KEYS:
                v = s.get(k)
                row.append(0.0 if v is None else float(v))
                row.append(1.0 if v is None else 0.0)
            X.append(row)
            y.append(float(fwd))
        return np.array(X, dtype=float), np.array(y, dtype=float)

    def fit(self, samples: list[dict]) -> bool:
        X, y = self._xy(samples)
        if len(y) < 50 or not _SKLEARN:
            return False
        self.reg_e.fit(X, y)
        for m in self.reg_q.values():
            m.fit(X, y)
        preds = self.reg_e.predict(X)
        self.resid_std = float(np.std(y - preds))   # 供 confidence 的 std 因子（非分位依据）
        self._trained = True
        return True

    def predict_dist(self, features: list[float]) -> dict:
        """返回 {e, q10, q50, q90, std}。

        Q10/Q50/Q90 为 quantile 回归真条件分位（非正态近似）；交叉时单调重排。
        """
        if not self._trained:
            return {"e": 0.0, "q10": 0.0, "q50": 0.0, "q90": 0.0, "std": 0.0}
        e = float(self.reg_e.predict([features])[0])
        q10 = float(self.reg_q[0.10].predict([features])[0])
        q50 = float(self.reg_q[0.50].predict([features])[0])
        q90 = float(self.reg_q[0.90].predict([features])[0])
        q10, q50, q90 = sorted((q10, q50, q90))     # 单调兑底（分位数交叉时重排）
        return {"e": e, "q10": q10, "q50": q50, "q90": q90, "std": self.resid_std or 0.0}


class ForecastEngine:
    """多周期条件分布预测器：Direction(三分类) + ReturnQuantile(分位数) + 置信度。"""

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or _load_cfg()
        fc = cfg.get("forecast", {})
        self.horizons = fc.get("horizons", [1, 3, 5])
        self.flat_margin = fc.get("prob_flat_margin", 0.003)
        self.model_ready = bool(fc.get("model_ready", False))
        self.model_loaded = False               # 权重是否已安全加载（离线研究可用真推理）
        self.model_approved = False              # config + registry validation/promotion 原子授权
        self.approval_error: str | None = None
        self.dirmodels = {h: DirectionModel(h, self.flat_margin) for h in self.horizons}
        self.quantmodels = {h: ReturnQuantileModel(h) for h in self.horizons}
        self._fit_ok = False
        self._evals: dict = {}       # {horizon: {brier, rank_ic, calib, ...}}
        self.loaded_at: str | None = None   # 权重训练时间（save/load 时写入；None=从未训存）
        self.trained_oos_start: str | None = None  # 冻结切分点：train < 此日期（2026-08-28）
        self.last_fit_n: int = 0        # 最近一次 fit 的样本数（registry 留档用）
        self.load_error: str | None = None    # v7：load 拒绝原因（hash_mismatch 等），None=正常
        # v8（2026-08-30）：LOFO 跨基金泛化证据 → 逐基金 stability（缺失=中性 0.5）
        self._fund_evidence = _load_fund_evidence()

    # ---------- 训练 / 评估 ----------
    def fit(self, samples: list[dict]) -> bool:
        ok_dir = sum(m.fit(samples) for m in self.dirmodels.values())
        ok_quant = sum(m.fit(samples) for m in self.quantmodels.values())
        self._fit_ok = (ok_dir == len(self.horizons)) and (ok_quant == len(self.horizons))
        self.last_fit_n = len(samples)      # v7：registry 登记用（n_train 留档）
        return self._fit_ok

    # ---------- 持久化（fit → save → load 闭环） ----------
    def save_models(self, snapshot_provenance: dict | None = None) -> Path | None:
        """全部 horizon 的分类器/回归器原子写入 data/models/。未训练成功返回 None。

        snapshot_provenance（V4.3.1 ③）：resolve_samples() 的冻结件三元组，
        原样透传给 register_model 绑定进 registry；缺省 None ⇒ 条目三字段为
        None（诚实留痕，不伪造）。
        """
        if not self._fit_ok:
            return None
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "model_version": MODEL_VERSION,
            "feature_keys": list(FEATURE_KEYS),
            "horizons": list(self.horizons),
            "flat_margin": self.flat_margin,
            "trained_at": datetime.now().isoformat(timespec="seconds"),
            "oos_start": getattr(self, "trained_oos_start", None),
            "dirmodels": {h: m.clf for h, m in self.dirmodels.items()},
            "quantmodels": {h: {"reg_e": m.reg_e, "reg_q": m.reg_q,
                                "resid_std": m.resid_std}
                            for h, m in self.quantmodels.items()},
        }
        path = MODELS_DIR / f"forecast_v{MODEL_VERSION}.pkl"
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            pickle.dump(payload, fh)
        tmp.replace(path)                  # 原子替换：断电不留半截权重
        self.loaded_at = payload["trained_at"]   # 本进程自训时也记录训练时间
        # v7（P1 registry+hash）：落盘后立即登记 sha256 + 训练元数据
        from core import model_registry
        digest = model_registry.register_model(path, meta={
            "model_version": MODEL_VERSION,
            "feature_keys": list(FEATURE_KEYS),
            "horizons": list(self.horizons),
            "flat_margin": self.flat_margin,
            "trained_at": payload["trained_at"],
            "oos_start": payload["oos_start"],
            "n_train": int(getattr(self, "last_fit_n", 0)),
        }, snapshot_provenance=snapshot_provenance)
        if digest is None:
            print(f"[warn] pkl 已落盘但 registry 登记失败：{path}（load 将拒绝加载直到重新登记）")
        else:
            # v3（2026-09-01，GPT 五审）：登记特征协议（逻辑键 + 实际维度 14）
            model_registry.bind_feature_protocol(
                path.name, model_registry.make_feature_protocol(
                    FEATURE_KEYS, masking=MASKING_PROTOCOL))
        return path

    def load_models(self) -> bool:
        """从 data/models/ 恢复权重。版本/特征键/horizon 任一不匹配 → 拒绝并返回 False。"""
        if not _SKLEARN:
            return False
        path = MODELS_DIR / f"forecast_v{MODEL_VERSION}.pkl"
        if not path.exists():
            return False
        from core import model_registry
        expected_protocol = model_registry.make_feature_protocol(
            FEATURE_KEYS, masking=MASKING_PROTOCOL)
        # 安全边界：必须先验证原始 bytes 的 registry hash，再反序列化 pickle。
        raw, reason = model_registry.read_verified_model_bytes(path)
        if raw is None:
            self.load_error = reason
            return False
        ok2, reason2 = model_registry.verify_feature_protocol(path.name, expected_protocol)
        if not ok2:
            self.load_error = f"feature_protocol:{reason2}"
            return False
        approval_ok, approval_reason = model_registry.verify_approval(path, expected_protocol)
        self.approval_error = None if approval_ok else approval_reason
        try:
            payload = pickle.loads(raw)
        except Exception:
            self.load_error = "deserialize_error"
            return False                    # 损坏文件不阻断主流程，保持未训练占位
        if (payload.get("model_version") != MODEL_VERSION
                or payload.get("feature_keys") != list(FEATURE_KEYS)
                or list(payload.get("horizons", [])) != list(self.horizons)
                or abs(float(payload.get("flat_margin", -1.0)) - self.flat_margin) > 1e-9):
            self.load_error = "payload_contract_mismatch"
            return False
        for h, clf in payload.get("dirmodels", {}).items():
            dm = self.dirmodels.get(h)
            if dm is not None and clf is not None:
                dm.clf = clf
                dm._trained = True
        for h, qm_payload in payload.get("quantmodels", {}).items():
            qm = self.quantmodels.get(h)
            if (qm is not None and isinstance(qm_payload, dict)
                    and qm_payload.get("reg_e") is not None):
                qm.reg_e = qm_payload["reg_e"]
                qm.reg_q = qm_payload.get("reg_q", {})
                qm.resid_std = qm_payload.get("resid_std")
                qm._trained = True
        self._fit_ok = True
        self.model_loaded = True                # 权重安全加载成功（与对外批准分离）
        self.model_approved = bool(self.model_ready and approval_ok)
        if self.model_ready and not approval_ok:
            self.load_error = f"approval:{approval_reason}"
        self.loaded_at = payload.get("trained_at")
        self.trained_oos_start = payload.get("oos_start")
        return True

    def evaluate(self, samples: list[dict]) -> dict:
        """TimeSeriesSplit + OOS 评估 → 写入 self._evals，返回汇总。
        backtest_forecast.py 肩负实际指标计算；本方法仅保留兼容占位。"""
        return self._evals

    # ---------- 推理 ----------
    def predict(self, features: dict, data_quality: dict | None = None,
                freshness: float = 1.0, cross_horizon: dict | None = None,
                fund_code: str | None = None) -> Forecast:
        """给定特征 + 数据质量信号，输出三周期预测。

        data_quality: {coverage, ...} → 数据完整度（不进市场特征，用于置信度）
        freshness:    持仓新鲜度 [0,1]
        cross_horizon: 外部多周期一致性信号（可选）；缺省时由引擎内部
                       用其余周期的 p_up 合成（2026-08-29 P1：真实接线）。
        fund_code:    基金代码（v8：LOFO 逐基金证据查 stability，无则中性 0.5）
        """
        # B1（2026-08-31）：推理侧与训练侧同协议——值 + missing_mask 双列（14 维）
        vec = []
        for k in FEATURE_KEYS:
            v = features.get(k)
            vec.append(0.0 if v is None else float(v))
            vec.append(1.0 if v is None else 0.0)
        hf_list = []
        # 第一遍：先算出全部周期的 proba/dist（供跨周期一致性使用）
        raws = []
        for h in self.horizons:
            dm = self.dirmodels[h]
            qm = self.quantmodels[h]
            if not (dm._trained and qm._trained):
                raws.append(None)
                continue
            proba = dm.clf.predict_proba([vec])[0]
            raws.append({"proba": proba, "dist": qm.predict_dist(vec)})
        # 第二遍：合成各周期置信度 + 输出（P1：cross_horizon 真实接线 + data_f 去占位）
        for i, h in enumerate(self.horizons):
            r = raws[i]
            if r is None:
                hf_list.append(HorizonForecast(
                    horizon=h, p_up=0.0, p_flat=0.0, p_down=0.0,
                    e_return=0.0, q10=0.0, q50=0.0, q90=0.0,
                    confidence=0.0, model_ready=False))
                continue
            # 类别顺序：0=down, 1=flat, 2=up
            p_down, p_flat, p_up = float(r["proba"][0]), float(r["proba"][1]), float(r["proba"][2])
            dist = r["dist"]
            # 跨周期一致性：外部信号优先；否则用其余周期（不含自身）的 p_up
            if cross_horizon is not None:
                ch = cross_horizon
            else:
                ch = {oh: {"p_up": float(raws[j]["proba"][2])}
                      for j, oh in enumerate(self.horizons)
                      if j != i and raws[j] is not None}
            conf = self._confidence(freshness, ch, p_up, dist["std"],
                                    data_quality=data_quality, fund_code=fund_code)
            hf_list.append(HorizonForecast(
                horizon=h, p_up=_clamp(p_up, 0, 1), p_flat=_clamp(p_flat, 0, 1),
                p_down=_clamp(p_down, 0, 1), e_return=dist["e"], q10=dist["q10"],
                q50=dist["q50"], q90=dist["q90"],
                confidence=conf, model_ready=self.model_approved and self._fit_ok))
        return Forecast(
            t1=hf_list[0], t3=hf_list[1] if len(hf_list) > 1 else hf_list[0],
            t5=hf_list[2] if len(hf_list) > 2 else hf_list[-1],
            state=self._infer_state(hf_list), path=self._infer_path(hf_list),
            overall_confidence=self._overall_confidence(hf_list),
            meta={"model_ready": self.model_approved and self._fit_ok,
                  "model_loaded": self.model_loaded,
                  "fund_evidence": self.get_fund_evidence(fund_code) if fund_code else None})

    # ---------- 置信度合成（GPT 第二十五节） ----------
    def _confidence(self, freshness: float, cross_horizon: dict | None,
                    p_up: float, std: float,
                    data_quality: dict | None = None,
                    fund_code: str | None = None) -> float:
        """四因子置信度：模型稳定性 → 数据完整 → 持仓新鲜 → 周期一致。

        2026-08-28 修（GPT 九）：不再用「训练成功」冒充「OOS 稳定」。
        2026-08-29 P1（GPT 三审）：两个死因子接线——
        - data_f：由 data_quality.coverage（重仓覆盖度%）映射：≥80%→1.0，
          ≤40%→0.0，线性；无数据 → 0.6 中性（如实标注）。
        - consistency：cross_horizon 已由 predict 内部用其余周期 p_up 真实合成
          （此前从未传入，恒走 0.6 占位）。
        2026-08-30（GPT 四审 P1-⑬）：stability 改吃 LOFO 逐基金证据，不再全局
        0.85 硬编码；无证据 → 0.5 中性。
        """
        # v8：stability 由 LOFO 证据决定。冻结映射（backtest_lofo.py docstring）：
        #   泛化成立 0.85 / 泛化不成立 0.2 / 证据不足(CI跨零) 0.6 / 小样本或无证据 0.5。
        # 未过模型闸（model_ready=false）仍封顶 0.2——未验证模型不因证据抬升。
        if fund_code and fund_code in self._fund_evidence:
            stability = float(self._fund_evidence[fund_code].get("stability", 0.5))
        else:
            stability = 0.5                      # 无 LOFO 证据 → 诚实中性
        if not (self.model_approved and self._fit_ok):
            stability = min(stability, 0.2)
        freshness_f = _clamp(freshness, 0, 1)
        cov = (data_quality or {}).get("coverage")
        if isinstance(cov, (int, float)):
            data_f = _clamp((float(cov) - 40.0) / 40.0, 0.0, 1.0)
        else:
            data_f = 0.6                                     # 无覆盖度数据 → 中性
        if cross_horizon is not None:
            pu = [v.get("p_up", 0.0) for v in cross_horizon.values()]
            if len(pu) >= 2:
                consistency = 1.0 - np.std(pu)              # 周期方向差异小 → 高一致
            elif len(pu) == 1:
                consistency = 0.7                            # 单周期参考，弱一致
            else:
                consistency = 0.6
        else:
            consistency = 0.6
        return _clamp(stability * 0.4 + data_f * 0.2 + freshness_f * 0.2 + consistency * 0.2, 0, 1)

    def _overall_confidence(self, hf_list: list[HorizonForecast]) -> float:
        if not hf_list:
            return 0.0
        return float(np.mean([h.confidence for h in hf_list]))

    def get_fund_evidence(self, fund_code: str) -> dict | None:
        """LOFO 逐基金证据摘要（报告层「概率 vs 可信度」分离展示用）。"""
        ev = self._fund_evidence.get(fund_code)
        if not ev:
            return None
        return {"stability": float(ev.get("stability", 0.5)),
                "verdict": ev.get("verdict", "无判词"),
                "n_oos": (ev.get("horizons") or {}).get("5", {}).get("n_oos")}

    # ---------- 轻量状态 / 路径（State+Scenario 轻量版，不铺重型 engine） ----------
    def _infer_state(self, hf_list: list[HorizonForecast]) -> str:
        """由三周期方向概率合成轻量状态。UP / DOWN / RANGE / MIXED。"""
        if not hf_list or not hf_list[0].model_ready:
            return "UNKNOWN"
        t1 = hf_list[0]
        if t1.p_up >= 0.6:
            return "UP"
        if t1.p_down >= 0.6:
            return "DOWN"
        if t1.p_flat >= 0.5:
            return "RANGE"
        return "MIXED"

    def _infer_path(self, hf_list: list[HorizonForecast]) -> str:
        """多周期一致性 → 最可能路径描述（2026-08-30 重写，GPT 四审 P0-⑤）。

        旧版缺陷：只看 E[return] 符号序列，与方向概率直接矛盾——
        T+5 P(down)=57% 时仍可因 e 单调递增输出「持续上行」。

        新口径：路径方向由各周期方向概率主导倾向决定（p_up/p_flat/p_down
        的 argmax，且主导概率 ≥0.5 才算明确方向，否则该周期记 mixed），
        从 T+1→T+3→T+5 倾向序列推导：
          全 up            → 短期→中期持续上行
          全 down          → 短期→中期持续走弱
          全 flat          → 三周期均以震荡为主
          down → up        → 短线回调后企稳回升（V 型）
          up → down        → 短线冲高后回落（中期转弱）
          up → flat/mixed  → 冲高后转震荡
          down → flat/mixed→ 走弱后企稳震荡
          flat → up        → 震荡后中枢上移
          flat → down      → 震荡后中枢下移
          其余             → 无明确单一路径
        E[return] 不再参与路径方向判断（仍展示于表格）。
        """
        if not hf_list or not hf_list[0].model_ready:
            return "数据不足以判断路径"

        def _lean(hf: HorizonForecast) -> str:
            probs = {"up": hf.p_up, "flat": hf.p_flat, "down": hf.p_down}
            top = max(probs, key=probs.get)
            return top if probs[top] >= 0.5 else "mixed"

        seq = [_lean(h) for h in hf_list]
        first, last = seq[0], seq[-1]
        if all(s == "up" for s in seq):
            return "短期→中期持续上行"
        if all(s == "down" for s in seq):
            return "短期→中期持续走弱"
        if all(s == "flat" for s in seq):
            return "三周期均以震荡为主"
        if first == "down" and last == "up":
            return "短线回调后企稳回升（V 型）"
        if first == "up" and last == "down":
            return "短线冲高后回落（中期转弱）"
        if first == "up":
            return "冲高后转震荡"
        if first == "down":
            return "走弱后企稳震荡"
        if first == "flat" and last == "up":
            return "震荡后中枢上移"
        if first == "flat" and last == "down":
            return "震荡后中枢下移"
        return "无明确单一路径（以震荡为主，注意区间分化）"


# ---------- 进程级单例（run.py / decision_engine 共用） ----------
_ENGINE_CACHE: dict = {}


def get_loaded_engine() -> "ForecastEngine":
    """返回加载了持久化权重的引擎单例。

    - data/models/forecast_vN.pkl 存在且版本匹配 → 载入（_fit_ok=True，可真推理）
    - 无文件/版本不符/sklearn 缺失 → 返回未训练空壳（predict 恒走占位分支）
    """
    fc = _load_cfg().get("forecast", {})
    key = (MODEL_VERSION, tuple(FEATURE_KEYS),
           tuple(fc.get("horizons", [1, 3, 5])), float(fc.get("prob_flat_margin", 0.003)))
    eng = _ENGINE_CACHE.get(key)
    if eng is None:
        eng = ForecastEngine()
        eng.load_models()
        _ENGINE_CACHE[key] = eng
    return eng


def forecast_to_cn(f: Forecast) -> dict:
    """Forecast → 中文输出字典（报告/推送用，兼容旧 decision_to_cn 风格）。"""
    def _h(hf: HorizonForecast) -> dict:
        return {
            "horizon": hf.horizon,
            "p_up": round(hf.p_up, 3), "p_flat": round(hf.p_flat, 3),
            "p_down": round(hf.p_down, 3),
            "e_return": round(hf.e_return, 4), "q10": round(hf.q10, 4),
            "q50": round(hf.q50, 4), "q90": round(hf.q90, 4),
            "confidence": round(hf.confidence, 2),
            "model_ready": hf.model_ready,
        }
    return {
        "T1": _h(f.t1), "T3": _h(f.t3), "T5": _h(f.t5),
        "state": f.state, "path": f.path,
        "overall_confidence": round(f.overall_confidence, 2),
        "model_ready": f.meta.get("model_ready", False),
        "fund_evidence": f.meta.get("fund_evidence"),
    }
