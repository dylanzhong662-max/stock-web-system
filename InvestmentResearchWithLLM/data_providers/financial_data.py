"""FMP 财务快照 — Starter plan ($29/mo)

主力接口：
  /stable/profile         — price, market_cap, beta, 52w, sector, industry
  /api/v3/ratios-ttm     — PE(TTM), PE(fwd), gross_margin, operating_margin
  /api/v3/income-statement — revenue growth YoY
"""
import os
import asyncio
from datetime import datetime
from typing import Optional

import httpx

from .ticker_utils import fmp_ticker, safe_float
from .cache import fin_cache_get, fin_cache_set

_FMP_BASE = "https://financialmodelingprep.com"

_fmp_sem = asyncio.Semaphore(5)


async def _fmp_get(path: str, params: dict, client: httpx.AsyncClient) -> dict | list | None:
    async with _fmp_sem:
        try:
            r = await client.get(f"{_FMP_BASE}{path}", params=params)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
    return None


async def _get_fmp_snapshot(ticker: str, fmp_key: str) -> dict:
    """FMP profile + ratios-ttm + income-statement → 完整财务快照"""
    fmp_sym = fmp_ticker(ticker)
    if not fmp_sym or not fmp_key:
        raise ValueError(f"No FMP support for {ticker}")

    params = {"symbol": fmp_sym, "apikey": fmp_key}

    cached = fin_cache_get(fmp_sym)
    if cached and cached.get("Symbol"):
        # 缓存命中但仍需刷新实时 price/change/volume（profile 接口轻量）
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                profile_raw = await _fmp_get("/stable/profile", params, c)
            profile = profile_raw[0] if isinstance(profile_raw, list) and profile_raw else {}
            if profile.get("price"):
                cached["price"] = profile["price"]
                cached["change"] = profile.get("change")
                cached["changePercentage"] = profile.get("changePercentage")
                cached["volume"] = profile.get("volume")
                cached["averageVolume"] = profile.get("averageVolume")
                cached["lastDividend"] = profile.get("lastDividend")
                rng = profile.get("range", "")
                if rng and "-" in rng:
                    parts = rng.split("-")
                    if len(parts) == 2:
                        cached["52w_high"] = safe_float(parts[1])
                        cached["52w_low"] = safe_float(parts[0])
        except Exception:
            pass
        return _build_snapshot(ticker, cached)

    async with httpx.AsyncClient(timeout=15.0) as c:
        profile_task = _fmp_get("/stable/profile", params, c)
        ratios_task = _fmp_get("/stable/ratios-ttm", params, c)
        income_task = _fmp_get(
            "/stable/income-statement",
            {"symbol": fmp_sym, "apikey": fmp_key, "limit": 2},
            c,
        )

        profile_raw, ratios_raw, income_raw = await asyncio.gather(
            profile_task, ratios_task, income_task
        )

    profile = profile_raw[0] if isinstance(profile_raw, list) and profile_raw else {}
    ratios = ratios_raw[0] if isinstance(ratios_raw, list) and ratios_raw else {}
    income_list = income_raw if isinstance(income_raw, list) else []

    if not profile.get("price") and not profile.get("companyName"):
        raise ValueError(f"FMP returned no data for {ticker}")

    revenue_growth = _calc_revenue_growth(income_list)

    w52_low, w52_high = None, None
    rng = profile.get("range", "")
    if rng and "-" in rng:
        parts = rng.split("-")
        if len(parts) == 2:
            w52_low = safe_float(parts[0])
            w52_high = safe_float(parts[1])

    merged = {
        "Symbol": fmp_sym,
        "price": profile.get("price"),
        "companyName": profile.get("companyName", fmp_sym),
        "marketCap": profile.get("marketCap") or profile.get("mktCap"),
        "beta": profile.get("beta"),
        "sector": profile.get("sector"),
        "industry": profile.get("industry"),
        "52w_high": w52_high,
        "52w_low": w52_low,
        "change": profile.get("change"),
        "changePercentage": profile.get("changePercentage"),
        "volume": profile.get("volume"),
        "averageVolume": profile.get("averageVolume"),
        "lastDividend": profile.get("lastDividend"),
        "isEtf": profile.get("isEtf", False),
        "isAdr": profile.get("isAdr", False),
        "peRatioTTM": ratios.get("priceToEarningsRatioTTM"),
        "peForward": None,
        "pegRatio": ratios.get("forwardPriceToEarningsGrowthRatioTTM"),
        "grossProfitMarginTTM": ratios.get("grossProfitMarginTTM"),
        "operatingProfitMarginTTM": ratios.get("operatingProfitMarginTTM"),
        "revenue_growth": revenue_growth,
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
    last_div = safe_float(data.get("lastDividend"))
    div_yield = None
    if price and last_div and price > 0:
        div_yield = round(last_div * 4 / price, 4)

    volume = safe_float(data.get("volume"))
    avg_volume = safe_float(data.get("averageVolume"))
    vol_ratio = None
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
        "revenue_ttm": None,
        "sector": data.get("sector"),
        "industry": data.get("industry"),
        "current_price": price,
        "change_pct": safe_float(data.get("changePercentage")),
        "volume": volume,
        "avg_volume": avg_volume,
        "vol_ratio": vol_ratio,
        "div_yield": div_yield,
        "is_etf": data.get("isEtf", False),
        "is_adr": data.get("isAdr", False),
        "52w_high": safe_float(data.get("52w_high")),
        "52w_low": safe_float(data.get("52w_low")),
        "beta": safe_float(data.get("beta")),
        "ma50": None,
        "ma200": None,
        "analyst_target": None,
        "source": "fmp",
        "fetched_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }


_yf_info_cache: dict[str, dict] = {}


async def _yf_supplement(snapshot: dict) -> dict:
    """yfinance 补充 FMP profile 缺失的 PE/毛利率/营收增速/MA/目标价"""
    from .ticker_utils import yf_ticker

    if not snapshot.get("current_price"):
        return snapshot
    if snapshot.get("is_etf"):
        return snapshot
    needs_supplement = (
        snapshot.get("pe_forward") is None
        or snapshot.get("ma50") is None
        or snapshot.get("analyst_target") is None
        or snapshot.get("pe_ttm") is None
        or snapshot.get("gross_margin") is None
    )
    if not needs_supplement:
        return snapshot

    ticker = snapshot["ticker"]
    yf_sym = yf_ticker(ticker)

    if yf_sym in _yf_info_cache:
        info = _yf_info_cache[yf_sym]
    else:
        def _sync():
            try:
                import yfinance as yf
                t = yf.Ticker(yf_sym)
                info = t.info or {}
                return info
            except Exception:
                return {}

        try:
            info = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _sync),
                timeout=10,
            )
        except (asyncio.TimeoutError, Exception):
            info = {}
        _yf_info_cache[yf_sym] = info

    if not info:
        return snapshot

    if snapshot.get("pe_ttm") is None:
        snapshot["pe_ttm"] = safe_float(info.get("trailingPE"))
    if snapshot.get("pe_forward") is None:
        snapshot["pe_forward"] = safe_float(info.get("forwardPE"))
    if snapshot.get("gross_margin") is None:
        gm = safe_float(info.get("grossMargins"))
        if gm and gm <= 1:
            snapshot["gross_margin"] = gm
    if snapshot.get("operating_margin") is None:
        om = safe_float(info.get("operatingMargins"))
        if om and om <= 1:
            snapshot["operating_margin"] = om
    if snapshot.get("revenue_growth") is None:
        snapshot["revenue_growth"] = safe_float(info.get("revenueGrowth"))
    if snapshot.get("ma50") is None:
        snapshot["ma50"] = safe_float(info.get("fiftyDayAverage"))
    if snapshot.get("ma200") is None:
        snapshot["ma200"] = safe_float(info.get("twoHundredDayAverage"))
    if snapshot.get("analyst_target") is None:
        snapshot["analyst_target"] = safe_float(info.get("targetMeanPrice"))

    snapshot["source"] = "fmp+yfinance"
    return snapshot


async def get_batch_stock_data(tickers: list[str]) -> dict[str, dict]:
    """批量财务快照 — FMP profile + yfinance 补充（顺序调用避免 429）"""
    fmp_key = os.getenv("FMP_API_KEY", "")
    tasks = [_get_single_stock(t, fmp_key) for t in tickers]
    raw = await asyncio.gather(*tasks, return_exceptions=True)

    results: dict[str, dict] = {}
    for t, r in zip(tickers, raw):
        results[t] = r if isinstance(r, dict) else {"ticker": t, "source": "unavailable"}

    for t in tickers:
        if results[t].get("current_price"):
            results[t] = await _yf_supplement(results[t])
            await asyncio.sleep(1.5)

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
        async with httpx.AsyncClient(timeout=10.0) as c:
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
