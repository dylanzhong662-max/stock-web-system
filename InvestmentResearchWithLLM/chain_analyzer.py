import os
import re
import asyncio
from typing import AsyncGenerator

import data_fetcher
import report_generator
from llm_client import get_client, resolve_model, is_deepseek

_PROMPT_FILE = os.path.join(os.path.dirname(__file__), "prompts", "chain_analysis.md")

# Common non-ticker uppercase words to exclude when scanning search results
_TICKER_STOPWORDS = {
    "AI", "US", "USA", "UK", "EU", "CEO", "CFO", "CTO", "IPO", "ETF", "GPU", "CPU",
    "HBM", "API", "SDK", "ASIC", "DRAM", "NAND", "LLC", "INC", "LTD", "CORP",
    "CAGR", "YOY", "QOQ", "TTM", "EBIT", "EBITDA", "PE", "PB", "EPS", "ML", "LLM",
    "AWS", "GCP", "IDC", "IOT", "HPC", "B2B", "B2C", "FCF", "ROI", "ROE", "ROA",
    "ROIC", "GDP", "CPI", "PPI", "ECS", "VPC", "CDN", "DNS", "CSP", "SLA",
    "THE", "FOR", "AND", "NOT", "BUT", "ARE", "WAS", "HAS", "HAD", "ITS",
    "NEW", "TOP", "KEY", "CEO", "COO", "CXO", "FY", "Q1", "Q2", "Q3", "Q4",
    "AI", "NN", "CV", "NLP", "RL", "GAN", "CNN", "RNN", "IT", "EV", "AR", "VR",
    "PC", "OS", "KB", "MB", "GB", "TB", "PB", "EB",
}


def _extract_tickers(search_results: list[dict]) -> list[str]:
    """Scan search results for US ticker-like symbols (e.g. $NVDA or standalone NVDA)."""
    full_text = " ".join(
        r.get("content", "") + " " + r.get("title", "")
        for r in search_results
    )
    # Dollar-prefixed tickers take priority; also catch bare uppercase 2-5 letter words
    dollar_hits = re.findall(r'\$([A-Z]{1,5})\b', full_text)
    plain_hits = re.findall(r'\b([A-Z]{2,5})\b', full_text)

    seen: set[str] = set()
    tickers: list[str] = []
    for sym in dollar_hits + plain_hits:
        if sym and sym not in _TICKER_STOPWORDS and sym not in seen:
            seen.add(sym)
            tickers.append(sym)
    return tickers[:10]


def _fmt_financial(fin: dict) -> str:
    """One-line financial summary for prompt injection."""
    def pct(v): return f"{v*100:.1f}%" if v is not None else "N/A"
    def cap(v):
        if v is None:
            return "N/A"
        if v >= 1e12:
            return f"${v/1e12:.2f}T"
        if v >= 1e9:
            return f"${v/1e9:.1f}B"
        return f"${v/1e6:.0f}M"

    ticker = fin.get("ticker", "")
    name = fin.get("name", ticker)
    return (
        f"**{ticker}** ({name}): "
        f"市值 {cap(fin.get('market_cap'))} | "
        f"毛利率 {pct(fin.get('gross_margin'))} | "
        f"营业利润率 {pct(fin.get('operating_margin'))} | "
        f"营收增速 {pct(fin.get('revenue_growth'))} | "
        f"Forward PE {fin.get('pe_forward') or 'N/A'} | "
        f"来源: {fin.get('source', 'yfinance')}"
    )


class ChainAnalyzer:
    def _load_prompt(
        self,
        industry: str,
        search_results: list[dict],
        financial_data: list[dict],
    ) -> str:
        with open(_PROMPT_FILE, encoding="utf-8") as f:
            template = f.read()

        # P0: 800-char content; P3: include URL for citation
        news_text = "\n\n".join(
            f"**{r['title']}**"
            + (f"（{r['published_date']}）" if r.get("published_date") else "")
            + (f" — [原文]({r['url']})" if r.get("url") else "")
            + f"\n{r['content'][:800]}"
            for r in search_results
        )

        # P2: real financial data as ground truth
        fin_text = (
            "\n".join(_fmt_financial(f) for f in financial_data)
            if financial_data
            else "（本次未匹配到代表性上市公司财务数据，财务数字以搜索结果为准）"
        )

        return (
            template
            .replace("{industry}", industry)
            .replace("{search_results}", news_text)
            .replace("{financial_data}", fin_text)
        )

    async def _fetch_all_data(self, industry: str) -> tuple[list[dict], list[dict]]:
        """P0: dual Tavily queries; P2: yfinance for extracted tickers."""
        results_cn, results_en = await asyncio.gather(
            data_fetcher.search(
                f"{industry} 产业链 行业分析 投资 2025 利润率 关键变量",
                max_results=12,
            ),
            data_fetcher.search(
                f"{industry} industry top companies earnings revenue gross margin 2024 2025",
                max_results=5,
            ),
        )
        all_results = results_cn + results_en

        # P2: detect ticker symbols, validate via yfinance (market_cap presence = real company)
        tickers = _extract_tickers(all_results)
        fin_data: list[dict] = []
        if tickers:
            raw = await asyncio.gather(
                *[data_fetcher.get_stock_data(t) for t in tickers],
                return_exceptions=True,
            )
            for r in raw:
                if isinstance(r, dict) and r.get("market_cap"):
                    fin_data.append(r)

        return all_results, fin_data

    async def analyze(self, industry: str, model: str | None = None) -> tuple[str, bool]:
        model = resolve_model(model)
        cached = report_generator.get_cached("chain", industry)
        if cached:
            return cached, True

        all_results, fin_data = await self._fetch_all_data(industry)
        prompt = self._load_prompt(industry, all_results, fin_data)

        chunks: list[str] = []
        async for chunk in self._stream(prompt, model):
            chunks.append(chunk)
        content = "".join(chunks)

        source_note = f"Tavily × {len(all_results)} 条 + yfinance × {len(fin_data)} 只 + {model}"
        report = report_generator.format_report(content, source_note)
        report_generator.save_cache("chain", industry, report)
        return report, False

    async def stream(self, industry: str, model: str | None = None) -> AsyncGenerator[str, None]:
        model = resolve_model(model)
        cached = report_generator.get_cached("chain", industry)
        if cached:
            yield cached
            return

        all_results, fin_data = await self._fetch_all_data(industry)
        prompt = self._load_prompt(industry, all_results, fin_data)

        chunks: list[str] = []
        async for chunk in self._stream(prompt, model):
            chunks.append(chunk)
            yield chunk

        content = "".join(chunks)
        source_note = f"Tavily × {len(all_results)} 条 + yfinance × {len(fin_data)} 只 + {model}"
        report = report_generator.format_report(content, source_note)
        report_generator.save_cache("chain", industry, report)

    async def _stream(self, prompt: str, model: str) -> AsyncGenerator[str, None]:
        kwargs: dict = dict(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=12000,
            stream=True,
        )
        stream = await get_client(model).chat.completions.create(**kwargs)
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if is_deepseek(model) and hasattr(delta, "reasoning_content") and delta.reasoning_content:
                continue
            if delta.content:
                yield delta.content
