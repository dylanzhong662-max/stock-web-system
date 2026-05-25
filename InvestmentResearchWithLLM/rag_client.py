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


async def get_risk_overlay(days: int = 7) -> dict | None:
    """获取 Polymarket 风险 overlay（regime + position_multiplier + advice）"""
    if not RAG_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{RAG_API_URL}/api/v1/risk/polymarket",
                headers={"X-API-Key": RAG_API_KEY},
                params={"days": days},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return None


async def check_trade(asset: str, direction: str, asset_class: str = "equity") -> dict | None:
    """P2 信号过滤：检查 Polymarket 宏观环境是否支持该交易"""
    if not RAG_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{RAG_API_URL}/api/v1/risk/check",
                headers={"X-API-Key": RAG_API_KEY},
                json={"asset": asset, "direction": direction, "asset_class": asset_class},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return None


def fmt_risk_overlay(overlay: dict | None) -> str:
    """把 risk overlay 格式化为 prompt 注入文本"""
    if not overlay:
        return ""
    regime = overlay.get("regime", "neutral")
    mult = overlay.get("position_multiplier", 1.0)
    reasons = overlay.get("reasons", [])
    advice = overlay.get("advice", {})

    lines = [f"【Polymarket 宏观风险信号】"]
    lines.append(f"  Regime: {regime.upper()} | 仓位系数: {mult}")
    if reasons:
        lines.append(f"  原因: {'; '.join(reasons)}")
    if advice:
        lines.append(f"  止损建议: {advice.get('stop_loss', '')}")
        lines.append(f"  新开仓: {advice.get('new_positions', '')}")
        lines.append(f"  现有仓位: {advice.get('existing_positions', '')}")
    return "\n".join(lines)


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
