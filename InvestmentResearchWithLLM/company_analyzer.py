import asyncio
import os
from typing import AsyncGenerator

import data_fetcher
import rag_client
import report_generator
from llm_client import get_client, resolve_model, is_deepseek

_PROMPT_FILE = os.path.join(os.path.dirname(__file__), "prompts", "company_analysis.md")


def _fmt(val, pct=False, suffix="") -> str:
    if val is None:
        return "N/A"
    if pct:
        return f"{val * 100:.1f}%"
    if isinstance(val, float) and val > 1e9:
        return f"${val / 1e9:.1f}B"
    if isinstance(val, float) and val > 1e6:
        return f"${val / 1e6:.0f}M"
    if isinstance(val, float):
        return f"{val:.2f}{suffix}"
    return str(val)


class CompanyAnalyzer:
    def _load_prompt(self, ticker: str, financial: dict, news: list[dict]) -> str:
        with open(_PROMPT_FILE, encoding="utf-8") as f:
            template = f.read()
        news_text = "\n\n".join(
            f"**{r['title']}**"
            + (f"（{r['published_date']}）" if r.get("published_date") else "")
            + (f" — [原文]({r['url']})" if r.get("url") else "")
            + f"\n{r['content'][:800]}"
            for r in news
        )
        return (
            template
            .replace("{company}", financial.get("name", ticker))
            .replace("{ticker}", ticker)
            .replace("{market_cap}", _fmt(financial.get("market_cap")))
            .replace("{gross_margin}", _fmt(financial.get("gross_margin"), pct=True))
            .replace("{operating_margin}", _fmt(financial.get("operating_margin"), pct=True))
            .replace("{pe_ttm}", _fmt(financial.get("pe_ttm"), suffix="x"))
            .replace("{pe_forward}", _fmt(financial.get("pe_forward"), suffix="x"))
            .replace("{revenue_growth}", _fmt(financial.get("revenue_growth"), pct=True))
            .replace("{news_results}", news_text)
        )

    async def analyze(self, ticker: str, model: str | None = None) -> tuple[str, dict]:
        model = resolve_model(model)
        cached = report_generator.get_cached("company", ticker.upper(), model)
        if cached:
            return cached, {}

        financial, news = await self._fetch_data(ticker)
        prompt = self._load_prompt(ticker, financial, news)

        chunks = []
        async for chunk in self._stream(prompt, model):
            chunks.append(chunk)
        content = "".join(chunks)

        report = report_generator.format_report(content, f"FMP + yfinance + Tavily + {model}")
        report_generator.save_cache("company", ticker.upper(), report, model)
        return report, financial

    async def stream(self, ticker: str, model: str | None = None) -> AsyncGenerator[str, None]:
        model = resolve_model(model)
        cached = report_generator.get_cached("company", ticker.upper(), model)
        if cached:
            yield cached
            return

        financial, news = await self._fetch_data(ticker)
        prompt = self._load_prompt(ticker, financial, news)

        chunks = []
        async for chunk in self._stream(prompt, model):
            chunks.append(chunk)
            yield chunk

        content = "".join(chunks)
        report = report_generator.format_report(content, f"FMP + yfinance + Tavily + {model}")
        report_generator.save_cache("company", ticker.upper(), report, model)

    async def _fetch_data(self, ticker: str) -> tuple[dict, list[dict]]:
        financial, rag_results, tavily_results = await asyncio.gather(
            data_fetcher.get_stock_data(ticker),
            rag_client.search_news(
                query=f"{ticker} earnings revenue outlook risk",
                tickers=[ticker.upper().split(".")[0]],
                data_types=["news", "earnings", "sec_filing", "insider"],
                top_k=6,
            ),
            data_fetcher.search(f"{ticker} 公司分析 最新动态 财报 2025", max_results=5),
        )
        # RAG 结果优先（带时效标注），不足时用 Tavily 补
        if rag_results:
            rag_news = [
                {
                    "title": r.get("chunk_text", "")[:80],
                    "content": r.get("chunk_text", ""),
                    "published_date": (r.get("published_at") or "")[:10],
                    "url": "",
                    "_source": r.get("source_name", ""),
                    "_sentiment": r.get("sentiment", ""),
                    "_importance": r.get("importance_score"),
                }
                for r in rag_results
            ]
            # Tavily 补充至 8 条
            combined = rag_news + tavily_results[: max(0, 8 - len(rag_news))]
        else:
            combined = tavily_results
        return financial, combined

    async def _stream(self, prompt: str, model: str) -> AsyncGenerator[str, None]:
        kwargs: dict = dict(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=8000,
            temperature=0.3,
            stream=True,
        )

        stream = await get_client(model).chat.completions.create(**kwargs)
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if is_deepseek(model) and hasattr(delta, "reasoning_content") and delta.reasoning_content:
                continue
            if delta.content:
                yield delta.content
