import os
import asyncio
from typing import Optional
import yfinance as yf

_tavily_client = None
_FMP_BASE = "https://financialmodelingprep.com"


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
            date_str = date_raw[:10] if date_raw else ""
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", ""),
                "published_date": date_str,
            })
        return results
    return await asyncio.get_event_loop().run_in_executor(None, _sync)


async def _get_stock_data_fmp(ticker: str, api_key: str) -> dict:
    """FMP 财务快照：4 个并发请求，数据来自 SEC 档案 + 卖方共识（stable API）"""
    import httpx
    params = {"symbol": ticker, "apikey": api_key}

    def _first(data) -> dict:
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return {}

    async with httpx.AsyncClient(timeout=15.0) as client:
        profile_r, ratios_r, growth_r, estimates_r = await asyncio.gather(
            client.get(f"{_FMP_BASE}/stable/profile", params=params),
            client.get(f"{_FMP_BASE}/stable/ratios-ttm", params=params),
            client.get(f"{_FMP_BASE}/stable/financial-growth", params={**params, "limit": 1}),
            client.get(f"{_FMP_BASE}/stable/analyst-estimates", params={**params, "period": "annual", "limit": 2}),
        )
        try:
            profile = _first(profile_r.json())
            ratios = _first(ratios_r.json())
            growth = _first(growth_r.json())
            estimates_data = estimates_r.json()
        except Exception as e:
            raise ValueError(f"FMP JSON parse error for {ticker}: {e}")

    if not profile:
        raise ValueError(f"FMP returned no profile for {ticker}")

    # 52-week range: profile.range 格式为 "low-high"
    week52_low, week52_high = None, None
    range_str = profile.get("range", "")
    if range_str and "-" in range_str:
        parts = range_str.split("-")
        if len(parts) == 2:
            try:
                week52_low = float(parts[0])
                week52_high = float(parts[1])
            except ValueError:
                pass

    # Forward PE = 当前价 / 最近一期卖方共识 EPS avg
    current_price = profile.get("price")
    forward_pe = None
    try:
        if isinstance(estimates_data, list):
            future = [e for e in estimates_data if isinstance(e, dict) and (e.get("epsAvg") or 0) > 0]
            if future and current_price:
                eps_fwd = future[0].get("epsAvg")
                if eps_fwd and eps_fwd > 0:
                    forward_pe = round(current_price / eps_fwd, 2)
    except Exception:
        pass

    return {
        "ticker": ticker,
        "name": profile.get("companyName", ticker),
        "market_cap": profile.get("marketCap"),
        "pe_ttm": ratios.get("priceToEarningsRatioTTM"),
        "pe_forward": forward_pe,
        "gross_margin": ratios.get("grossProfitMarginTTM"),
        "operating_margin": ratios.get("operatingProfitMarginTTM"),
        "revenue_growth": growth.get("revenueGrowth"),
        "revenue_ttm": ratios.get("revenuePerShareTTM"),
        "sector": profile.get("sector"),
        "industry": profile.get("industry"),
        "current_price": current_price,
        "52w_high": week52_high,
        "52w_low": week52_low,
        "source": "fmp",
    }


async def _get_stock_data_yfinance(ticker: str) -> dict:
    """yfinance 财务快照（降级备用）"""
    def _sync():
        t = yf.Ticker(ticker)
        info = t.info or {}
        return {
            "ticker": ticker,
            "name": info.get("longName") or info.get("shortName", ticker),
            "market_cap": info.get("marketCap"),
            "pe_ttm": info.get("trailingPE"),
            "pe_forward": info.get("forwardPE"),
            "gross_margin": info.get("grossMargins"),
            "operating_margin": info.get("operatingMargins"),
            "revenue_growth": info.get("revenueGrowth"),
            "revenue_ttm": info.get("totalRevenue"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "52w_high": info.get("fiftyTwoWeekHigh"),
            "52w_low": info.get("fiftyTwoWeekLow"),
            "source": "yfinance",
        }
    return await asyncio.get_event_loop().run_in_executor(None, _sync)


async def get_stock_data(ticker: str) -> dict:
    """财务快照：优先 FMP（SEC 档案 + 卖方共识），降级 yfinance"""
    fmp_key = os.getenv("FMP_API_KEY", "")
    if fmp_key:
        try:
            return await _get_stock_data_fmp(ticker, fmp_key)
        except Exception:
            pass
    return await _get_stock_data_yfinance(ticker)


async def get_beta(ticker: str, period: str = "1y", benchmark: str = "SPY") -> Optional[float]:
    """OLS beta vs benchmark (SPY 默认)，用 1 年日收益率计算"""
    def _sync():
        import numpy as np
        import pandas as pd
        stock_df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
        bench_df = yf.download(benchmark, period=period, progress=False, auto_adjust=True)
        if stock_df.empty or bench_df.empty:
            return None
        stock_ret = stock_df["Close"].squeeze().pct_change().dropna()
        bench_ret = bench_df["Close"].squeeze().pct_change().dropna()
        aligned = pd.concat([stock_ret, bench_ret], axis=1).dropna()
        if len(aligned) < 30:
            return None
        cov_matrix = np.cov(aligned.iloc[:, 0].values, aligned.iloc[:, 1].values)
        beta = cov_matrix[0, 1] / cov_matrix[1, 1]
        return round(float(beta), 2)
    return await asyncio.get_event_loop().run_in_executor(None, _sync)


async def get_correlation_matrix(tickers: list[str], period: str = "1y") -> dict:
    """计算多 ticker 间的日收益率相关矩阵，返回 {ticker_pair: corr} 和原始矩阵"""
    def _sync():
        import numpy as np
        import pandas as pd
        if len(tickers) < 2:
            return {"matrix": {}, "tickers": tickers}
        raw = yf.download(tickers, period=period, progress=False, auto_adjust=True)
        if raw.empty:
            return {"matrix": {}, "tickers": tickers}
        close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw
        returns = close.pct_change().dropna()
        corr = returns.corr().round(2)
        pairs = {}
        for i, t1 in enumerate(corr.columns):
            for j, t2 in enumerate(corr.columns):
                if i < j:
                    pairs[f"{t1}/{t2}"] = corr.loc[t1, t2]
        return {"matrix": corr.to_dict(), "pairs": pairs, "tickers": list(corr.columns)}
    return await asyncio.get_event_loop().run_in_executor(None, _sync)


async def get_cn_stock(code: str) -> dict:
    """AKShare A 股基本信息（代码如 000001 或 600519）"""
    def _sync():
        import akshare as ak
        try:
            df = ak.stock_individual_info_em(symbol=code)
            info = dict(zip(df.iloc[:, 0], df.iloc[:, 1]))
            return {"code": code, "data": info, "source": "akshare"}
        except Exception as e:
            return {"code": code, "error": str(e), "source": "akshare"}
    return await asyncio.get_event_loop().run_in_executor(None, _sync)
