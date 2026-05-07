"""
news-rag-system 接入客户端

环境变量：
  RAG_API_URL  — RAG 服务地址，默认 http://43.139.5.125:8080
  RAG_API_KEY  — X-API-Key 鉴权
"""
import os
from typing import Optional

import httpx

RAG_API_URL = os.getenv("RAG_API_URL", "http://43.139.5.125:8080")
RAG_API_KEY = os.getenv("RAG_API_KEY", "")

_TIMEOUT = 8.0  # 超时短，RAG 慢了直接降级，不阻塞主流程


async def search_news(
    query: str,
    tickers: list[str] | None = None,
    data_types: list[str] | None = None,
    top_k: int = 5,
    min_importance: float = 0.3,
    hours: int = 72,
) -> list[dict]:
    """语义检索 RAG 新闻，返回 chunk 列表，失败时返回 []（静默降级）

    data_types 可选: news / sec_filing / regulatory / insider / earnings / macro
    """
    if not RAG_API_KEY:
        return []

    body: dict = {
        "query": query,
        "top_k": top_k,
        "min_importance": min_importance,
        "hours": hours,
    }
    if tickers:
        body["tickers"] = tickers
    if data_types:
        body["data_types"] = data_types

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{RAG_API_URL}/api/v1/rag/search",
                headers={"X-API-Key": RAG_API_KEY},
                json=body,
            )
            resp.raise_for_status()
            return resp.json().get("results", [])
    except Exception:
        return []


async def get_signals(
    asset: str | None = None,
    min_importance: float = 0.6,
    hours: int = 48,
) -> list[dict]:
    """查询 RAG 系统中的结构化信号，失败时返回 []"""
    if not RAG_API_KEY:
        return []

    params: dict = {"min_importance": min_importance, "hours": hours}
    if asset:
        params["asset"] = asset

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{RAG_API_URL}/api/v1/signals",
                headers={"X-API-Key": RAG_API_KEY},
                params=params,
            )
            resp.raise_for_status()
            return resp.json().get("signals", [])
    except Exception:
        return []


def fmt_news_context(results: list[dict], max_chars: int = 300) -> str:
    """把 RAG 结果格式化为 prompt 注入文本"""
    if not results:
        return ""
    lines = []
    for r in results:
        date = (r.get("published_at") or "")[:10]
        src = r.get("source_name", "")
        sentiment = r.get("sentiment", "")
        importance = r.get("importance_score")
        imp_str = f" 重要性{importance:.1f}" if importance else ""
        sentiment_str = f" [{sentiment}]" if sentiment else ""
        text = (r.get("chunk_text") or r.get("summary_cn") or "")[:max_chars]
        lines.append(f"[{date}][{src}]{sentiment_str}{imp_str}\n{text}")
    return "\n\n".join(lines)
