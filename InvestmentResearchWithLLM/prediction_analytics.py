"""预测分析：confidence calibration + walk-forward + IC decay

补齐评分中指出的关键缺陷：
1. Confidence Calibration：模型说 0.65 时，实际命中率是否接近 65%？
2. Walk-Forward：按时间滚动窗口计算命中率/IC 的稳定性
3. IC Decay：预测发出后 1/7/14/30 天的信息系数衰减曲线
"""
import math
from datetime import datetime, timedelta
from typing import Optional

from database import SessionLocal
from models import Prediction


def confidence_calibration(
    report_type: str | None = None,
    since_days: int = 365,
    n_buckets: int = 5,
) -> dict:
    """按 confidence 分桶统计实际命中率，检验模型是否校准

    完美校准：confidence=0.6 的预测，实际命中率也应 ≈ 60%
    返回每个桶的 {bucket_range, count, predicted_conf, actual_hit_rate, calibration_error}
    """
    cutoff = datetime.utcnow() - timedelta(days=since_days)
    db = SessionLocal()
    try:
        q = db.query(Prediction).filter(
            Prediction.resolved_at.isnot(None),
            Prediction.created_at >= cutoff,
            Prediction.confidence.isnot(None),
        )
        if report_type:
            q = q.filter(Prediction.report_type == report_type)
        rows = q.all()
    finally:
        db.close()

    if not rows:
        return {"buckets": [], "total": 0, "avg_calibration_error": None, "brier_score": None, "interpretation": "数据不足"}

    # 按 confidence 排序分桶
    sorted_rows = sorted(rows, key=lambda r: r.confidence or 0)
    bucket_size = max(1, len(sorted_rows) // n_buckets)
    buckets = []

    for i in range(0, len(sorted_rows), bucket_size):
        chunk = sorted_rows[i:i + bucket_size]
        if not chunk:
            continue
        confs = [r.confidence for r in chunk]
        hits = [1 if r.hit else 0 for r in chunk]
        avg_conf = sum(confs) / len(confs)
        actual_rate = sum(hits) / len(hits)
        cal_error = abs(actual_rate - avg_conf)

        buckets.append({
            "bucket_range": f"{min(confs):.2f}-{max(confs):.2f}",
            "count": len(chunk),
            "predicted_conf": round(avg_conf, 3),
            "actual_hit_rate": round(actual_rate, 3),
            "calibration_error": round(cal_error, 3),
        })

    # Brier Score（越低越好，0 = 完美，0.25 = 随机）
    brier = sum(
        (r.confidence - (1.0 if r.hit else 0.0)) ** 2
        for r in rows
    ) / len(rows)

    avg_cal_error = sum(b["calibration_error"] for b in buckets) / len(buckets) if buckets else None

    return {
        "buckets": buckets,
        "total": len(rows),
        "avg_calibration_error": round(avg_cal_error, 3) if avg_cal_error is not None else None,
        "brier_score": round(brier, 4),
        "interpretation": _interpret_calibration(avg_cal_error, brier),
    }


def _interpret_calibration(avg_error: float | None, brier: float | None) -> str:
    if avg_error is None or brier is None:
        return "数据不足"
    if avg_error < 0.05 and brier < 0.15:
        return "校准良好：置信度可信赖"
    if avg_error < 0.10:
        return "校准尚可：轻微高估/低估置信度"
    if avg_error < 0.20:
        return "校准偏差较大：模型过度自信或过度保守"
    return "校准严重偏差：confidence 字段不可参考"


def walk_forward_performance(
    report_type: str | None = None,
    window_days: int = 30,
    since_days: int = 365,
) -> dict:
    """滚动窗口 walk-forward 分析：按月统计命中率、IC、Sharpe 的稳定性

    检验策略是否时间一致（非偶然某段好）
    """
    cutoff = datetime.utcnow() - timedelta(days=since_days)
    db = SessionLocal()
    try:
        q = db.query(Prediction).filter(
            Prediction.resolved_at.isnot(None),
            Prediction.created_at >= cutoff,
        )
        if report_type:
            q = q.filter(Prediction.report_type == report_type)
        rows = q.order_by(Prediction.created_at).all()
    finally:
        db.close()

    if not rows:
        return {"windows": [], "stability": None}

    # 按 window_days 分窗
    windows = []
    start = rows[0].created_at
    end = datetime.utcnow()
    current = start

    while current < end:
        window_end = current + timedelta(days=window_days)
        chunk = [r for r in rows if current <= r.created_at < window_end]
        if chunk:
            hits = sum(1 for r in chunk if r.hit)
            returns = [r.realized_return for r in chunk if r.realized_return is not None]
            excess = [r.excess_return for r in chunk if r.excess_return is not None]

            hit_rate = hits / len(chunk)
            avg_ret = sum(returns) / len(returns) if returns else None
            avg_excess = sum(excess) / len(excess) if excess else None

            windows.append({
                "period": f"{current.strftime('%Y-%m-%d')} ~ {window_end.strftime('%Y-%m-%d')}",
                "count": len(chunk),
                "hit_rate": round(hit_rate, 3),
                "avg_return": round(avg_ret, 4) if avg_ret is not None else None,
                "avg_excess": round(avg_excess, 4) if avg_excess is not None else None,
            })
        current = window_end

    # 稳定性：命中率的标准差 / 均值（变异系数）
    hit_rates = [w["hit_rate"] for w in windows if w["count"] >= 3]
    if len(hit_rates) >= 3:
        mean_hr = sum(hit_rates) / len(hit_rates)
        std_hr = (sum((h - mean_hr) ** 2 for h in hit_rates) / len(hit_rates)) ** 0.5
        cv = std_hr / mean_hr if mean_hr > 0 else float("inf")
        stability = {
            "mean_hit_rate": round(mean_hr, 3),
            "std_hit_rate": round(std_hr, 3),
            "cv": round(cv, 3),
            "is_stable": cv < 0.3,
            "interpretation": (
                "时间稳定" if cv < 0.2
                else "轻微不稳定" if cv < 0.3
                else "高度不稳定——策略可能依赖特定市场环境"
            ),
        }
    else:
        stability = None

    return {"windows": windows, "stability": stability}


def ic_decay_analysis(
    report_type: str | None = None,
    since_days: int = 365,
) -> dict:
    """IC Decay：预测发出后不同时间点的方向正确率

    评估信号衰减速度——30 天 horizon 的预测，可能在第 7 天就已经 price-in 了
    """
    cutoff = datetime.utcnow() - timedelta(days=since_days)
    db = SessionLocal()
    try:
        q = db.query(Prediction).filter(
            Prediction.resolved_at.isnot(None),
            Prediction.created_at >= cutoff,
            Prediction.entry_price.isnot(None),
            Prediction.ticker.isnot(None),
        )
        if report_type:
            q = q.filter(Prediction.report_type == report_type)
        rows = q.all()
    finally:
        db.close()

    if not rows:
        return {"decay_curve": [], "peak_ic_day": None, "peak_ic": None, "interpretation": "数据不足"}

    # 按 horizon_days 分组
    horizon_buckets: dict[int, list] = {}
    for r in rows:
        bucket = r.horizon_days
        if bucket not in horizon_buckets:
            horizon_buckets[bucket] = []
        horizon_buckets[bucket].append(r)

    decay_curve = []
    for horizon in sorted(horizon_buckets.keys()):
        chunk = horizon_buckets[horizon]
        if len(chunk) < 5:
            continue

        def _dir_code(d: str) -> int:
            return {"bullish": 1, "bearish": -1, "neutral": 0}.get(d, 0)

        paired = [
            (_dir_code(r.direction), r.realized_return)
            for r in chunk if r.realized_return is not None
        ]
        if len(paired) < 5:
            continue

        n = len(paired)
        dx = [p[0] for p in paired]
        dy = [p[1] for p in paired]
        mx, my = sum(dx) / n, sum(dy) / n
        num = sum((dx[i] - mx) * (dy[i] - my) for i in range(n))
        dx2 = sum((v - mx) ** 2 for v in dx)
        dy2 = sum((v - my) ** 2 for v in dy)
        denom = math.sqrt(dx2 * dy2) if dx2 > 0 and dy2 > 0 else 0
        ic = num / denom if denom > 0 else 0

        hits = sum(1 for r in chunk if r.hit)
        decay_curve.append({
            "horizon_days": horizon,
            "count": len(chunk),
            "ic": round(ic, 3),
            "hit_rate": round(hits / len(chunk), 3),
        })

    peak = max(decay_curve, key=lambda x: abs(x["ic"])) if decay_curve else None

    return {
        "decay_curve": decay_curve,
        "peak_ic_day": peak["horizon_days"] if peak else None,
        "peak_ic": peak["ic"] if peak else None,
        "interpretation": _interpret_decay(decay_curve) if rows else "数据不足",
    }


def _interpret_decay(curve: list[dict]) -> str:
    if not curve:
        return "数据不足"
    ics = [c["ic"] for c in curve]
    if all(abs(ic) < 0.05 for ic in ics):
        return "信号无预测力（IC 接近 0）——模型建议可能无真实 alpha"
    if len(ics) >= 2 and abs(ics[-1]) < abs(ics[0]) * 0.5:
        return "信号快速衰减——考虑缩短持仓周期"
    if all(abs(ic) > 0.1 for ic in ics):
        return "信号持久——当前 horizon 设定合理"
    return "信号强度混合——需要更多数据判断"


def full_analytics(report_type: str | None = None, since_days: int = 365) -> dict:
    """一次性返回所有分析指标"""
    return {
        "calibration": confidence_calibration(report_type, since_days),
        "walk_forward": walk_forward_performance(report_type, since_days=since_days),
        "ic_decay": ic_decay_analysis(report_type, since_days),
    }
