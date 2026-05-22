"""Alpha Vantage + FMP 财务快照"""
import os
import asyncio
from datetime import datetime
from typing import Optional

import httpx

from .ticker_utils import fmp_ticker, av_ticker, safe_float
from .cache import fin_cache_get, fin_cache_set

_FMP_BASE = "https://financialmodelingprep.com"
_AV_BASE = "https://www.alphavantage.co/query"

_av_mem_cache: dict[str, dict] = {}
_av_sem = asyncio.Semaphore(1)


async def _get_av_overview(av_sym: str, api_key: str) -> dict:
    """Alpha Vantage OVERVIEW，三层缓存：内存 → SQLite(3天) → 实时请求"""
    if av_sym in _av_mem_cache:
        return _av_mem_cache[av_sym]

    cached = fin_cache_get(av_sym)
    if cached:
        _av_mem_cache[av_sym] = cached
        return cached

    async with _av_sem:
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=15.0) as c:
                    r = await c.get(_AV_BASE, params={
                        "function": "OVERVIEW",
                        "symbol": av_sym,
                        "apikey": api_key,
                    })
                    data = r.json() if r.status_code == 200 else {}
            except Exception:
                data = {}

            if data.get("Note") or data.get("Information"):
                if attempt == 0:
                    await asyncio.sleep(15)
                    continue
                data = {}
            break

        await asyncio.sleep(13)

    if data.get("Symbol"):
        fin_cache_set(av_sym, data)

    _av_mem_cache[av_sym] = data
    return data


async def _snapshot_from_av(ticker: str, av_data: dict, price: float | None,
                            fmp_price_data: dict) -> dict:
    """AV OVERVIEW + FMP price 组装财务快照"""
    gp = safe_float(av_data.get("GrossProfitTTM"))
    rev = safe_float(av_data.get("RevenueTTM"))
    gross_margin = round(gp / rev, 4) if gp and rev and rev > 0 else None

    current_price = price or fmp_price_data.get("price")

    return {
        "ticker":           ticker,
        "name":             av_data.get("Name") or fmp_price_data.get("name", ticker),
        "market_cap":       safe_float(av_data.get("MarketCapitalization")) or fmp_price_data.get("market_cap"),
        "pe_ttm":           safe_float(av_data.get("TrailingPE")),
        "pe_forward":       safe_float(av_data.get("ForwardPE")),
        "gross_margin":     gross_margin,
        "operating_margin": safe_float(av_data.get("OperatingMarginTTM")),
        "revenue_growth":   safe_float(av_data.get("QuarterlyRevenueGrowthYOY")),
        "revenue_ttm":      safe_float(av_data.get("RevenueTTM")),
        "sector":           av_data.get("Sector"),
        "industry":         av_data.get("Industry"),
        "current_price":    current_price,
        "52w_high":         safe_float(av_data.get("52WeekHigh")) or fmp_price_data.get("52w_high"),
        "52w_low":          safe_float(av_data.get("52WeekLow")) or fmp_price_data.get("52w_low"),
        "beta":             safe_float(av_data.get("Beta")),
        "ma50":             safe_float(av_data.get("50DayMovingAverage")),
        "ma200":            safe_float(av_data.get("200DayMovingAverage")),
        "analyst_target":   safe_float(av_data.get("AnalystTargetPrice")),
        "source":           "alphavantage",
        "fetched_at":       datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }


async def _get_fmp_price(fmp_sym: str, api_key: str) -> dict:
    """FMP profile 免费接口：返回 {price, market_cap, name, 52w_high, 52w_low}"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{_FMP_BASE}/stable/profile",
                            params={"symbol": fmp_sym, "apikey": api_key})
            data = r.json()
            if isinstance(data, list) and data:
                p = data[0]
                w52_low, w52_high = None, None
                rng = p.get("range", "")
                if rng and "-" in rng:
                    parts = rng.split("-")
                    if len(parts) == 2:
                        try:
                            w52_low = float(parts[0])
                            w52_high = float(parts[1])
                        except ValueError:
                            pass
                return {
                    "price":      p.get("price"),
                    "market_cap": p.get("marketCap"),
                    "name":       p.get("companyName", fmp_sym),
                    "52w_high":   w52_high,
                    "52w_low":    w52_low,
                }
    except Exception:
        pass
    return {}


async def _get_single_stock(ticker: str, av_key: str, fmp_key: str) -> dict:
    """单 ticker 完整快照：AV OVERVIEW（财务）+ FMP profile（价格）并行"""
    av_sym = av_ticker(ticker)
    fmp_sym = fmp_ticker(ticker)

    av_task = _get_av_overview(av_sym, av_key) if av_sym and av_key else asyncio.sleep(0, result={})
    fmp_task = _get_fmp_price(fmp_sym, fmp_key) if fmp_sym and fmp_key else asyncio.sleep(0, result={})

    av_data, fmp_data = await asyncio.gather(av_task, fmp_task)

    if av_data and av_data.get("Symbol"):
        return await _snapshot_from_av(ticker, av_data, None, fmp_data)

    if fmp_data.get("price"):
        return {
            "ticker":        ticker,
            "name":          fmp_data.get("name", ticker),
            "market_cap":    fmp_data.get("market_cap"),
            "pe_ttm":        None,
            "pe_forward":    None,
            "gross_margin":  None,
            "operating_margin": None,
            "revenue_growth": None,
            "revenue_ttm":   None,
            "sector":        None,
            "industry":      None,
            "current_price": fmp_data.get("price"),
            "52w_high":      fmp_data.get("52w_high"),
            "52w_low":       fmp_data.get("52w_low"),
            "beta":          None,
            "source":        "fmp_profile_only",
            "fetched_at":    datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        }

    raise ValueError(f"No data from AV or FMP for {ticker}")


async def get_batch_stock_data(tickers: list[str]) -> dict[str, dict]:
    """批量财务快照：AV OVERVIEW + FMP profile 并行拉取"""
    av_key = os.getenv("ALPHA_VANTAGE_API_KEY", "")
    fmp_key = os.getenv("FMP_API_KEY", "")

    tasks = [_get_single_stock(t, av_key, fmp_key) for t in tickers]
    raw = await asyncio.gather(*tasks, return_exceptions=True)

    results: dict[str, dict] = {}
    for t, r in zip(tickers, raw):
        results[t] = r if isinstance(r, dict) else {"ticker": t, "source": "unavailable"}
    return results


async def get_stock_data(ticker: str) -> dict:
    """单 ticker 财务快照"""
    r = await get_batch_stock_data([ticker])
    return r[ticker]


async def get_cn_stock(code: str) -> dict:
    """AKShare A 股基本信息"""
    def _sync():
        import akshare as ak
        try:
            df = ak.stock_individual_info_em(symbol=code)
            return {"code": code, "data": dict(zip(df.iloc[:, 0], df.iloc[:, 1])), "source": "akshare"}
        except Exception as e:
            return {"code": code, "error": str(e), "source": "akshare"}
    return await asyncio.get_event_loop().run_in_executor(None, _sync)


async def fallback_overview_beta(ticker: str) -> Optional[dict]:
    """AV OVERVIEW 的 Beta（黑盒兜底）"""
    av_key = os.getenv("ALPHA_VANTAGE_API_KEY", "")
    av_sym = av_ticker(ticker)
    if not (av_key and av_sym):
        return None
    av_data = await _get_av_overview(av_sym, av_key)
    beta_val = safe_float(av_data.get("Beta"))
    if beta_val is None:
        return None
    return {
        "beta":      beta_val,
        "r2":        None,
        "benchmark": "SPY",
        "n_obs":     None,
        "period":    "unknown",
        "source":    "alphavantage_overview",
    }
