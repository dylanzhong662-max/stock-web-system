import json
from typing import AsyncGenerator

from llm_client import get_client, resolve_model, is_deepseek
from chain_analyzer import ChainAnalyzer
from company_analyzer import CompanyAnalyzer
from portfolio_research import PortfolioResearch

_INTENT_PROMPT = """你是一个投研意图识别器。根据用户输入，返回 JSON，格式严格如下：
{"intent": "<chain|company|portfolio|compare|qa>", "entities": ["实体1", "实体2"]}

意图说明：
- chain：分析某个行业/产业链（如"分析AI算力产业链"）
- company：分析某家公司或股票代码（如"分析英伟达"、"NVDA在哪一层"）
- portfolio：用户询问自己持仓相关（如"我的持仓哪些值得加仓"）
- compare：对比两家公司（如"比较NVDA和AMD"）
- qa：其他通用问题

只返回 JSON，不要任何解释。"""


class Orchestrator:
    def __init__(self):
        self._chain = ChainAnalyzer()
        self._company = CompanyAnalyzer()
        self._portfolio = PortfolioResearch()

    async def _detect_intent(self, message: str, model: str) -> dict:
        # 意图识别用轻量模型：deepseek 系列用 v4-pro，其余用传入模型
        intent_model = "deepseek-v4-pro" if is_deepseek(model) else model
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

        elif intent == "portfolio":
            async for chunk in self._portfolio.stream(model):
                yield chunk

        else:
            async for chunk in self._qa_stream(message, model):
                yield chunk

    async def _qa_stream(self, message: str, model: str) -> AsyncGenerator[str, None]:
        stream = await get_client(model).chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是一个专业的投资研究助手，擅长行业分析和股票研究。"},
                {"role": "user", "content": message},
            ],
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
