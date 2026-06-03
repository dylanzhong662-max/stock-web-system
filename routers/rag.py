"""RAG 事件搜索接口 — 代理到 news-rag-system"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "InvestmentResearchWithLLM"))

from fastapi import APIRouter, Query
from typing import Optional

router = APIRouter()


@router.get("/search")
async def rag_search(
    query: str = Query(..., description="搜索关键词或标的名"),
    ticker: Optional[str] = Query(None, description="标的 ticker，如 NVDA"),
    top_k: int = Query(10, ge=1, le=30),
    hours: int = Query(168, ge=1, le=720),
):
    from rag_client import search_news

    tickers = [ticker.upper()] if ticker else None
    results = await search_news(
        query=query,
        tickers=tickers,
        top_k=top_k,
        hours=hours,
        min_importance=0.0,
    )
    return {"results": results, "count": len(results)}
