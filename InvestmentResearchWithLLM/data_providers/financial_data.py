"""FMP 财务快照 — Starter plan ($29/mo)

全量使用 FMP stable 端点，不再依赖 yfinance 补充：
  /stable/profile              — price, market_cap, beta, 52w, sector, industry
  /stable/quote                — MA50, MA200, 日内高低, 成交量
  /stable/ratios-ttm           — PE(TTM), PEG, gross_margin, operating_margin, div_yield
  /stable/price-target-consensus — 分析师目标价（高/低/共识/中位）
  /stable/financial-growth     — 营收增速, EPS 增速（季度）
  /stable/discounted-cash-flow — DCF 估值
  /stable/key-metrics-ttm      — ROE, ROIC, earnings_yield
  /stable/income-statement     — 营收绝对值（计算 YoY 增速兜底）
"""
import os
import asyncio
from datetime import datetime
from typing import Optional

import httpx

from .ticker_utils import fmp_ticker, safe_float
from .cache import fin_cache_get, fin_cache_set

_FMP_BASE = "https://financialmodelingprep.com"
_SSL_VERIFY = os.getenv("SSL_VERIFY", "1") != "0"

_fmp_sem = asyncio.Semaphore(8)


async def _fmp_get(path: str, params: dict, client: httpx.AsyncClient) -> dict | list | None:
    async with _fmp_sem:
        try:
            r = await client.get(f"{_FMP_BASE}{path}", params=params, timeout=12.0)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
    return None


def _first(data) -> dict:
    if isinstance(data, list) and data:
        return data[0]
    return {}


async def _get_fmp_snapshot(ticker: str, fmp_key: str) -> dict:
    """FMP 全量快照：profile + quote + ratios-ttm + price-target + financial-growth"""
    fmp_sym = fmp_ticker(ticker)
    if not fmp_sym or not fmp_key:
        raise ValueError(f"No FMP support for {ticker}")

    params = {"symbol": fmp_sym, "apikey": fmp_key}

    async with httpx.AsyncClient(timeout=15.0, verify=_SSL_VERIFY) as c:
        profile_task = _fmp_get("/stable/profile", params, c)
        quote_task = _fmp_get("/stable/quote", params, c)
        ratios_task = _fmp_get("/stable/ratios-ttm", params, c)
        target_task = _fmp_get("/stable/price-target-consensus", params, c)
        growth_task = _fmp_get(
            "/stable/financial-growth",
            {"symbol": fmp_sym, "apikey": fmp_key, "limit": 1, "period": "quarter"},
            c,
        )
        dcf_task = _fmp_get("/stable/discounted-cash-flow", params, c)
        income_task = _fmp_get(
            "/stable/income-statement",
            {"symbol": fmp_sym, "apikey": fmp_key, "limit": 2},
            c,
        )

        profile_raw, quote_raw, ratios_raw, target_raw, growth_raw, dcf_raw, income_raw = (
            await asyncio.gather(
                profile_task, quote_task, ratios_task, target_task,
                growth_task, dcf_task, income_task,
            )
        )

    profile = _first(profile_raw)
    quote = _first(quote_raw)
    ratios = _first(ratios_raw)
    target = _first(target_raw)
    growth = _first(growth_raw)
    dcf = _first(dcf_raw)
    income_list = income_raw if isinstance(income_raw, list) else []

    if not profile.get("price") and not quote.get("price"):
        raise ValueError(f"FMP returned no data for {ticker}")

    revenue_growth = safe_float(growth.get("revenueGrowth")) or _calc_revenue_growth(income_list)

    w52_low, w52_high = None, None
    rng = profile.get("range", "")
    if rng and "-" in rng:
        parts = rng.split("-")
        if len(parts) == 2:
            w52_low = safe_float(parts[0])
            w52_high = safe_float(parts[1])

    price = safe_float(quote.get("price")) or safe_float(profile.get("price"))
    change_pct = safe_float(quote.get("changePercentage")) or safe_float(profile.get("changePercentage"))
    volume = safe_float(quote.get("volume")) or safe_float(profile.get("volume"))
    avg_volume = safe_float(profile.get("averageVolume"))

    vol_ratio = None
    if volume and avg_volume and avg_volume > 0:
        vol_ratio = round(volume / avg_volume, 2)

    ma50 = safe_float(quote.get("priceAvg50"))
    ma200 = safe_float(quote.get("priceAvg200"))

    gross_margin = safe_float(ratios.get("grossProfitMarginTTM"))
    if gross_margin and gross_margin > 1:
        gross_margin = None
    op_margin = safe_float(ratios.get("operatingProfitMarginTTM"))
    if op_margin and op_margin > 1:
        op_margin = None

    pe_ttm = safe_float(ratios.get("priceToEarningsRatioTTM"))
    peg_ratio = safe_float(ratios.get("priceToEarningsGrowthRatioTTM"))
    div_yield = safe_float(ratios.get("dividendYieldTTM"))

    # Forward PE: PE_TTM / (1 + eps_growth) — 仅在增速合理范围内计算
    pe_forward = None
    eps_growth = safe_float(growth.get("epsdilutedGrowth"))
    if pe_ttm and pe_ttm > 0 and eps_growth is not None and eps_growth > -0.5:
        denom = 1 + eps_growth
        if denom > 0.1:
            pe_forward = round(pe_ttm / denom, 1)

    last_div = safe_float(profile.get("lastDividend"))
    if div_yield is None and price and last_div and price > 0:
        div_yield = round(last_div * 4 / price, 4)

    analyst_target = safe_float(target.get("targetConsensus"))
    analyst_high = safe_float(target.get("targetHigh"))
    analyst_low = safe_float(target.get("targetLow"))
    analyst_median = safe_float(target.get("targetMedian"))

    dcf_value = safe_float(dcf.get("dcf"))

    eps_diluted_growth = safe_float(growth.get("epsdilutedGrowth"))

    merged = {
        "Symbol": fmp_sym,
        "price": price,
        "companyName": profile.get("companyName") or quote.get("name", fmp_sym),
        "marketCap": safe_float(quote.get("marketCap")) or safe_float(profile.get("marketCap")),
        "beta": safe_float(profile.get("beta")),
        "sector": profile.get("sector"),
        "industry": profile.get("industry"),
        "52w_high": w52_high or safe_float(quote.get("yearHigh")),
        "52w_low": w52_low or safe_float(quote.get("yearLow")),
        "change": safe_float(quote.get("change")) or safe_float(profile.get("change")),
        "changePercentage": change_pct,
        "volume": volume,
        "averageVolume": avg_volume,
        "lastDividend": last_div,
        "isEtf": profile.get("isEtf", False),
        "isAdr": profile.get("isAdr", False),
        "peRatioTTM": pe_ttm,
        "peForward": pe_forward,
        "pegRatio": peg_ratio,
        "grossProfitMarginTTM": gross_margin,
        "operatingProfitMarginTTM": op_margin,
        "revenue_growth": revenue_growth,
        "eps_growth": eps_diluted_growth,
        "ma50": ma50,
        "ma200": ma200,
        "analyst_target": analyst_target,
        "analyst_high": analyst_high,
        "analyst_low": analyst_low,
        "analyst_median": analyst_median,
        "dcf": dcf_value,
        "div_yield": div_yield,
        "vol_ratio": vol_ratio,
    }

    fin_cache_set(fmp_sym, merged)
    return _build_snapshot(ticker, merged)


def _calc_revenue_growth(income_list: list) -> float | None:
    if len(income_list) < 2:
        return None
    curr = safe_float(income_list[0].get("revenue"))
    prev = safe_float(income_list[1].get("revenue"))
    if curr and prev and prev > 0:
        return round((curr - prev) / prev, 4)
    return None


def _build_snapshot(ticker: str, data: dict) -> dict:
    gross_margin = safe_float(data.get("grossProfitMarginTTM"))
    if gross_margin and gross_margin > 1:
        gross_margin = None
    op_margin = safe_float(data.get("operatingProfitMarginTTM"))
    if op_margin and op_margin > 1:
        op_margin = None

    price = safe_float(data.get("price"))
    div_yield = safe_float(data.get("div_yield"))
    if div_yield is None:
        last_div = safe_float(data.get("lastDividend"))
        if price and last_div and price > 0:
            div_yield = round(last_div * 4 / price, 4)

    vol_ratio = safe_float(data.get("vol_ratio"))
    if vol_ratio is None:
        volume = safe_float(data.get("volume"))
        avg_volume = safe_float(data.get("averageVolume"))
        if volume and avg_volume and avg_volume > 0:
            vol_ratio = round(volume / avg_volume, 2)

    return {
        "ticker": ticker,
        "name": data.get("companyName", ticker),
        "market_cap": safe_float(data.get("marketCap")),
        "pe_ttm": safe_float(data.get("peRatioTTM")),
        "pe_forward": safe_float(data.get("peForward")),
        "peg_ratio": safe_float(data.get("pegRatio")),
        "gross_margin": gross_margin,
        "operating_margin": op_margin,
        "revenue_growth": safe_float(data.get("revenue_growth")),
        "eps_growth": safe_float(data.get("eps_growth")),
        "revenue_ttm": None,
        "sector": data.get("sector"),
        "industry": data.get("industry"),
        "current_price": price,
        "change_pct": safe_float(data.get("changePercentage")),
        "volume": safe_float(data.get("volume")),
        "avg_volume": safe_float(data.get("averageVolume")),
        "vol_ratio": vol_ratio,
        "div_yield": div_yield,
        "is_etf": data.get("isEtf", False),
        "is_adr": data.get("isAdr", False),
        "52w_high": safe_float(data.get("52w_high")),
        "52w_low": safe_float(data.get("52w_low")),
        "beta": safe_float(data.get("beta")),
        "ma50": safe_float(data.get("ma50")),
        "ma200": safe_float(data.get("ma200")),
        "analyst_target": safe_float(data.get("analyst_target")),
        "analyst_high": safe_float(data.get("analyst_high")),
        "analyst_low": safe_float(data.get("analyst_low")),
        "analyst_median": safe_float(data.get("analyst_median")),
        "dcf": safe_float(data.get("dcf")),
        "source": "fmp",
        "fetched_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }


async def get_batch_stock_data(tickers: list[str]) -> dict[str, dict]:
    """批量财务快照 — 全部使用 FMP stable 端点，无 yfinance 依赖"""
    fmp_key = os.getenv("FMP_API_KEY", "")
    tasks = [_get_single_stock(t, fmp_key) for t in tickers]
    raw = await asyncio.gather(*tasks, return_exceptions=True)

    results: dict[str, dict] = {}
    for t, r in zip(tickers, raw):
        results[t] = r if isinstance(r, dict) else {"ticker": t, "source": "unavailable"}

    return results


async def _get_single_stock(ticker: str, fmp_key: str) -> dict:
    return await _get_fmp_snapshot(ticker, fmp_key)


async def get_stock_data(ticker: str) -> dict:
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
    """FMP profile 的 Beta 作为兜底"""
    fmp_key = os.getenv("FMP_API_KEY", "")
    fmp_sym = fmp_ticker(ticker)
    if not (fmp_key and fmp_sym):
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=_SSL_VERIFY) as c:
            data = await _fmp_get("/stable/profile", {"symbol": fmp_sym, "apikey": fmp_key}, c)
            if isinstance(data, list) and data:
                beta_val = safe_float(data[0].get("beta"))
                if beta_val is not None:
                    return {
                        "beta": beta_val,
                        "r2": None,
                        "benchmark": "SPY",
                        "n_obs": None,
                        "period": "unknown",
                        "source": "fmp_profile",
                    }
    except Exception:
        pass
    return None
