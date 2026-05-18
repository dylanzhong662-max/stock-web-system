import asyncio
import os
import sqlite3
from datetime import datetime
from typing import AsyncGenerator

import data_fetcher
import rag_client
import report_generator
import predictions
import transaction_costs
import watchlist
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
        await self._save_predictions(content, enriched)
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
        await self._save_predictions(content, enriched)

    async def _save_predictions(self, content: str, enriched: list[dict]):
        entry_prices: dict[str, float] = {}
        for p in enriched:
            fin = p.get("financial", {}) or {}
            price = fin.get("current_price") or p.get("entry_price")
            if price:
                entry_prices[p["ticker"]] = float(price)
        try:
            await predictions.extract_via_llm(content, "portfolio", "latest", entry_prices)
        except Exception:
            pass
        # 自动提取监控清单
        try:
            await watchlist.extract_and_save(content)
        except Exception:
            pass

    async def _enrich_positions(self, positions: list[dict]) -> list[dict]:
        tickers = [p["ticker"] for p in positions]

        async def _safe_atr(pos: dict):
            ep = pos.get("entry_price")
            if ep is None:
                return None
            return await data_fetcher.get_atr_stops(pos["ticker"], float(ep))

        # 重量级计算（价格序列），FMP 无严格限速，30s 已足够；超时走降级（空 dict/None）
        async def _timed(coro, default, label):
            try:
                return await asyncio.wait_for(coro, timeout=30)
            except asyncio.TimeoutError:
                return default
            except Exception:
                return default

        fin_map, beta_results, factor_map, corr_data, atr_results = await asyncio.gather(
            data_fetcher.get_batch_stock_data(tickers),
            asyncio.gather(*[data_fetcher.get_beta(t) for t in tickers], return_exceptions=True),
            _timed(data_fetcher.get_batch_factor_exposures(tickers), {}, "factors"),
            _timed(data_fetcher.get_correlation_matrix(tickers), {}, "corr"),
            asyncio.gather(*[_safe_atr(p) for p in positions], return_exceptions=True),
            return_exceptions=True,
        )
        if not isinstance(fin_map, dict):
            fin_map = {}
        if not isinstance(factor_map, dict):
            factor_map = {}

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
            entry["factor_data"] = factor_map.get(pos["ticker"])
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
        period = beta_data.get("period", "3y")
        if beta is None:
            return "N/A"
        r2_note = f"R²={r2:.2f}" if r2 is not None else ""
        warn = " ⚠️低置信" if r2 is not None and r2 < 0.1 else ""
        return f"{beta:.2f}x vs {bench}（{period}，{r2_note}{warn}）"

    def _fmt_factors(self, factor_data: dict | None) -> str:
        if not factor_data:
            return "N/A（数据不可用）"
        parts = []
        for name, label in [("mkt", "Mkt"), ("smb", "SMB"), ("hml", "HML"), ("umd", "UMD")]:
            f = factor_data.get(name)
            if not f:
                continue
            sig = "*" if f.get("significant") else ""
            parts.append(f"{label}={f['beta']:+.2f}{sig}(t_NW={f['t_stat']:+.1f})")
        alpha = factor_data.get("alpha_annual")
        alpha_t = factor_data.get("alpha_t_stat")
        r2 = factor_data.get("r2")
        source = factor_data.get("source", "")
        tail = f" | α年化={alpha:+.2%}(t_NW={alpha_t:+.1f})" if alpha is not None else ""
        r2_note = f" | R²={r2:.2f}" if r2 is not None else ""
        src_tag = f" [{source}]" if source else ""
        return " ".join(parts) + tail + r2_note + f"  (* = |t_NW|>2 显著){src_tag}"

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
            factor_str = self._fmt_factors(p.get("factor_data"))
            atr_str = self._fmt_atr(p.get("atr_data"), p.get("entry_price"))
            fetch_note = fin.get("fetched_at", data_fetch_ts)
            lines.append(
                f"- **{p['ticker']}** ({p.get('asset_name', '')}): "
                f"成本 {p.get('entry_price', 'N/A')} | 盈亏 {pnl_str} | "
                f"Beta {beta_str} | "
                f"因子暴露: {factor_str} | "
                f"ATR止损位: {atr_str} | "
                f"毛利率 {fin.get('gross_margin', 'N/A')} | "
                f"Forward PE {fin.get('pe_forward', 'N/A')} | "
                f"营收增速 {fin.get('revenue_growth', 'N/A')} | "
                f"财务数据抓取时间: {fetch_note}"
            )

        positions_text = "\n".join(lines)
        weighted_beta_str = self._calc_weighted_beta(positions)
        portfolio_factor_str = self._calc_portfolio_factors(positions)
        corr_data = positions[0].get("_corr_data", {}) if positions else {}
        corr_text = self._fmt_corr(corr_data)
        rag_text = rag_context if rag_context else "（RAG 服务未配置或暂无相关新闻）"

        # 交易成本估算
        costs_data = transaction_costs.estimate_portfolio_costs(positions)
        costs_text = transaction_costs.format_cost_section(costs_data)

        # 监控清单上下文
        watchlist_ctx = watchlist.build_watchlist_context()

        rendered = (
            template
            .replace("{positions_data}", positions_text)
            .replace("{count}", str(len(positions)))
            .replace("{weighted_beta}", weighted_beta_str)
            .replace("{correlation_matrix}", corr_text)
            .replace("{rag_news_context}", rag_text)
            .replace("{transaction_costs}", costs_text)
            .replace("{watchlist_context}", watchlist_ctx)
        )
        if "{portfolio_factor_exposure}" in rendered:
            rendered = rendered.replace("{portfolio_factor_exposure}", portfolio_factor_str)
        else:
            rendered = rendered.replace(
                "- 持仓相关性矩阵",
                f"- 组合因子暴露（市值加权，3y OLS）：\n{portfolio_factor_str}\n- 持仓相关性矩阵",
                1,
            )
        return rendered

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
            r2 = beta_data.get("r2") if beta_data.get("r2") is not None else 1.0

            fin = p.get("financial", {})
            price = fin.get("current_price") or p.get("entry_price")
            qty = p.get("quantity", 1)
            if price is None:
                continue

            weight = float(qty) * float(price)
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

    def _calc_portfolio_factors(self, positions: list[dict]) -> str:
        """市值加权的组合 Fama-French 因子暴露"""
        factor_sums = {"mkt": 0.0, "smb": 0.0, "hml": 0.0, "umd": 0.0}
        total_weight = 0.0
        valid = 0

        for p in positions:
            fdata = p.get("factor_data")
            if not fdata:
                continue
            fin = p.get("financial", {})
            price = fin.get("current_price") or p.get("entry_price")
            qty = p.get("quantity", 1)
            if price is None:
                continue
            weight = float(qty) * float(price)
            for fname in factor_sums:
                f = fdata.get(fname)
                if f and f.get("beta") is not None:
                    factor_sums[fname] += f["beta"] * weight
            total_weight += weight
            valid += 1

        if total_weight == 0 or valid == 0:
            return "N/A（无可用多因子数据，可能 yfinance 429）"

        avgs = {k: v / total_weight for k, v in factor_sums.items()}
        # 组合风格解读
        interpret = []
        if abs(avgs["mkt"]) > 0:
            interpret.append("进攻型" if avgs["mkt"] > 1.1 else "防御型" if avgs["mkt"] < 0.9 else "市场型")
        if avgs["smb"] > 0.2:
            interpret.append("偏小盘")
        elif avgs["smb"] < -0.2:
            interpret.append("偏大盘")
        if avgs["hml"] > 0.2:
            interpret.append("偏价值")
        elif avgs["hml"] < -0.2:
            interpret.append("偏成长")
        if avgs["umd"] > 0.2:
            interpret.append("偏动量")
        elif avgs["umd"] < -0.2:
            interpret.append("偏反转")
        tag = "、".join(interpret) if interpret else "风格不明显"

        return (
            f"Mkt={avgs['mkt']:+.2f} | SMB={avgs['smb']:+.2f} | "
            f"HML={avgs['hml']:+.2f} | UMD={avgs['umd']:+.2f}  → **{tag}** "
            f"（{valid}/{len(positions)} 持仓有数据）"
        )

    def _fmt_corr(self, corr_data: dict) -> str:
        pairs = corr_data.get("pairs", {})
        tail_pairs = corr_data.get("tail_pairs", {})
        tail_days = corr_data.get("tail_days", 0)
        period = corr_data.get("period", "3y")

        if not pairs:
            return "数据不足，无法计算"

        lines = [f"  全样本相关性（{period} 日收益率 Pearson）："]
        for k, v in sorted(pairs.items()):
            flag = " ⚠️ 高度相关" if abs(v) >= 0.7 else ""
            lines.append(f"    - {k}: {v:+.2f}{flag}")

        if tail_pairs:
            lines.append(f"  尾部相关性（SPY 底部 10% 下跌日，n={tail_days}）：")
            for k, v in sorted(tail_pairs.items()):
                full = pairs.get(k)
                delta = f"（vs 全样本 {full:+.2f}，+{v - full:.2f}）" if full is not None else ""
                flag = " ⚠️ 分散化在危机时失效" if v >= 0.7 and (full is None or v - full >= 0.15) else ""
                lines.append(f"    - {k}: {v:+.2f}{delta}{flag}")
        else:
            lines.append("  尾部相关性：数据不足（<20 个尾部日）")

        return "\n".join(lines)

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
            max_tokens=12000,
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
