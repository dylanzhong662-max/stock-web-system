import os
import sqlite3
from typing import AsyncGenerator

import data_fetcher
import report_generator
from llm_client import get_client, resolve_model, is_deepseek
from company_analyzer import CompanyAnalyzer

_PROMPT_FILE = os.path.join(os.path.dirname(__file__), "prompts", "portfolio_research.md")

HOLDER_DB_PATH = os.getenv(
    "HOLDER_DB_PATH",
    os.path.expanduser("~/Desktop/holderAndAction/data/trading.db"),
)


def _read_positions() -> list[dict]:
    if not os.path.exists(HOLDER_DB_PATH):
        return []
    conn = sqlite3.connect(HOLDER_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT ticker, asset_name, quantity, entry_price, current_price, pnl_pct "
            "FROM positions WHERE status = 'open'"
        )
        return [dict(row) for row in cur.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


class PortfolioResearch:
    def __init__(self):
        self._company = CompanyAnalyzer()

    async def analyze(self, model: str | None = None) -> tuple[str, list[dict]]:
        model = resolve_model(model)
        positions = _read_positions()
        if not positions:
            return "暂无开仓持仓，无法生成报告。", []

        enriched = await self._enrich_positions(positions)
        prompt = self._build_prompt(enriched)

        chunks = []
        async for chunk in self._stream(prompt, model):
            chunks.append(chunk)
        content = "".join(chunks)

        report = report_generator.format_report(
            content, f"holderAndAction trading.db + FMP + Tavily + {model}"
        )
        report_generator.save_cache("portfolio", "latest", report)
        return report, enriched

    async def stream(self, model: str | None = None) -> AsyncGenerator[str, None]:
        model = resolve_model(model)
        positions = _read_positions()
        if not positions:
            yield "暂无开仓持仓，无法生成报告。"
            return

        enriched = await self._enrich_positions(positions)
        prompt = self._build_prompt(enriched)

        chunks = []
        async for chunk in self._stream(prompt, model):
            chunks.append(chunk)
            yield chunk

        content = "".join(chunks)
        report = report_generator.format_report(
            content, f"holderAndAction trading.db + FMP + Tavily + {model}"
        )
        report_generator.save_cache("portfolio", "latest", report)

    async def _enrich_positions(self, positions: list[dict]) -> list[dict]:
        import asyncio
        tickers = [p["ticker"] for p in positions]
        fin_results, beta_results, corr_data = await asyncio.gather(
            asyncio.gather(*[data_fetcher.get_stock_data(t) for t in tickers], return_exceptions=True),
            asyncio.gather(*[data_fetcher.get_beta(t) for t in tickers], return_exceptions=True),
            data_fetcher.get_correlation_matrix(tickers),
            return_exceptions=True,
        )
        enriched = []
        for pos, fin, beta in zip(positions, fin_results, beta_results):
            entry = dict(pos)
            entry["financial"] = fin if isinstance(fin, dict) else {}
            entry["beta"] = beta if isinstance(beta, float) else None
            enriched.append(entry)
        if isinstance(corr_data, dict):
            for entry in enriched:
                entry["_corr_data"] = corr_data
        return enriched

    def _fmt_beta(self, beta) -> str:
        if beta is None:
            return "N/A"
        return f"{beta:.2f}x"

    def _build_prompt(self, positions: list[dict]) -> str:
        with open(_PROMPT_FILE, encoding="utf-8") as f:
            template = f.read()

        lines = []
        for p in positions:
            fin = p.get("financial", {})
            pnl = p.get("pnl_pct")
            pnl_str = f"{pnl * 100:+.1f}%" if pnl is not None else "N/A"
            beta = p.get("beta")
            lines.append(
                f"- **{p['ticker']}** ({p.get('asset_name', '')}): "
                f"成本 {p.get('entry_price', 'N/A')} | 盈亏 {pnl_str} | "
                f"Beta {self._fmt_beta(beta)} | "
                f"毛利率 {fin.get('gross_margin', 'N/A')} | "
                f"Forward PE {fin.get('pe_forward', 'N/A')} | "
                f"营收增速 {fin.get('revenue_growth', 'N/A')}"
            )

        positions_text = "\n".join(lines)
        weighted_beta_str = self._calc_weighted_beta(positions)
        corr_data = positions[0].get("_corr_data", {}) if positions else {}
        corr_text = self._fmt_corr(corr_data)

        return (
            template
            .replace("{positions_data}", positions_text)
            .replace("{count}", str(len(positions)))
            .replace("{weighted_beta}", weighted_beta_str)
            .replace("{correlation_matrix}", corr_text)
        )

    def _calc_weighted_beta(self, positions: list[dict]) -> str:
        betas = [p.get("beta") for p in positions if p.get("beta") is not None]
        if not betas:
            return "N/A"
        avg = sum(betas) / len(betas)
        return f"{avg:.2f}x（{len(betas)}/{len(positions)} 个持仓有效，等权平均）"

    def _fmt_corr(self, corr_data: dict) -> str:
        pairs = corr_data.get("pairs", {})
        if not pairs:
            return "数据不足，无法计算"
        lines = []
        for k, v in sorted(pairs.items()):
            flag = " ⚠️ 高度相关" if abs(v) >= 0.7 else ""
            lines.append(f"  - {k}: {v:.2f}{flag}")
        return "\n".join(lines) if lines else "暂无数据"

    async def _stream(self, prompt: str, model: str) -> AsyncGenerator[str, None]:
        kwargs: dict = dict(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=6000,
            stream=True,
        )

        stream = await get_client(model).chat.completions.create(**kwargs)
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if is_deepseek(model) and hasattr(delta, "reasoning_content") and delta.reasoning_content:
                continue
            if delta.content:
                yield delta.content
