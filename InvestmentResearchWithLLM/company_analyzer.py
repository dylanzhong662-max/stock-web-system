import os
from typing import AsyncGenerator
from openai import AsyncOpenAI

import data_fetcher
import report_generator

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
    def __init__(self):
        self._client = AsyncOpenAI(
            api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            base_url="https://api.deepseek.com",
        )

    def _load_prompt(self, ticker: str, financial: dict, news: list[dict]) -> str:
        with open(_PROMPT_FILE, encoding="utf-8") as f:
            template = f.read()
        news_text = "\n\n".join(
            f"**{r['title']}**"
            + (f"（{r['published_date']}）" if r.get("published_date") else "")
            + f"\n{r['content'][:300]}"
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

    async def analyze(self, ticker: str) -> tuple[str, dict]:
        """返回 (report_markdown, financial_dict)"""
        cached = report_generator.get_cached("company", ticker.upper())
        if cached:
            return cached, {}

        financial, news = await self._fetch_data(ticker)
        prompt = self._load_prompt(ticker, financial, news)

        chunks = []
        async for chunk in self._stream_r1(prompt):
            chunks.append(chunk)
        content = "".join(chunks)

        report = report_generator.format_report(content, "FMP + yfinance + Tavily + DeepSeek V4 Pro")
        report_generator.save_cache("company", ticker.upper(), report)
        return report, financial

    async def stream(self, ticker: str) -> AsyncGenerator[str, None]:
        cached = report_generator.get_cached("company", ticker.upper())
        if cached:
            yield cached
            return

        financial, news = await self._fetch_data(ticker)
        prompt = self._load_prompt(ticker, financial, news)

        chunks = []
        async for chunk in self._stream_r1(prompt):
            chunks.append(chunk)
            yield chunk

        content = "".join(chunks)
        report = report_generator.format_report(content, "FMP + yfinance + Tavily + DeepSeek V4 Pro")
        report_generator.save_cache("company", ticker.upper(), report)

    async def _fetch_data(self, ticker: str) -> tuple[dict, list[dict]]:
        import asyncio
        financial, news = await asyncio.gather(
            data_fetcher.get_stock_data(ticker),
            data_fetcher.search(f"{ticker} 公司分析 最新动态 2025", max_results=3),
        )
        return financial, news

    async def _stream_r1(self, prompt: str) -> AsyncGenerator[str, None]:
        stream = await self._client.chat.completions.create(
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
            stream=True,
            extra_body={"thinking": {"type": "enabled", "budget_tokens": 2000}},
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                continue
            if delta.content:
                yield delta.content
