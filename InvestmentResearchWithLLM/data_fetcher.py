import os
import asyncio
from typing import Optional
import yfinance as yf

_tavily_client = None
_FMP_BASE = "https://financialmodelingprep.com"

_TICKER_SUFFIX_RE = __import__("re").compile(r"\.(US|HK|SH|SZ)$", __import__("re").IGNORECASE)

def _fmp_ticker(ticker: str) -> str | None:
    """把持仓 ticker 转成 FMP 支持的格式，不支持的返回 None"""
    # A股和BTC/加密货币 FMP 不支持
    if ticker.endswith(".SH") or ticker.endswith(".SZ") or ticker.upper() in ("BTC", "ETH"):
        return None
    # 去掉 .US 后缀
    return _TICKER_SUFFIX_RE.sub("", ticker)


def _yf_ticker(ticker: str) -> str:
    """把持仓 ticker 转成 yfinance 格式"""
    t = ticker.upper()
    if t == "BTC":
        return "BTC-USD"
    # A股：600549.SH → 600549.SS，000001.SZ → 000001.SZ
    if t.endswith(".SH"):
        return t.replace(".SH", ".SS")
    if t.endswith(".SZ"):
        return t  # yfinance 直接支持
    # 去掉 .US 后缀
    return _TICKER_SUFFIX_RE.sub("", t)


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


def _parse_fmp_record(ticker: str, profile: dict, ratios: dict, growth: dict, estimates_data) -> dict:
    """从 FMP 各接口数据组装单 ticker 结果"""
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

    current_price = profile.get("price")
    forward_pe = None
    try:
        from datetime import date
        today = date.today().isoformat()
        if isinstance(estimates_data, list) and current_price:
            # 只取未来财年的预测（date > today），避免把历史 EPS 误用为 Forward EPS
            future = [
                e for e in estimates_data
                if isinstance(e, dict)
                and (e.get("date") or "") > today
                and (e.get("epsAvg") or 0) > 0
            ]
            if future:
                eps_fwd = future[0].get("epsAvg")
                if eps_fwd and eps_fwd > 0:
                    forward_pe = round(float(current_price) / float(eps_fwd), 2)
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


async def _get_batch_stock_data_fmp(tickers: list[str], api_key: str) -> dict[str, dict]:
    """FMP 批量拉取：把所有支持的 ticker 合并成 4 个请求（原 ticker → result）"""
    import httpx

    sym_map = {}  # fmp_sym → original_ticker
    for t in tickers:
        s = _fmp_ticker(t)
        if s:
            sym_map[s] = t

    if not sym_map:
        return {}

    symbols_str = ",".join(sym_map.keys())
    base_params = {"symbol": symbols_str, "apikey": api_key}

    def _by_sym(data: list) -> dict:
        return {r["symbol"]: r for r in data if isinstance(r, dict) and r.get("symbol")}

    async with httpx.AsyncClient(timeout=20.0) as client:
        profile_r, ratios_r, growth_r, estimates_r = await asyncio.gather(
            client.get(f"{_FMP_BASE}/stable/profile", params=base_params),
            client.get(f"{_FMP_BASE}/stable/ratios-ttm", params=base_params),
            client.get(f"{_FMP_BASE}/stable/financial-growth", params={**base_params, "limit": 1}),
            client.get(f"{_FMP_BASE}/stable/analyst-estimates", params={**base_params, "period": "annual", "limit": 2}),
        )
        try:
            profiles = _by_sym(profile_r.json() if isinstance(profile_r.json(), list) else [])
            ratios_map = _by_sym(ratios_r.json() if isinstance(ratios_r.json(), list) else [])
            growth_map = _by_sym(growth_r.json() if isinstance(growth_r.json(), list) else [])
            est_raw = estimates_r.json()
            est_map: dict[str, list] = {}
            if isinstance(est_raw, list):
                for e in est_raw:
                    s = e.get("symbol", "")
                    est_map.setdefault(s, []).append(e)
        except Exception:
            return {}

    results = {}
    for fmp_sym, orig_ticker in sym_map.items():
        profile = profiles.get(fmp_sym, {})
        if not profile:
            continue
        results[orig_ticker] = _parse_fmp_record(
            orig_ticker,
            profile,
            ratios_map.get(fmp_sym, {}),
            growth_map.get(fmp_sym, {}),
            est_map.get(fmp_sym, []),
        )
    return results


async def _get_stock_data_fmp(ticker: str, api_key: str) -> dict:
    """FMP 单 ticker 财务快照（供 company_analyzer 等非批量场景使用）"""
    results = await _get_batch_stock_data_fmp([ticker], api_key)
    if ticker in results:
        return results[ticker]
    raise ValueError(f"FMP returned no data for {ticker}")


async def _get_stock_data_yfinance(ticker: str) -> dict:
    """yfinance 财务快照（降级备用），info + fast_info 双保险"""
    def _sync():
        yf_sym = _yf_ticker(ticker)
        t = yf.Ticker(yf_sym)
        info = t.info or {}

        # fast_info 兜底实时价格（info 有时为空）
        current_price = info.get("currentPrice") or info.get("regularMarketPrice")
        if not current_price:
            try:
                fi = t.fast_info
                current_price = getattr(fi, "last_price", None) or getattr(fi, "regularMarketPrice", None)
            except Exception:
                pass

        result = {
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
            "current_price": current_price,
            "52w_high": info.get("fiftyTwoWeekHigh"),
            "52w_low": info.get("fiftyTwoWeekLow"),
            "source": "yfinance",
        }
        if not current_price:
            raise ValueError(f"yfinance returned no price for {ticker}")
        return result
    return await asyncio.get_event_loop().run_in_executor(None, _sync)


async def get_batch_stock_data(tickers: list[str]) -> dict[str, dict]:
    """批量财务快照：优先 FMP 批量接口（4 个请求覆盖所有 ticker），降级逐个 yfinance
    返回 {原始ticker → 财务数据dict}，失败的 ticker 值为 {"ticker": t, "source": "unavailable"}
    """
    fmp_key = os.getenv("FMP_API_KEY", "")
    results: dict[str, dict] = {}

    if fmp_key:
        try:
            results = await _get_batch_stock_data_fmp(tickers, fmp_key)
        except Exception:
            pass

    # 对 FMP 拿不到的 ticker 逐个用 yfinance 补
    missing = [t for t in tickers if t not in results]
    if missing:
        yf_results = await asyncio.gather(
            *[_get_stock_data_yfinance(t) for t in missing],
            return_exceptions=True,
        )
        for t, r in zip(missing, yf_results):
            results[t] = r if isinstance(r, dict) else {"ticker": t, "source": "unavailable"}

    # 确保每个 ticker 都有记录
    for t in tickers:
        if t not in results:
            results[t] = {"ticker": t, "source": "unavailable"}
    return results


async def get_stock_data(ticker: str) -> dict:
    """单 ticker 财务快照（保持向后兼容）"""
    r = await get_batch_stock_data([ticker])
    return r[ticker]


def _get_benchmark(ticker: str) -> str:
    """根据 ticker 类型选择合适的基准指数"""
    t = ticker.upper()
    if t in ("BTC", "ETH") or t.endswith("-USD"):
        return "QQQ"           # 加密货币与纳指相关性更高
    if t.endswith(".HK"):
        return "^HSI"          # 港股 → 恒生指数
    if t.endswith((".SH", ".SS", ".SZ")):
        return "510300.SS"     # A股 → 沪深300 ETF
    return "SPY"


async def get_beta(ticker: str, period: str = "1y", benchmark: str | None = None) -> Optional[dict]:
    """OLS beta vs 适配基准，返回 {beta, r2, benchmark, n_obs} 或 None

    R² < 0.1 时 beta 无统计意义，调用方应降权处理。
    """
    _benchmark = benchmark or _get_benchmark(ticker)

    def _sync():
        import numpy as np
        import pandas as pd
        yf_sym = _yf_ticker(ticker)
        stock_df = yf.download(yf_sym, period=period, progress=False, auto_adjust=True)
        bench_df = yf.download(_benchmark, period=period, progress=False, auto_adjust=True)
        if stock_df.empty or bench_df.empty:
            return None
        stock_ret = stock_df["Close"].squeeze().pct_change().dropna()
        bench_ret = bench_df["Close"].squeeze().pct_change().dropna()
        aligned = pd.concat([stock_ret, bench_ret], axis=1).dropna()
        # 过滤异常收益（数据错误导致失真）
        extreme = (aligned.abs() > 0.3).any(axis=1)
        aligned = aligned[~extreme]
        if len(aligned) < 30:
            return None
        x = aligned.iloc[:, 1].values
        y = aligned.iloc[:, 0].values
        cov_matrix = np.cov(y, x)
        beta = cov_matrix[0, 1] / cov_matrix[1, 1]
        # R² = corr²
        corr = np.corrcoef(y, x)[0, 1]
        r2 = round(float(corr ** 2), 3)
        return {
            "beta": round(float(beta), 2),
            "r2": r2,
            "benchmark": _benchmark,
            "n_obs": len(aligned),
        }
    return await asyncio.get_event_loop().run_in_executor(None, _sync)


async def get_correlation_matrix(tickers: list[str], period: str = "1y") -> dict:
    """计算多 ticker 间的日收益率相关矩阵，返回 {ticker_pair: corr} 和原始矩阵"""
    def _sync():
        import numpy as np
        import pandas as pd
        if len(tickers) < 2:
            return {"matrix": {}, "tickers": tickers}
        yf_tickers = [_yf_ticker(t) for t in tickers]
        raw = yf.download(yf_tickers, period=period, progress=False, auto_adjust=True)
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


async def get_atr_stops(
    ticker: str,
    entry_price: float,
    atr_period: int = 14,
    stop_multiplier: float = 2.5,
    target_multiplier: float = 3.0,
) -> Optional[dict]:
    """基于 ATR(14) 计算动态止损位和目标位

    返回 {atr, stop_loss, take_profit, entry_valid, ma20, current_price}
    entry_valid: 当前价 > 20日均线（动量确认）
    """
    def _sync():
        import numpy as np
        import pandas as pd
        yf_sym = _yf_ticker(ticker)
        df = yf.download(yf_sym, period="3mo", progress=False, auto_adjust=True)
        if df.empty or len(df) < atr_period + 5:
            return None
        high = df["High"].squeeze()
        low = df["Low"].squeeze()
        close = df["Close"].squeeze()
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = float(tr.rolling(atr_period).mean().iloc[-1])
        ma20 = float(close.rolling(20).mean().iloc[-1])
        current = float(close.iloc[-1])
        return {
            "atr": round(atr, 4),
            "stop_loss": round(entry_price - stop_multiplier * atr, 4),
            "take_profit": round(entry_price + target_multiplier * atr, 4),
            "entry_valid": current > ma20,
            "ma20": round(ma20, 4),
            "current_price": round(current, 4),
        }
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
