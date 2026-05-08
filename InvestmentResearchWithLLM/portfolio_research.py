import asyncio
import os
import sqlite3
from datetime import datetime
from typing import AsyncGenerator

import data_fetcher
import rag_client
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
            "SELECT ticker, asset AS asset_name, quantity, entry_price, "
            "NULL AS current_price, NULL AS pnl_pct "
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

        tickers = [p["ticker"] for p in positions]
        enriched, rag_results, tavily_results = await asyncio.gather(
            self._enrich_positions(positions),
            rag_client.search_news(
                query=" ".join(tickers) + " portfolio risk earnings outlook",
                tickers=tickers,
                data_types=["news", "earnings", "sec_filing", "insider"],
                top_k=8,
            ),
            data_fetcher.search(
                " ".join(tickers[:6]) + " earnings outlook risk 2025",
                max_results=6,
            ),
        )
        rag_context = self._build_news_context(rag_results, tavily_results)
        prompt = self._build_prompt(enriched, rag_context)

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

        cached = report_generator.get_cached("portfolio", "latest")
        if cached:
            yield cached
            return

        positions = _read_positions()
        if not positions:
            yield "暂无开仓持仓，无法生成报告。"
            return

        tickers = [p["ticker"] for p in positions]
        enriched, rag_results, tavily_results = await asyncio.gather(
            self._enrich_positions(positions),
            rag_client.search_news(
                query=" ".join(tickers) + " portfolio risk earnings outlook",
                tickers=tickers,
                data_types=["news", "earnings", "sec_filing", "insider"],
                top_k=8,
            ),
            data_fetcher.search(
                " ".join(tickers[:6]) + " earnings outlook risk 2025",
                max_results=6,
            ),
        )
        rag_context = self._build_news_context(rag_results, tavily_results)
        prompt = self._build_prompt(enriched, rag_context)

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
        tickers = [p["ticker"] for p in positions]

        # ATR 止损位需要 entry_price，无 entry_price 的给 None
        async def _safe_atr(pos: dict):
            ep = pos.get("entry_price")
            if ep is None:
                return None
            return await data_fetcher.get_atr_stops(pos["ticker"], float(ep))

        fin_map, beta_results, corr_data, atr_results = await asyncio.gather(
            data_fetcher.get_batch_stock_data(tickers),
            asyncio.gather(*[data_fetcher.get_beta(t) for t in tickers], return_exceptions=True),
            data_fetcher.get_correlation_matrix(tickers),
            asyncio.gather(*[_safe_atr(p) for p in positions], return_exceptions=True),
            return_exceptions=True,
        )
        if not isinstance(fin_map, dict):
            fin_map = {}

        enriched = []
        for pos, beta_raw, atr_raw in zip(
            positions,
            beta_results if isinstance(beta_results, (list, tuple)) else [None] * len(positions),
            atr_results if isinstance(atr_results, (list, tuple)) else [None] * len(positions),
        ):
            entry = dict(pos)
            fin_data = fin_map.get(pos["ticker"], {"ticker": pos["ticker"], "source": "unavailable"})
            entry["financial"] = fin_data
            entry["beta_data"] = beta_raw if isinstance(beta_raw, dict) else None
            entry["atr_data"] = atr_raw if isinstance(atr_raw, dict) else None
            current_price = fin_data.get("current_price")
            entry_price = pos.get("entry_price")
            if current_price and entry_price and float(entry_price) > 0:
                entry["pnl_pct"] = (float(current_price) - float(entry_price)) / float(entry_price)
            enriched.append(entry)

        if isinstance(corr_data, dict):
            for entry in enriched:
                entry["_corr_data"] = corr_data
        return enriched

    def _fmt_beta(self, beta_data: dict | None) -> str:
        if not beta_data:
            return "N/A"
        beta = beta_data.get("beta")
        r2 = beta_data.get("r2")
        bench = beta_data.get("benchmark", "SPY")
        if beta is None:
            return "N/A"
        r2_note = f"R²={r2:.2f}" if r2 is not None else ""
        warn = " ⚠️低置信" if r2 is not None and r2 < 0.1 else ""
        return f"{beta:.2f}x vs {bench}（{r2_note}{warn}）"

    def _fmt_atr(self, atr_data: dict | None, entry_price) -> str:
        if not atr_data:
            return "N/A"
        sl = atr_data.get("stop_loss")
        tp = atr_data.get("take_profit")
        atr = atr_data.get("atr")
        valid = atr_data.get("entry_valid")
        parts = []
        if sl is not None:
            parts.append(f"止损 {sl}")
        if tp is not None:
            parts.append(f"目标 {tp}")
        if atr is not None:
            parts.append(f"ATR={atr}")
        if valid is not None:
            parts.append("价格>MA20✓" if valid else "价格<MA20⚠️")
        return " | ".join(parts) if parts else "N/A"

    def _build_prompt(self, positions: list[dict], rag_context: str = "") -> str:
        with open(_PROMPT_FILE, encoding="utf-8") as f:
            template = f.read()

        data_fetch_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        lines = []
        for p in positions:
            fin = p.get("financial", {})
            pnl = p.get("pnl_pct")
            pnl_str = f"{pnl * 100:+.1f}%" if pnl is not None else "N/A"
            beta_str = self._fmt_beta(p.get("beta_data"))
            atr_str = self._fmt_atr(p.get("atr_data"), p.get("entry_price"))
            fetch_note = fin.get("fetched_at", data_fetch_ts)
            lines.append(
                f"- **{p['ticker']}** ({p.get('asset_name', '')}): "
                f"成本 {p.get('entry_price', 'N/A')} | 盈亏 {pnl_str} | "
                f"Beta {beta_str} | "
                f"ATR止损位: {atr_str} | "
                f"毛利率 {fin.get('gross_margin', 'N/A')} | "
                f"Forward PE {fin.get('pe_forward', 'N/A')} | "
                f"营收增速 {fin.get('revenue_growth', 'N/A')} | "
                f"财务数据抓取时间: {fetch_note}"
            )

        positions_text = "\n".join(lines)
        weighted_beta_str = self._calc_weighted_beta(positions)
        corr_data = positions[0].get("_corr_data", {}) if positions else {}
        corr_text = self._fmt_corr(corr_data)
        rag_text = rag_context if rag_context else "（RAG 服务未配置或暂无相关新闻）"

        return (
            template
            .replace("{positions_data}", positions_text)
            .replace("{count}", str(len(positions)))
            .replace("{weighted_beta}", weighted_beta_str)
            .replace("{correlation_matrix}", corr_text)
            .replace("{rag_news_context}", rag_text)
        )

    def _calc_weighted_beta(self, positions: list[dict]) -> str:
        """市值加权 Beta（quantity × current_price），R²<0.1 的持仓降权 50%"""
        weighted_sum = 0.0
        total_weight = 0.0
        valid_count = 0
        low_confidence = []

        for p in positions:
            beta_data = p.get("beta_data")
            if not beta_data or beta_data.get("beta") is None:
                continue
            beta = beta_data["beta"]
            r2 = beta_data.get("r2", 1.0)

            fin = p.get("financial", {})
            price = fin.get("current_price") or p.get("entry_price")
            qty = p.get("quantity", 1)
            if price is None:
                continue

            weight = float(qty) * float(price)
            # 低置信 beta 降权 50%
            if r2 < 0.1:
                weight *= 0.5
                low_confidence.append(p["ticker"])

            weighted_sum += beta * weight
            total_weight += weight
            valid_count += 1

        if total_weight == 0:
            return "N/A"

        avg = weighted_sum / total_weight
        note = f"市值加权，{valid_count}/{len(positions)} 有效"
        if low_confidence:
            note += f"，低置信降权: {', '.join(low_confidence)}"
        return f"{avg:.2f}x（{note}）"

    def _fmt_corr(self, corr_data: dict) -> str:
        pairs = corr_data.get("pairs", {})
        if not pairs:
            return "数据不足，无法计算"
        lines = []
        for k, v in sorted(pairs.items()):
            flag = " ⚠️ 高度相关" if abs(v) >= 0.7 else ""
            lines.append(f"  - {k}: {v:.2f}{flag}")
        return "\n".join(lines) if lines else "暂无数据"

    def _build_news_context(self, rag_results: list[dict], tavily_results: list[dict]) -> str:
        """RAG 优先，RAG 空时用 Tavily 兜底，两者均空时返回空字符串"""
        if rag_results:
            return rag_client.fmt_news_context(rag_results)
        if tavily_results:
            return "\n\n".join(
                f"**{r['title']}**（{r.get('published_date', '')}）\n{r['content'][:400]}"
                for r in tavily_results
            )
        return ""

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
