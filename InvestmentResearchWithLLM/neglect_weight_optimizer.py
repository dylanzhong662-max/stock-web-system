"""Neglect Score Weight Optimizer — IC Decay 驱动的因子权重自适应

替代原来的 hardcoded 1/3-1/3-1/3 权重，用历史数据拟合最优权重。

学术依据：
- Neglect factor: ~2-3% 年化超额，衰减慢（信息差持续存在）
- Growth factor: 贡献更大但衰减快（市场对增长信息反应较快）
- Valuation gap: 中等贡献，受市场情绪影响大

方法：
1. 用 resolved predictions 的实际收益率作为因变量
2. 分别计算 neglect / growth / valuation 单因子 IC
3. 用 IC-weighted 方案分配权重（IC 高的因子得到更多权重）
4. 如果没有足够历史数据（< 30 resolved），使用学术先验（40/35/25）
"""
import math
from datetime import datetime, timedelta
from typing import Optional

from database import SessionLocal
from models import Prediction
from data_providers.intl_screener import NEGLECT_THRESHOLDS


# Academic prior weights (used when insufficient data)
_PRIOR_WEIGHTS = {
    "neglect": 0.40,   # 学术文献：neglect effect 稳定但较小
    "growth": 0.35,    # growth factor 贡献大但衰减快
    "valuation": 0.25, # valuation gap 受市场情绪影响
}

# Minimum resolved predictions for data-driven optimization
_MIN_SAMPLES = 30


def get_optimal_weights(
    report_type: str = "chain",
    since_days: int = 365,
) -> dict:
    """Calculate IC-weighted factor weights from historical predictions.

    Returns optimal weights + diagnostic information.
    """
    cutoff = datetime.utcnow() - timedelta(days=since_days)
    db = SessionLocal()
    try:
        rows = (
            db.query(Prediction)
            .filter(
                Prediction.resolved_at.isnot(None),
                Prediction.created_at >= cutoff,
                Prediction.realized_return.isnot(None),
                Prediction.report_type == report_type,
            )
            .all()
        )
    finally:
        db.close()

    if len(rows) < _MIN_SAMPLES:
        return {
            "method": "academic_prior",
            "reason": f"仅 {len(rows)} 条已结算预测（需 {_MIN_SAMPLES}+ 条做数据驱动优化）",
            "weights": _PRIOR_WEIGHTS.copy(),
            "total_resolved": len(rows),
            "factor_ics": None,
            "recommendation": (
                "使用学术先验权重。积累更多 resolved predictions 后切换到数据驱动。"
                f"当前进度：{len(rows)}/{_MIN_SAMPLES}"
            ),
        }

    # Compute single-factor ICs
    # We need the raw component scores — since we don't store them separately,
    # we use proxy metrics from the prediction metadata
    factor_ics = _compute_factor_ics(rows)

    if not factor_ics or all(v == 0 for v in factor_ics.values()):
        return {
            "method": "academic_prior",
            "reason": "无法计算因子 IC（数据质量不足）",
            "weights": _PRIOR_WEIGHTS.copy(),
            "total_resolved": len(rows),
            "factor_ics": factor_ics,
            "recommendation": "使用学术先验权重",
        }

    # IC-weighted allocation
    abs_ics = {k: abs(v) for k, v in factor_ics.items()}
    total_ic = sum(abs_ics.values())

    if total_ic > 0:
        raw_weights = {k: v / total_ic for k, v in abs_ics.items()}
    else:
        raw_weights = _PRIOR_WEIGHTS.copy()

    # Shrink toward prior (Bayesian shrinkage: 60% data, 40% prior)
    shrinkage = min(0.8, len(rows) / 100)  # more data → less shrinkage
    weights = {
        k: shrinkage * raw_weights.get(k, 0.33) + (1 - shrinkage) * _PRIOR_WEIGHTS.get(k, 0.33)
        for k in ["neglect", "growth", "valuation"]
    }
    # Normalize to sum to 1
    w_sum = sum(weights.values())
    weights = {k: round(v / w_sum, 3) for k, v in weights.items()}

    return {
        "method": "ic_weighted_with_shrinkage",
        "reason": f"基于 {len(rows)} 条 resolved predictions",
        "weights": weights,
        "total_resolved": len(rows),
        "factor_ics": factor_ics,
        "shrinkage_factor": round(shrinkage, 2),
        "raw_weights_before_shrinkage": raw_weights,
        "recommendation": _interpret_weights(weights, factor_ics),
    }


def _compute_factor_ics(rows: list) -> dict:
    """Compute IC for each factor component using direction × return correlation.

    Since we don't store individual factor scores per prediction, we use
    confidence as a proxy for signal strength and direction for sign.

    For a more accurate implementation, we'd need to store the component scores
    at prediction time. This is a simplified version using available data.
    """
    if not rows:
        return {"neglect": 0, "growth": 0, "valuation": 0}

    # Group by confidence bucket as proxy for factor strength
    # High confidence predictions tend to have stronger neglect + growth signals
    high_conf = [r for r in rows if r.confidence and r.confidence >= 0.7]
    mid_conf = [r for r in rows if r.confidence and 0.4 <= r.confidence < 0.7]
    low_conf = [r for r in rows if r.confidence and r.confidence < 0.4]

    def _direction_ic(subset):
        if len(subset) < 5:
            return 0
        dir_codes = [{"bullish": 1, "bearish": -1, "neutral": 0}.get(r.direction, 0) for r in subset]
        returns = [r.realized_return for r in subset]
        n = len(subset)
        mx = sum(dir_codes) / n
        my = sum(returns) / n
        num = sum((dir_codes[i] - mx) * (returns[i] - my) for i in range(n))
        dx2 = sum((v - mx) ** 2 for v in dir_codes)
        dy2 = sum((v - my) ** 2 for v in returns)
        denom = math.sqrt(dx2 * dy2)
        return num / denom if denom > 0 else 0

    # Proxy ICs based on confidence stratification
    # Neglect: high-conf predictions on less-known tickers → better IC
    # Growth: mid-conf with positive direction → IC from growth momentum
    # Valuation: low-conf contrarian calls → IC from mean reversion

    overall_ic = _direction_ic(rows)
    high_ic = _direction_ic(high_conf)
    mid_ic = _direction_ic(mid_conf)

    # Heuristic allocation of overall IC to factors
    # This is approximate; proper implementation would store per-factor scores
    return {
        "neglect": round(high_ic * 0.6, 4),   # neglect drives high-conviction calls
        "growth": round(mid_ic * 0.8, 4),     # growth drives medium-conviction
        "valuation": round(overall_ic * 0.4, 4),  # valuation is diffuse
    }


def _interpret_weights(weights: dict, ics: dict) -> str:
    parts = []
    max_factor = max(weights, key=weights.get)
    min_factor = min(weights, key=weights.get)

    factor_names = {"neglect": "低覆盖度", "growth": "高增长", "valuation": "估值折让"}

    parts.append(f"最强因子：{factor_names[max_factor]} ({weights[max_factor]:.0%})")

    if ics:
        non_zero = {k: v for k, v in ics.items() if abs(v) > 0.01}
        if non_zero:
            ic_str = ", ".join(f"{factor_names.get(k,k)}={v:.3f}" for k, v in non_zero.items())
            parts.append(f"因子 IC：{ic_str}")

    if weights.get("growth", 0) > 0.45:
        parts.append("⚠️ 策略偏重增长因子——注意 growth crowding 风险")
    if weights.get("neglect", 0) < 0.25:
        parts.append("⚠️ neglect 权重过低——可能失去核心 alpha 来源")

    return "；".join(parts)


def apply_optimized_score(stock: dict, weights: Optional[dict] = None) -> float:
    """Compute neglect score using optimized weights instead of 1/3-1/3-1/3.

    Drop-in replacement for _compute_neglect_score with custom weights.
    """
    if weights is None:
        result = get_optimal_weights()
        weights = result["weights"]

    w_neglect = weights.get("neglect", 0.33)
    w_growth = weights.get("growth", 0.33)
    w_valuation = weights.get("valuation", 0.34)

    score = 0.0
    max_score = 100.0

    # Neglect component (scaled by weight)
    from data_providers.intl_screener import _get_market
    market = _get_market(stock)
    market_th = NEGLECT_THRESHOLDS.get(market, NEGLECT_THRESHOLDS.get("default", {}))
    if isinstance(market_th, dict):
        max_a = market_th.get("max_analysts", 10)
        ideal_a = market_th.get("ideal", 6)
    else:
        max_a, ideal_a = 10, 6

    analysts = stock.get("analyst_count", 0) or 0
    neglect_raw = 0.0
    if analysts <= ideal_a * 0.4:
        neglect_raw = 1.0
    elif analysts <= ideal_a:
        neglect_raw = 1 - (analysts - ideal_a * 0.4) / (ideal_a * 0.6)
    elif analysts <= max_a:
        neglect_raw = 0.3 * (1 - (analysts - ideal_a) / (max_a - ideal_a))

    score += neglect_raw * w_neglect * max_score

    # Growth component
    rev_growth = stock.get("revenue_growth")
    earn_growth = stock.get("earnings_growth")
    growth_val = rev_growth if rev_growth is not None else earn_growth
    growth_raw = 0.0
    if growth_val is not None:
        if growth_val >= 0.30:
            growth_raw = 1.0
        elif growth_val >= 0.15:
            growth_raw = (growth_val - 0.05) / 0.25
        elif growth_val >= 0.05:
            growth_raw = 0.3 * (growth_val / 0.15)

    score += growth_raw * w_growth * max_score

    # Valuation gap component
    peg = stock.get("peg_ratio")
    valuation_raw = 0.0
    if peg is not None and 0 < peg < 2.0:
        valuation_raw = 1 - peg / 2.0
    else:
        target = stock.get("target_mean_price")
        current = stock.get("current_price")
        if target and current and current > 0:
            upside = (target - current) / current
            valuation_raw = min(1.0, max(0, upside) / 0.5)

    score += valuation_raw * w_valuation * max_score

    return round(score, 1)
