from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Optional

from chain_analyzer import ChainAnalyzer
from company_analyzer import CompanyAnalyzer
from portfolio_research import PortfolioResearch
from technical_analyzer import TechnicalAnalyzer
import predictions as predictions_mod
import prediction_analytics
import watchlist as watchlist_mod
import watchlist_alerter
import backtest_simulation
import parameter_sensitivity
import consistency_test
import neglect_weight_optimizer
import scaling_advisor

router = APIRouter()
_chain = ChainAnalyzer()
_company = CompanyAnalyzer()
_portfolio = PortfolioResearch()
_technical = TechnicalAnalyzer()


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


# ---------------------------------------------------------------------------
# 技术分析
# ---------------------------------------------------------------------------

class TechnicalRequest(BaseModel):
    ticker: str
    model: Optional[str] = None


@router.post("/technical")
async def research_technical(req: TechnicalRequest):
    """技术分析：多维度指标计算 + LLM 综合研判（趋势/动量/结构/Fibonacci）"""
    report, indicators = await _technical.analyze(req.ticker, model=req.model)
    return {"report": report, "indicators": indicators}


@router.post("/technical/portfolio")
async def research_technical_portfolio(model: Optional[str] = None):
    """持仓技术面综合分析：对每个持仓计算指标 + LLM 综合研判"""
    report, indicators = await _technical.analyze_portfolio(model=model)
    return {"report": report, "positions_count": len(indicators)}


@router.get("/technical/indicators")
async def technical_indicators(
    ticker: str = Query(..., description="股票代码，如 NVDA"),
):
    """仅返回计算指标（不调 LLM），轻量快速"""
    return await _technical.quick_indicators(ticker)


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


# ---------------------------------------------------------------------------
# Backtest Simulation
# ---------------------------------------------------------------------------

class BacktestRequest(BaseModel):
    industry: str
    n_months: int = 12
    horizon_days: int = 90
    transaction_cost_bps: int = 50
    top_n: int = 10


@router.post("/backtest")
async def run_backtest(req: BacktestRequest):
    """Walk-forward backtest：每月筛选 → 持有 horizon_days → 结算超额收益。

    包含交易成本，输出 t-stat 检验是否统计显著。
    """
    return await backtest_simulation.run_walk_forward_backtest(
        industry=req.industry,
        n_months=req.n_months,
        horizon_days=req.horizon_days,
        transaction_cost_bps=req.transaction_cost_bps,
        top_n=req.top_n,
    )


# ---------------------------------------------------------------------------
# Parameter Sensitivity Analysis
# ---------------------------------------------------------------------------

class SensitivityRequest(BaseModel):
    industry: str


@router.post("/sensitivity")
async def run_sensitivity(req: SensitivityRequest):
    """参数敏感性分析：测试 Neglect Score 各阈值 ±30% 时结果变化。

    CV < 0.3 = 稳健，> 0.5 = 极度敏感（信号不可靠）。
    """
    import data_fetcher

    # Screen with current params to get raw stock pool
    try:
        candidates = await data_fetcher.screen_neglected_growth(
            industry=req.industry,
            search_fn=data_fetcher.search,
            max_candidates=50,
        )
    except Exception as e:
        return {"error": f"Screening failed: {e}"}

    # Get raw stock pool (before filtering) by lowering thresholds
    from data_providers.intl_screener import _yf_fundamentals
    from industry_seed_lists import get_seed_list

    us_seeds, intl_seeds = get_seed_list(req.industry)
    intl_stocks = await _yf_fundamentals(intl_seeds[:25]) if intl_seeds else []

    # Combine screened candidates + raw intl stocks as pool
    stock_pool = candidates + intl_stocks

    if not stock_pool:
        return {"error": "No stock pool data available for this industry"}

    return parameter_sensitivity.run_sensitivity(stock_pool)


# ---------------------------------------------------------------------------
# Consistency Test
# ---------------------------------------------------------------------------

class ConsistencyRequest(BaseModel):
    industry: str
    n_runs: int = 3
    model: Optional[str] = None


@router.post("/consistency")
async def run_consistency_test(req: ConsistencyRequest):
    """一致性测试：同一产业链跑 N 次分析，比较预测方向一致性。

    consistency_score > 80 = 信号可复现，< 40 = 纯噪声。
    耗时约 N × 30-60 秒（每次都是完整的 R1 调用）。
    """
    return await consistency_test.run_consistency_test(
        industry=req.industry,
        n_runs=req.n_runs,
        model=req.model,
    )


# ---------------------------------------------------------------------------
# Neglect Score Weight Optimization
# ---------------------------------------------------------------------------

@router.get("/weights")
async def get_optimal_weights(
    report_type: str = Query("chain", description="chain | company"),
    since_days: int = Query(365, ge=30, le=3650),
):
    """查看当前 Neglect Score 因子权重（IC-weighted 或学术先验）。

    积累 30+ resolved predictions 后自动切换到数据驱动权重。
    """
    return neglect_weight_optimizer.get_optimal_weights(
        report_type=report_type,
        since_days=since_days,
    )


# ---------------------------------------------------------------------------
# 盈利加仓评估
# ---------------------------------------------------------------------------

@router.get("/scaling")
async def get_scaling_advice():
    """评估所有开仓持仓的盈利加仓条件。

    基于 ATR 倒金字塔规则：浮盈≥1ATR + 价格>MA5 + 无放量上影 + 建仓<5天。
    返回每个持仓的详细评估和操作建议。
    """
    results = await scaling_advisor.evaluate_scaling()
    eligible = [r for r in results if r.get("eligible")]
    return {
        "total_positions": len(results),
        "eligible_count": len(eligible),
        "positions": results,
        "summary": scaling_advisor.format_scaling_context(results),
    }
