import json
import hashlib
from typing import AsyncGenerator

from llm_client import get_client, resolve_model, is_qwen, stream_chat
from chain_analyzer import ChainAnalyzer
from company_analyzer import CompanyAnalyzer
from portfolio_research import PortfolioResearch
from technical_analyzer import TechnicalAnalyzer
import rag_client
import report_generator

_INTENT_PROMPT = """你是一个投研意图识别器。根据用户输入，返回 JSON，格式严格如下：
{"intent": "<chain|company|portfolio|compare|technical|portfolio_technical|qa>", "entities": ["实体1", "实体2"]}

意图说明：
- chain：分析某个行业/产业链（如"分析AI算力产业链"）
- company：分析某家公司或股票代码（如"分析英伟达"、"NVDA在哪一层"）
- portfolio：用户询问自己持仓相关（如"我的持仓哪些值得加仓"）
- compare：对比两家公司（如"比较NVDA和AMD"）
- technical：技术分析/走势分析/K线/趋势/支撑阻力（如"NVDA技术面"、"特斯拉走势分析"、"BTC技术分析"、"苹果的支撑位在哪"）
- portfolio_technical：持仓技术面分析/持仓走势（如"持仓技术面"、"我的持仓走势怎么样"、"持仓技术分析"）
- qa：其他通用问题

只返回 JSON，不要任何解释。"""


class Orchestrator:
    def __init__(self):
        self._chain = ChainAnalyzer()
        self._company = CompanyAnalyzer()
        self._portfolio = PortfolioResearch()
        self._technical = TechnicalAnalyzer()

    async def _detect_intent(self, message: str, model: str) -> dict:
        # 意图识别统一用轻量模型：deepseek/其余模型用 v4-pro，qwen 系列用 flash
        # 不能直接用传入的 model（如 claude-sonnet-5 等推理模型不支持 temperature 参数会 400）
        if is_qwen(model):
            intent_model = "qwen3.6-flash"
        else:
            intent_model = "deepseek-v4-pro"
        resp = await get_client(intent_model).chat.completions.create(
            model=intent_model,
            messages=[
                {"role": "system", "content": _INTENT_PROMPT},
                {"role": "user", "content": message},
            ],
            temperature=0,
            max_tokens=100,
        )
        raw = (resp.choices[0].message.content or "").strip()
        try:
            return json.loads(raw)
        except Exception:
            return {"intent": "qa", "entities": []}

    async def stream(self, message: str, session_id: str, model: str | None = None) -> AsyncGenerator[str, None]:
        model = resolve_model(model)
        intent_data = await self._detect_intent(message, model)
        intent = intent_data.get("intent", "qa")
        entities = intent_data.get("entities", [])

        if intent == "chain" and entities:
            async for chunk in self._chain.stream(entities[0], model):
                yield chunk

        elif intent == "company" and entities:
            async for chunk in self._company.stream(entities[0], model):
                yield chunk

        elif intent == "compare" and len(entities) >= 2:
            yield f"## 对比分析：{entities[0]} vs {entities[1]}\n\n"
            for ticker in entities[:2]:
                yield "---\n\n"
                async for chunk in self._company.stream(ticker, model):
                    yield chunk

        elif intent == "technical" and entities:
            async for chunk in self._technical.stream(entities[0], model):
                yield chunk

        elif intent == "portfolio_technical":
            async for chunk in self._technical.stream_portfolio(model):
                yield chunk

        elif intent == "portfolio":
            async for chunk in self._portfolio.stream(model):
                yield chunk

        else:
            async for chunk in self._qa_stream(message, model):
                yield chunk

    async def _qa_stream(self, message: str, model: str) -> AsyncGenerator[str, None]:
        cache_key = hashlib.md5(message.encode()).hexdigest()
        cached = report_generator.get_cached("qa", cache_key, model)
        if cached:
            yield cached
            return

        # 尝试从 RAG 获取实时新闻上下文
        rag_results = await rag_client.search_news(query=message, top_k=4, hours=72)
        rag_context = rag_client.fmt_news_context(rag_results)

        system_content = "你是一个专业的投资研究助手，擅长行业分析和股票研究。"
        if rag_context:
            system_content += f"\n\n【实时新闻参考（RAG 检索）】\n{rag_context}\n\n请在回答中注明引用的新闻来源和时间。"

        chunks = []
        async for text in stream_chat(
            model=model,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": message},
            ],
            max_tokens=8000,
            temperature=0.3,
        ):
            chunks.append(text)
            yield text

        if chunks:
            report_generator.save_cache("qa", cache_key, "".join(chunks), model)
