import os
import json
import sqlite3
import asyncio
from datetime import datetime, timedelta
from typing import Optional
import yfinance as yf

_tavily_client = None
_FMP_BASE = "https://financialmodelingprep.com"
_AV_BASE   = "https://www.alphavantage.co/query"

_TICKER_SUFFIX_RE = __import__("re").compile(r"\.(US|HK|SH|SZ)$", __import__("re").IGNORECASE)

# 内存缓存：进程内去重，避免同一次批量请求重复打 AV
_av_mem_cache: dict[str, dict] = {}
# AV 并发限制（免费版 5次/分钟，串行确保不超速）
_av_sem = asyncio.Semaphore(1)

# 财务数据持久化缓存 TTL（天）
_FIN_CACHE_TTL_DAYS = 3

# SQLite 缓存路径（复用 reports.db）
_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "reports.db")


def _fin_cache_init():
    """建表（如不存在）"""
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS financial_cache (
            ticker      TEXT PRIMARY KEY,
            data_json   TEXT NOT NULL,
            fetched_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _fin_cache_get(ticker: str) -> dict | None:
    """读缓存，过期返回 None"""
    try:
        conn = sqlite3.connect(_DB_PATH)
        row = conn.execute(
            "SELECT data_json, expires_at FROM financial_cache WHERE ticker = ?", (ticker,)
        ).fetchone()
        conn.close()
        if row and row[1] > datetime.utcnow().isoformat():
            return json.loads(row[0])
    except Exception:
        pass
    return None


def _fin_cache_set(ticker: str, data: dict):
    """写缓存，TTL 3天"""
    try:
        now = datetime.utcnow()
        expires = (now + timedelta(days=_FIN_CACHE_TTL_DAYS)).isoformat()
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            """INSERT INTO financial_cache (ticker, data_json, fetched_at, expires_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET
                   data_json=excluded.data_json,
                   fetched_at=excluded.fetched_at,
                   expires_at=excluded.expires_at""",
            (ticker, json.dumps(data), now.isoformat(), expires),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# 初始化建表
try:
    _fin_cache_init()
except Exception:
    pass


# ---------------------------------------------------------------------------
# Ticker 格式转换
# ---------------------------------------------------------------------------

def _fmp_ticker(ticker: str) -> str | None:
    """持仓 ticker → FMP profile 格式，不支持的返回 None"""
    t = ticker.upper()
    if t in ("BTC", "ETH") or t.endswith(".SH") or t.endswith(".SZ"):
        return None
    return _TICKER_SUFFIX_RE.sub("", t)


def _av_ticker(ticker: str) -> str | None:
    """持仓 ticker → Alpha Vantage OVERVIEW 格式，A 股 / 加密货币返回 None"""
    t = ticker.upper()
    if t in ("BTC", "ETH") or t.endswith(".SH") or t.endswith(".SZ"):
        return None
    return _TICKER_SUFFIX_RE.sub("", t)


def _yf_ticker(ticker: str) -> str:
    """持仓 ticker → yfinance 格式（仅用于 Beta/ATR，服务器可能 429）"""
    t = ticker.upper()
    if t == "BTC":
        return "BTC-USD"
    if t.endswith(".SH"):
        return t.replace(".SH", ".SS")
    if t.endswith(".SZ"):
        return t
    return _TICKER_SUFFIX_RE.sub("", t)


def _safe_float(v) -> float | None:
    if v is None or str(v).strip() in ("None", "N/A", "-", ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Tavily 搜索
# ---------------------------------------------------------------------------

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
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", ""),
                "published_date": date_raw[:10] if date_raw else "",
            })
        return results
    return await asyncio.get_event_loop().run_in_executor(None, _sync)


# ---------------------------------------------------------------------------
# Alpha Vantage — 财务快照（主力数据源）
# ---------------------------------------------------------------------------

async def _get_av_overview(av_sym: str, api_key: str) -> dict:
    """Alpha Vantage OVERVIEW，三层缓存：内存 → SQLite(3天) → 实时请求

    免费版限制：5次/分钟。并发限为1，每次请求后等待13秒确保不超速。
    """
    # 1. 内存缓存（同一进程内去重）
    if av_sym in _av_mem_cache:
        return _av_mem_cache[av_sym]

    # 2. SQLite 持久缓存（3天有效）
    cached = _fin_cache_get(av_sym)
    if cached:
        _av_mem_cache[av_sym] = cached
        return cached

    # 3. 实时请求 AV API
    import httpx
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

            # AV 限流时返回 {"Note": "..."} 或 {"Information": "..."}
            if data.get("Note") or data.get("Information"):
                if attempt == 0:
                    await asyncio.sleep(15)
                    continue
                data = {}
            break

        # 每次 API 请求后等 13 秒（5次/分钟限速保护）
        await asyncio.sleep(13)

    # 有效数据（有 Symbol 字段）才写入持久缓存
    if data.get("Symbol"):
        _fin_cache_set(av_sym, data)

    _av_mem_cache[av_sym] = data
    return data


async def _snapshot_from_av(ticker: str, av_data: dict, price: float | None,
                             fmp_price_data: dict) -> dict:
    """AV OVERVIEW + FMP price 组装财务快照"""
    # 毛利率 = GrossProfitTTM / RevenueTTM
    gp  = _safe_float(av_data.get("GrossProfitTTM"))
    rev = _safe_float(av_data.get("RevenueTTM"))
    gross_margin = round(gp / rev, 4) if gp and rev and rev > 0 else None

    current_price = price or fmp_price_data.get("price")

    return {
        "ticker":           ticker,
        "name":             av_data.get("Name") or fmp_price_data.get("name", ticker),
        "market_cap":       _safe_float(av_data.get("MarketCapitalization")) or fmp_price_data.get("market_cap"),
        "pe_ttm":           _safe_float(av_data.get("TrailingPE")),
        "pe_forward":       _safe_float(av_data.get("ForwardPE")),
        "gross_margin":     gross_margin,
        "operating_margin": _safe_float(av_data.get("OperatingMarginTTM")),
        "revenue_growth":   _safe_float(av_data.get("QuarterlyRevenueGrowthYOY")),
        "revenue_ttm":      _safe_float(av_data.get("RevenueTTM")),
        "sector":           av_data.get("Sector"),
        "industry":         av_data.get("Industry"),
        "current_price":    current_price,
        "52w_high":         _safe_float(av_data.get("52WeekHigh"))  or fmp_price_data.get("52w_high"),
        "52w_low":          _safe_float(av_data.get("52WeekLow"))   or fmp_price_data.get("52w_low"),
        "beta":             _safe_float(av_data.get("Beta")),
        "ma50":             _safe_float(av_data.get("50DayMovingAverage")),
        "ma200":            _safe_float(av_data.get("200DayMovingAverage")),
        "analyst_target":   _safe_float(av_data.get("AnalystTargetPrice")),
        "source":           "alphavantage",
        "fetched_at":       datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }


# ---------------------------------------------------------------------------
# FMP — 价格（免费 profile 接口，补充 AV 没价格的问题）
# ---------------------------------------------------------------------------

async def _get_fmp_price(fmp_sym: str, api_key: str) -> dict:
    """FMP profile 免费接口：返回 {price, market_cap, name, 52w_high, 52w_low}"""
    import httpx
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
                            w52_low  = float(parts[0])
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
    av_sym  = _av_ticker(ticker)
    fmp_sym = _fmp_ticker(ticker)

    # 并行拉取
    av_task  = _get_av_overview(av_sym, av_key)   if av_sym  and av_key  else asyncio.sleep(0, result={})
    fmp_task = _get_fmp_price(fmp_sym, fmp_key)   if fmp_sym and fmp_key else asyncio.sleep(0, result={})

    av_data, fmp_data = await asyncio.gather(av_task, fmp_task)

    # AV 返回有效数据（有 Symbol 字段）
    if av_data and av_data.get("Symbol"):
        return await _snapshot_from_av(ticker, av_data, None, fmp_data)

    # AV 无数据（A 股 / BTC / AV 不支持）：用 FMP 价格兜底
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


# ---------------------------------------------------------------------------
# 公共接口
# ---------------------------------------------------------------------------

async def get_batch_stock_data(tickers: list[str]) -> dict[str, dict]:
    """批量财务快照：AV OVERVIEW（财务）+ FMP profile（价格），并行拉取
    返回 {原始ticker → 数据dict}，失败的返回 {"ticker": t, "source": "unavailable"}
    """
    av_key  = os.getenv("ALPHA_VANTAGE_API_KEY", "")
    fmp_key = os.getenv("FMP_API_KEY", "")

    tasks = [_get_single_stock(t, av_key, fmp_key) for t in tickers]
    raw = await asyncio.gather(*tasks, return_exceptions=True)

    results: dict[str, dict] = {}
    for t, r in zip(tickers, raw):
        results[t] = r if isinstance(r, dict) else {"ticker": t, "source": "unavailable"}
    return results


async def get_stock_data(ticker: str) -> dict:
    """单 ticker 财务快照（保持向后兼容）"""
    r = await get_batch_stock_data([ticker])
    return r[ticker]


# ---------------------------------------------------------------------------
# Beta 计算（AV 提供现成 Beta，yfinance OLS 作为降级备用）
# ---------------------------------------------------------------------------

def _get_benchmark(ticker: str) -> str:
    t = ticker.upper()
    if t in ("BTC", "ETH") or t.endswith("-USD"):
        return "QQQ"
    if t.endswith(".HK"):
        return "^HSI"
    if t.endswith((".SH", ".SS", ".SZ")):
        code = t.split(".")[0]
        if code in ("518800", "518880", "159934"):
            return "GLD"
        return "510300.SS"
    _GOLD_US = {"IAUI", "GLD", "IAU", "GOLD", "SGOL", "AAAU"}
    base = _TICKER_SUFFIX_RE.sub("", t)
    if base in _GOLD_US:
        return "GLD"
    return "SPY"


async def get_beta(ticker: str, period: str = "1y", benchmark: str | None = None) -> Optional[dict]:
    """Beta 计算：优先从 AV OVERVIEW 直接取（已内存缓存），降级 yfinance OLS"""
    av_key = os.getenv("ALPHA_VANTAGE_API_KEY", "")
    av_sym = _av_ticker(ticker)

    if av_key and av_sym:
        av_data = await _get_av_overview(av_sym, av_key)
        beta_val = _safe_float(av_data.get("Beta"))
        if beta_val is not None:
            return {
                "beta":      beta_val,
                "r2":        None,
                "benchmark": "SPY",
                "n_obs":     None,
                "source":    "alphavantage",
            }

    # 降级：yfinance OLS（服务器可能 429，返回 None）
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
        extreme = (aligned.abs() > 0.3).any(axis=1)
        aligned = aligned[~extreme]
        if len(aligned) < 30:
            return None
        x = aligned.iloc[:, 1].values
        y = aligned.iloc[:, 0].values
        cov = np.cov(y, x)
        beta = cov[0, 1] / cov[1, 1]
        corr = np.corrcoef(y, x)[0, 1]
        return {
            "beta":      round(float(beta), 2),
            "r2":        round(float(corr ** 2), 3),
            "benchmark": _benchmark,
            "n_obs":     len(aligned),
            "source":    "yfinance_ols",
        }

    try:
        return await asyncio.get_event_loop().run_in_executor(None, _sync)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 相关性矩阵（yfinance，服务器 429 时优雅降级）
# ---------------------------------------------------------------------------

async def get_correlation_matrix(tickers: list[str], period: str = "1y") -> dict:
    """多 ticker 日收益率相关矩阵，yfinance 不可用时返回空"""
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

    try:
        return await asyncio.get_event_loop().run_in_executor(None, _sync)
    except Exception:
        return {"matrix": {}, "tickers": tickers}


# ---------------------------------------------------------------------------
# ATR 止损位（yfinance，服务器 429 时优雅降级）
# ---------------------------------------------------------------------------

async def get_atr_stops(
    ticker: str,
    entry_price: float,
    atr_period: int = 14,
    stop_multiplier: float = 2.5,
    target_multiplier: float = 3.0,
) -> Optional[dict]:
    """ATR(14) 动态止损/目标位，yfinance 不可用时返回 None"""
    def _sync():
        import pandas as pd
        yf_sym = _yf_ticker(ticker)
        df = yf.download(yf_sym, period="3mo", progress=False, auto_adjust=True)
        if df.empty or len(df) < atr_period + 5:
            return None
        high  = df["High"].squeeze()
        low   = df["Low"].squeeze()
        close = df["Close"].squeeze()
        prev  = close.shift(1)
        tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
        atr     = float(tr.rolling(atr_period).mean().iloc[-1])
        ma20    = float(close.rolling(20).mean().iloc[-1])
        current = float(close.iloc[-1])
        return {
            "atr":          round(atr, 4),
            "stop_loss":    round(entry_price - stop_multiplier  * atr, 4),
            "take_profit":  round(entry_price + target_multiplier * atr, 4),
            "entry_valid":  current > ma20,
            "ma20":         round(ma20, 4),
            "current_price": round(current, 4),
        }

    try:
        return await asyncio.get_event_loop().run_in_executor(None, _sync)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# AKShare — A 股基本信息
# ---------------------------------------------------------------------------

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
