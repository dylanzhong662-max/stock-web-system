import os
from typing import AsyncGenerator
from openai import AsyncOpenAI

import data_fetcher
import report_generator

_PROMPT_FILE = os.path.join(os.path.dirname(__file__), "prompts", "chain_analysis.md")


class ChainAnalyzer:
    def __init__(self):
        self._client = AsyncOpenAI(
            api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            base_url="https://api.deepseek.com",
        )

    def _load_prompt(self, industry: str, search_results: list[dict]) -> str:
        with open(_PROMPT_FILE, encoding="utf-8") as f:
            template = f.read()
        news_text = "\n\n".join(
            f"**{r['title']}**"
            + (f"（{r['published_date']}）" if r.get("published_date") else "")
            + f"\n{r['content'][:300]}"
            for r in search_results
        )
        return template.replace("{industry}", industry).replace("{search_results}", news_text)

    async def analyze(self, industry: str) -> tuple[str, bool]:
        """返回 (report_markdown, is_cached)"""
        cached = report_generator.get_cached("chain", industry)
        if cached:
            return cached, True

        results = await data_fetcher.search(f"{industry} 产业链 行业分析 投资 2025 利润率 关键变量", max_results=8)
        prompt = self._load_prompt(industry, results)

        chunks = []
        async for chunk in self._stream_r1(prompt):
            chunks.append(chunk)
        content = "".join(chunks)

        report = report_generator.format_report(content, "Tavily 搜索 + DeepSeek V4 Pro")
        report_generator.save_cache("chain", industry, report)
        return report, False

    async def stream(self, industry: str) -> AsyncGenerator[str, None]:
        cached = report_generator.get_cached("chain", industry)
        if cached:
            yield cached
            return

        results = await data_fetcher.search(f"{industry} 产业链 行业分析 投资 2025 利润率 关键变量", max_results=8)
        prompt = self._load_prompt(industry, results)

        chunks = []
        async for chunk in self._stream_r1(prompt):
            chunks.append(chunk)
            yield chunk

        content = "".join(chunks)
        report = report_generator.format_report(content, "Tavily 搜索 + DeepSeek V4 Pro")
        report_generator.save_cache("chain", industry, report)

    async def _stream_r1(self, prompt: str) -> AsyncGenerator[str, None]:
        stream = await self._client.chat.completions.create(
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=12000,
            stream=True,
            extra_body={"thinking": {"type": "enabled", "budget_tokens": 4000}},
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                continue
            if delta.content:
                yield delta.content
