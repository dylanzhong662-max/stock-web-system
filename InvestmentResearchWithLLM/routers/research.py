from fastapi import APIRouter, Query
from pydantic import BaseModel

from chain_analyzer import ChainAnalyzer
from company_analyzer import CompanyAnalyzer
from portfolio_research import PortfolioResearch

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
