"""Tavily 搜索"""
import os
import asyncio

_tavily_client = None


def _get_tavily():
    global _tavily_client
    if _tavily_client is None:
        from tavily import TavilyClient
        api_key = os.getenv("TAVILY_API_KEY", "")
        if not api_key:
            raise RuntimeError("TAVILY_API_KEY not set")
        _tavily_client = TavilyClient(api_key=api_key)
    return _tavily_client


async def search(query: str, max_results: int = 5) -> list[dict]:
    """Tavily 搜索，返回 [{title, url, content, published_date}]"""
    def _sync():
        client = _get_tavily()
        resp = client.search(query, max_results=max_results, search_depth="advanced")
        results = []
        for r in resp.get("results", []):
            date_raw = r.get("published_date", "") or ""
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", ""),
                "published_date": date_raw[:10] if date_raw else "",
            })
        return results
    return await asyncio.get_event_loop().run_in_executor(None, _sync)
