from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Optional

from chain_analyzer import ChainAnalyzer
from company_analyzer import CompanyAnalyzer
from portfolio_research import PortfolioResearch
import predictions as predictions_mod
import prediction_analytics
import watchlist as watchlist_mod
import watchlist_alerter

router = APIRouter()
_chain = ChainAnalyzer()
_company = CompanyAnalyzer()
_portfolio = PortfolioResearch()


class ChainRequest(BaseModel):
    industry: str


class CompanyRequest(BaseModel):
    ticker: str


@router.post("/chain")
async def research_chain(req: ChainRequest):
    report, cached = await _chain.analyze(req.industry)
    return {"report": report, "cached": cached}


@router.post("/company")
async def research_company(req: CompanyRequest):
    report, financial = await _company.analyze(req.ticker)
    return {"report": report, "financial": financial}


@router.post("/portfolio")
async def research_portfolio():
    report, positions = await _portfolio.analyze()
    return {"report": report, "positions": positions}


@router.get("/reports")
async def list_reports(
    type: str = Query(None, description="chain | company | portfolio"),
    limit: int = Query(10, ge=1, le=50),
):
    from database import SessionLocal
    from models import ReportCache
    from datetime import datetime

    db = SessionLocal()
    try:
        q = db.query(ReportCache).filter(ReportCache.expires_at > datetime.utcnow())
        if type:
            q = q.filter(ReportCache.report_type == type)
        rows = q.order_by(ReportCache.created_at.desc()).limit(limit).all()
        return [
            {
                "id": r.id,
                "type": r.report_type,
                "key": r.cache_key,
                "created_at": r.created_at.isoformat(),
                "expires_at": r.expires_at.isoformat(),
            }
            for r in rows
        ]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 预测跟踪：命中率 / IC / 超额收益
# ---------------------------------------------------------------------------

@router.get("/predictions")
async def list_predictions(
    type: str = Query(None, description="chain | company | portfolio"),
    resolved: bool | None = Query(None, description="true=已结算, false=未到期, 省略=全部"),
    limit: int = Query(50, ge=1, le=500),
):
    return predictions_mod.list_recent(report_type=type, resolved=resolved, limit=limit)


@router.get("/predictions/performance")
async def predictions_performance(
    type: str = Query(None, description="chain | company | portfolio；省略=全部"),
    since_days: int = Query(365, ge=1, le=3650),
):
    return predictions_mod.performance(report_type=type, since_days=since_days)


@router.post("/predictions/resolve")
async def resolve_predictions(
    max_rows: int = Query(200, ge=1, le=1000),
    force: bool = Query(False, description="true=强制结算未到期预测（用当前价格），false=只结算已到期的"),
):
    """手动触发结算。force=true 会用当前价格提前结算（测试用）。"""
    return await predictions_mod.resolve_due(max_rows=max_rows, force=force)


# ---------------------------------------------------------------------------
# 预测分析：校准 / Walk-Forward / IC Decay
# ---------------------------------------------------------------------------

@router.get("/predictions/analytics")
async def predictions_analytics(
    type: str = Query(None, description="chain | company | portfolio"),
    since_days: int = Query(365, ge=30, le=3650),
):
    """完整分析：confidence 校准 + walk-forward + IC decay"""
    return prediction_analytics.full_analytics(report_type=type, since_days=since_days)


@router.get("/predictions/calibration")
async def predictions_calibration(
    type: str = Query(None),
    since_days: int = Query(365, ge=30, le=3650),
):
    """Confidence 校准分析：模型置信度 vs 实际命中率"""
    return prediction_analytics.confidence_calibration(report_type=type, since_days=since_days)


# ---------------------------------------------------------------------------
# 监控清单 API
# ---------------------------------------------------------------------------

class WatchItemCreate(BaseModel):
    variable: str
    ticker: Optional[str] = None
    frequency: str = "weekly"
    bullish_signal: Optional[str] = None
    bearish_signal: Optional[str] = None
    priority: int = 2
    notes: Optional[str] = None


class WatchItemUpdate(BaseModel):
    current_value: Optional[str] = None
    notes: Optional[str] = None


@router.get("/watchlist")
async def get_watchlist(
    ticker: str = Query(None, description="按 ticker 筛选"),
    limit: int = Query(50, ge=1, le=200),
):
    """查看当前活跃的监控清单"""
    return watchlist_mod.list_active(ticker=ticker, limit=limit)


@router.post("/watchlist")
async def add_watch_item(item: WatchItemCreate):
    """手动添加监控项"""
    return watchlist_mod.add_item(
        variable=item.variable,
        ticker=item.ticker,
        frequency=item.frequency,
        bullish_signal=item.bullish_signal,
        bearish_signal=item.bearish_signal,
        priority=item.priority,
        source="manual",
        notes=item.notes,
    )


@router.delete("/watchlist/{item_id}")
async def remove_watch_item(item_id: int):
    """停用监控项"""
    ok = watchlist_mod.remove_item(item_id)
    return {"success": ok}


@router.put("/watchlist/{item_id}/value")
async def update_watch_value(item_id: int, body: WatchItemUpdate):
    """更新监控项的最新观测值"""
    if body.current_value:
        ok = watchlist_mod.update_value(item_id, body.current_value)
        return {"success": ok}
    return {"success": False, "error": "current_value required"}


# ---------------------------------------------------------------------------
# 监控预警 → 飞书推送
# ---------------------------------------------------------------------------

@router.post("/watchlist/check-alerts")
async def check_watchlist_alerts(
    notify: bool = Query(True, description="是否发送飞书通知"),
):
    """检查所有监控项阈值，触发时推飞书。可 cron 定时调用。"""
    if notify:
        return await watchlist_alerter.run_check_and_notify()
    return await watchlist_alerter.check_alerts()
