import os
import json
import sqlite3
import asyncio
from datetime import datetime, timedelta
from typing import Optional

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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history_cache (
            ticker      TEXT PRIMARY KEY,
            series_json TEXT NOT NULL,   -- {"2024-01-02": 185.6, ...}
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


# ---------------------------------------------------------------------------
# 价格序列：Alpha Vantage TIME_SERIES_DAILY_ADJUSTED
# 作为 yfinance 在服务器 429 时的替代品。
# 免费 AV 限速 5次/分钟，所以序列缓存 24h；全部查询共享内存 + SQLite 缓存。
# ---------------------------------------------------------------------------

_PRICE_CACHE_TTL_HOURS = 24
_price_mem_cache: dict[str, dict] = {}  # {ticker: {"YYYY-MM-DD": close}}


def _price_cache_get(ticker: str) -> dict | None:
    if ticker in _price_mem_cache:
        return _price_mem_cache[ticker]
    try:
        conn = sqlite3.connect(_DB_PATH)
        row = conn.execute(
            "SELECT series_json, expires_at FROM price_history_cache WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        conn.close()
        if row and row[1] > datetime.utcnow().isoformat():
            series = json.loads(row[0])
            _price_mem_cache[ticker] = series
            return series
    except Exception:
        pass
    return None


def _price_cache_set(ticker: str, series: dict):
    try:
        now = datetime.utcnow()
        expires = (now + timedelta(hours=_PRICE_CACHE_TTL_HOURS)).isoformat()
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            """INSERT INTO price_history_cache (ticker, series_json, fetched_at, expires_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET
                   series_json=excluded.series_json,
                   fetched_at=excluded.fetched_at,
                   expires_at=excluded.expires_at""",
            (ticker, json.dumps(series), now.isoformat(), expires),
        )
        conn.commit()
        conn.close()
        _price_mem_cache[ticker] = series
    except Exception:
        pass


# 进程级去重：同一 ticker 的并发请求共享同一 future
_av_daily_inflight: dict[str, asyncio.Future] = {}


async def _get_akshare_daily(ticker: str) -> dict:
    """AKShare 日线（新浪数据源，无 key、国内直连、免费）

    覆盖美股 + ETF，比 FMP 免费版更全。ADR 冷门标的可能不支持 → 返回 {}
    """
    fmp_sym = _fmp_ticker(ticker)
    if not fmp_sym:
        return {}

    def _sync():
        try:
            import akshare as ak
            df = ak.stock_us_daily(symbol=fmp_sym, adjust="qfq")
            if df is None or df.empty:
                return {}
            series: dict[str, float] = {}
            for _, row in df.iterrows():
                date = row["date"]
                # 兼容 Timestamp 和 str
                date_str = date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else str(date)[:10]
                try:
                    series[date_str] = float(row["close"])
                except (TypeError, ValueError, KeyError):
                    continue
            return series
        except Exception:
            return {}

    return await asyncio.get_event_loop().run_in_executor(None, _sync)


async def _get_av_daily(ticker: str, outputsize: str = "full") -> dict:
    """日线价格 → {date: close}，24h 缓存。

    数据源优先级：
      1. AKShare (新浪)：免费、国内直连、覆盖美股/ETF
      2. FMP：作为 fallback（对 BESIY 等冷门 ADR 可能有用）

    A 股（.SH/.SZ）/ 加密货币不支持 → 返回 {}
    进程级去重：并发请求同一 ticker 只打一次外部 API
    """
    fmp_sym = _fmp_ticker(ticker)
    if not fmp_sym:
        return {}

    cached = _price_cache_get(fmp_sym)
    if cached is not None:
        return cached

    if fmp_sym in _av_daily_inflight:
        try:
            return await _av_daily_inflight[fmp_sym]
        except Exception:
            pass

    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    _av_daily_inflight[fmp_sym] = fut

    try:
        # 1) 首选：AKShare 新浪（国内直连，免费）
        series = await _get_akshare_daily(ticker)

        # 2) Fallback：FMP（对 BESIY 等冷门 ADR 可能有用）
        if not series:
            fmp_key = os.getenv("FMP_API_KEY", "")
            if fmp_key:
                import httpx
                try:
                    async with httpx.AsyncClient(timeout=30.0) as c:
                        r = await c.get(
                            f"{_FMP_BASE}/stable/historical-price-eod/full",
                            params={"symbol": fmp_sym, "apikey": fmp_key},
                        )
                        data = r.json() if r.status_code == 200 else []
                except Exception:
                    data = []
                if isinstance(data, list):
                    fmp_series: dict[str, float] = {}
                    for row in data:
                        date = row.get("date")
                        close = row.get("adjClose") or row.get("close")
                        if date and close is not None:
                            try:
                                fmp_series[date] = float(close)
                            except (TypeError, ValueError):
                                continue
                    if fmp_series:
                        series = fmp_series

        if series:
            _price_cache_set(fmp_sym, series)
        else:
            _price_mem_cache[fmp_sym] = {}
        if not fut.done():
            fut.set_result(series)
        return series
    except Exception as e:
        if not fut.done():
            fut.set_exception(e)
        raise
    finally:
        _av_daily_inflight.pop(fmp_sym, None)


async def _get_price_series(tickers: list[str]) -> "object":
    """批量拉价格序列 → pandas DataFrame
    列名为 _fmp_ticker() 后的干净 symbol（如 'AMD'），索引为日期
    """
    import pandas as pd
    seq = await asyncio.gather(*[_get_av_daily(t) for t in tickers], return_exceptions=True)
    frames = {}
    for t, s in zip(tickers, seq):
        if isinstance(s, dict) and s:
            key = _fmp_ticker(t) or t
            frames[key] = pd.Series(s).sort_index()
    if not frames:
        return pd.DataFrame()
    df = pd.DataFrame(frames)
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    return df


# Fama-French factor proxies (ETF 代理，作为 Kenneth French 数据不可用时的 fallback)
_FF_PROXIES = {
    "market": "SPY",
    "smb_long": "IWM", "smb_short": "SPY",
    "hml_long": "IWD", "hml_short": "IWF",
    "umd_long": "MTUM", "umd_short": "SPY",
}

# Kenneth French 真实因子数据缓存
_ff_factor_cache: dict[str, "object"] = {}  # {"daily": pd.DataFrame}
_FF_DATA_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_Factors_daily_CSV.zip"
_MOM_DATA_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Momentum_Factor_daily_CSV.zip"


def _parse_ff_csv(text: str) -> "pd.DataFrame":
    """解析 Kenneth French CSV（跳过头部描述行）"""
    import pandas as pd
    import io
    lines = text.strip().split("\n")
    start = None
    for i, line in enumerate(lines):
        if line.strip() and line.strip()[0].isdigit():
            start = i
            break
    if start is None:
        return pd.DataFrame()
    data_lines = []
    for line in lines[start:]:
        if not line.strip() or not line.strip()[0].isdigit():
            break
        data_lines.append(line)
    if not data_lines:
        return pd.DataFrame()
    csv_text = "\n".join(data_lines)
    df = pd.read_csv(io.StringIO(csv_text), header=None)
    return df


async def _get_ff_factors() -> "pd.DataFrame | None":
    """下载 Kenneth French 日度因子数据（Mkt-RF, SMB, HML, UMD, RF）
    缓存在内存中，进程生命周期内只下载一次。
    """
    import pandas as pd
    if "daily" in _ff_factor_cache:
        cached = _ff_factor_cache["daily"]
        if cached is not None and not cached.empty:
            return cached

    import httpx
    import zipfile
    import io

    async def _download_zip(url: str) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
                r = await c.get(url)
                if r.status_code != 200:
                    return None
                zf = zipfile.ZipFile(io.BytesIO(r.content))
                csv_name = [n for n in zf.namelist() if n.endswith(".CSV") or n.endswith(".csv")]
                if not csv_name:
                    return None
                return zf.read(csv_name[0]).decode("utf-8", errors="ignore")
        except Exception:
            return None

    try:
        ff_text, mom_text = await asyncio.gather(
            _download_zip(_FF_DATA_URL),
            _download_zip(_MOM_DATA_URL),
        )

        if not ff_text:
            _ff_factor_cache["daily"] = None
            return None

        ff_df = _parse_ff_csv(ff_text)
        if ff_df.empty or ff_df.shape[1] < 4:
            _ff_factor_cache["daily"] = None
            return None

        ff_df.columns = ["date", "mkt_rf", "smb", "hml", "rf"][:ff_df.shape[1]]
        ff_df["date"] = ff_df["date"].astype(str).str.strip()
        ff_df = ff_df[ff_df["date"].str.len() == 8]
        ff_df.index = pd.to_datetime(ff_df["date"], format="%Y%m%d")
        for col in ["mkt_rf", "smb", "hml", "rf"]:
            if col in ff_df.columns:
                ff_df[col] = pd.to_numeric(ff_df[col], errors="coerce") / 100.0

        if mom_text:
            mom_df = _parse_ff_csv(mom_text)
            if not mom_df.empty and mom_df.shape[1] >= 2:
                mom_df.columns = ["date", "umd"][:mom_df.shape[1]]
                mom_df["date"] = mom_df["date"].astype(str).str.strip()
                mom_df = mom_df[mom_df["date"].str.len() == 8]
                mom_df.index = pd.to_datetime(mom_df["date"], format="%Y%m%d")
                mom_df["umd"] = pd.to_numeric(mom_df["umd"], errors="coerce") / 100.0
                ff_df = ff_df.join(mom_df[["umd"]], how="left")

        if "umd" not in ff_df.columns:
            ff_df["umd"] = float("nan")

        ff_df = ff_df.drop(columns=["date"], errors="ignore")
        _ff_factor_cache["daily"] = ff_df
        return ff_df
    except Exception:
        _ff_factor_cache["daily"] = None
        return None


# ---------------------------------------------------------------------------
# 数据质量审计
# ---------------------------------------------------------------------------

def audit_price_data(returns: "pd.DataFrame") -> dict:
    """检测价格序列质量问题，返回审计报告
    - extreme_returns: |日收益率| > 50% 的观测
    - stale_prices: 连续 5 天无变动
    - data_gaps: 缺失率
    """
    import pandas as pd
    issues = []
    flags_per_ticker: dict[str, list[str]] = {}

    for col in returns.columns:
        col_issues = []
        series = returns[col].dropna()
        if series.empty:
            continue

        extreme = series[series.abs() > 0.5]
        if len(extreme) > 0:
            col_issues.append(f"极端收益率({len(extreme)}天|r|>50%)")
            issues.append({
                "type": "extreme_returns",
                "ticker": col,
                "count": len(extreme),
                "dates": extreme.index[:3].strftime("%Y-%m-%d").tolist(),
            })

        zero_runs = (series == 0).astype(int)
        if len(zero_runs) >= 5:
            rolling_zeros = zero_runs.rolling(5).sum()
            stale_count = int((rolling_zeros >= 5).sum())
            if stale_count > 0:
                col_issues.append(f"价格停滞({stale_count}个5日窗口)")
                issues.append({
                    "type": "stale_prices",
                    "ticker": col,
                    "stale_windows": stale_count,
                })

        if col_issues:
            flags_per_ticker[col] = col_issues

    total_cells = returns.shape[0] * returns.shape[1]
    nan_count = int(returns.isna().sum().sum())
    gap_pct = nan_count / total_cells if total_cells > 0 else 0

    return {
        "issues": issues,
        "flags_per_ticker": flags_per_ticker,
        "gap_pct": round(gap_pct, 4),
        "n_tickers": returns.shape[1],
        "n_obs": returns.shape[0],
        "is_clean": len(issues) == 0 and gap_pct < 0.1,
    }


def _newey_west_se(X: "np.ndarray", residuals: "np.ndarray", n_lags: int | None = None) -> "np.ndarray":
    """Newey-West HAC 标准误，修正日度收益率自相关导致的 t-stat 虚高
    n_lags 默认 floor(4*(n/100)^(2/9))（Andrews 1991 建议）
    """
    import numpy as np
    n, k = X.shape
    if n_lags is None:
        n_lags = int(4 * (n / 100) ** (2.0 / 9.0))
    n_lags = max(1, n_lags)

    xtx_inv = np.linalg.inv(X.T @ X)
    S = np.zeros((k, k))
    e = residuals.reshape(-1, 1)
    xe = X * e  # n x k

    # lag 0
    S += xe.T @ xe

    # lag 1..n_lags (Bartlett kernel)
    for lag in range(1, n_lags + 1):
        weight = 1.0 - lag / (n_lags + 1.0)
        gamma = xe[lag:].T @ xe[:-lag]
        S += weight * (gamma + gamma.T)

    cov = xtx_inv @ S @ xtx_inv
    se = np.sqrt(np.maximum(np.diag(cov), 0))
    return se

_BETA_WINDOW = "3y"        # 3 年滚动窗口
_BETA_MIN_OBS = 200        # 至少 200 个日度观测值
# 注意：不再剔除 |r| > 0.3 的极端日——那恰好是系统性风险的真实暴露


async def get_beta(
    ticker: str,
    period: str = _BETA_WINDOW,
    benchmark: str | None = None,
) -> Optional[dict]:
    """3y Beta（vs 基准），数据源 = Alpha Vantage 日线（免服务器 429 风险）

    - 3y 窗口，保留所有尾部数据（不剔除极端日）
    - A股/加密货币不支持 AV 日线 → 降级 AV OVERVIEW 的黑盒 Beta
    """
    _benchmark = benchmark or _get_benchmark(ticker)
    import numpy as np
    import pandas as pd

    # FMP 不支持 A 股/加密：直接走 OVERVIEW Beta 兜底
    if not _fmp_ticker(ticker):
        return await _fallback_overview_beta(ticker)

    fmp_ticker = _fmp_ticker(ticker)
    fmp_bench = _fmp_ticker(_benchmark) or _benchmark

    df = await _get_price_series([ticker, _benchmark])
    if df.empty or fmp_ticker not in df.columns or fmp_bench not in df.columns:
        return await _fallback_overview_beta(ticker)

    cutoff = pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=365 * 3)
    df = df[df.index >= cutoff]
    returns = df[[fmp_ticker, fmp_bench]].pct_change().dropna()
    if len(returns) < _BETA_MIN_OBS:
        return await _fallback_overview_beta(ticker)

    y = returns[fmp_ticker].values
    x = returns[fmp_bench].values
    cov = np.cov(y, x)
    beta = cov[0, 1] / cov[1, 1]
    corr = np.corrcoef(y, x)[0, 1]
    return {
        "beta":      round(float(beta), 2),
        "r2":        round(float(corr ** 2), 3),
        "benchmark": _benchmark,
        "n_obs":     len(returns),
        "period":    period,
        "source":    "fmp_daily_3y",
    }


async def _fallback_overview_beta(ticker: str) -> Optional[dict]:
    """AV OVERVIEW 的 Beta（黑盒兜底）"""
    av_key = os.getenv("ALPHA_VANTAGE_API_KEY", "")
    av_sym = _av_ticker(ticker)
    if not (av_key and av_sym):
        return None
    av_data = await _get_av_overview(av_sym, av_key)
    beta_val = _safe_float(av_data.get("Beta"))
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


# ---------------------------------------------------------------------------
# 多因子回归（Fama-French 3/4 因子）
# ---------------------------------------------------------------------------

async def get_factor_exposures(ticker: str, period: str = _BETA_WINDOW) -> Optional[dict]:
    """Fama-French 4 因子暴露（Mkt-RF/SMB/HML/UMD）+ Newey-West HAC 标准误

    数据源优先级：
      1. Kenneth French 真实日度因子（准确，学术标准）
      2. ETF 代理（fallback，tracking error ~2-3%年化）

    改进：
    - Newey-West HAC 修正自相关，t-stat 不再虚高
    - 数据质量审计，剔除极端 spike 观测
    """
    import numpy as np
    import pandas as pd
    fmp_ticker_sym = _fmp_ticker(ticker)
    if not fmp_ticker_sym:
        return None

    # 拉取股票价格序列
    price_df = await _get_price_series([ticker])
    if price_df.empty or fmp_ticker_sym not in price_df.columns:
        return None

    cutoff = pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=365 * 3)
    price_df = price_df[price_df.index >= cutoff]
    stock_returns = price_df[fmp_ticker_sym].pct_change().dropna()
    if len(stock_returns) < _BETA_MIN_OBS:
        return None

    # 尝试用 Kenneth French 真实因子
    ff_data = await _get_ff_factors()
    source = "kenneth_french"

    if ff_data is not None and not ff_data.empty:
        aligned = pd.DataFrame({"y": stock_returns}).join(ff_data, how="inner")
        aligned = aligned.dropna(subset=["y", "mkt_rf", "smb", "hml"])
        if len(aligned) >= _BETA_MIN_OBS:
            y_col = "y"
            factor_cols = ["mkt_rf", "smb", "hml"]
            if "umd" in aligned.columns and aligned["umd"].notna().sum() > _BETA_MIN_OBS * 0.8:
                factor_cols.append("umd")
                aligned = aligned.dropna(subset=factor_cols)
            else:
                aligned = aligned.dropna(subset=factor_cols)

            if len(aligned) >= _BETA_MIN_OBS:
                return _run_factor_regression(
                    aligned[y_col].values,
                    aligned[factor_cols].values,
                    factor_cols,
                    n_obs=len(aligned),
                    period=period,
                    source=source,
                )

    # Fallback: ETF 代理
    source = "etf_proxy"
    proxies = list({
        _FF_PROXIES["market"],
        _FF_PROXIES["smb_long"], _FF_PROXIES["smb_short"],
        _FF_PROXIES["hml_long"], _FF_PROXIES["hml_short"],
        _FF_PROXIES["umd_long"], _FF_PROXIES["umd_short"],
    })
    df = await _get_price_series([ticker, *proxies])
    if df.empty or fmp_ticker_sym not in df.columns:
        return None
    missing = [p for p in proxies if p not in df.columns]
    if missing:
        return None

    df = df[df.index >= cutoff]
    returns = df.pct_change().dropna()

    # 数据质量审计
    audit = audit_price_data(returns[[fmp_ticker_sym]])
    if not audit["is_clean"]:
        # 剔除极端日（|r|>50%），保留其余数据
        mask = returns[fmp_ticker_sym].abs() <= 0.5
        returns = returns[mask]

    if len(returns) < _BETA_MIN_OBS:
        return None

    y = returns[fmp_ticker_sym]
    mkt = returns[_FF_PROXIES["market"]]
    smb = returns[_FF_PROXIES["smb_long"]] - returns[_FF_PROXIES["smb_short"]]
    hml = returns[_FF_PROXIES["hml_long"]] - returns[_FF_PROXIES["hml_short"]]
    umd = returns[_FF_PROXIES["umd_long"]] - returns[_FF_PROXIES["umd_short"]]

    X = pd.concat([mkt, smb, hml, umd], axis=1).dropna()
    X.columns = ["mkt_rf", "smb", "hml", "umd"]
    data = pd.concat([y.rename("y"), X], axis=1).dropna()
    if len(data) < _BETA_MIN_OBS:
        return None

    return _run_factor_regression(
        data["y"].values,
        data[["mkt_rf", "smb", "hml", "umd"]].values,
        ["mkt_rf", "smb", "hml", "umd"],
        n_obs=len(data),
        period=period,
        source=source,
    )


def _run_factor_regression(
    y: "np.ndarray",
    X_factors: "np.ndarray",
    factor_names: list[str],
    n_obs: int,
    period: str,
    source: str,
) -> dict:
    """OLS 回归 + Newey-West HAC 标准误"""
    import numpy as np
    n = len(y)
    Xm = np.column_stack([np.ones(n), X_factors])
    try:
        xtx_inv = np.linalg.inv(Xm.T @ Xm)
    except np.linalg.LinAlgError:
        return None
    coef = xtx_inv @ Xm.T @ y

    y_hat = Xm @ coef
    resid = y - y_hat

    # Newey-West HAC 标准误（修正自相关）
    se_nw = _newey_west_se(Xm, resid)
    # 同时保留 naive OLS SE 供对比
    k = Xm.shape[1]
    sigma2 = (resid @ resid) / (n - k)
    se_ols = np.sqrt(np.maximum(np.diag(xtx_inv) * sigma2, 0))

    t_stats_nw = coef / np.where(se_nw > 0, se_nw, 1e-10)
    t_stats_ols = coef / np.where(se_ols > 0, se_ols, 1e-10)

    ss_tot = ((y - y.mean()) ** 2).sum()
    ss_res = (resid ** 2).sum()
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else None

    # 输出因子名统一为 mkt/smb/hml/umd
    name_map = {"mkt_rf": "mkt"}
    labels = ["alpha_daily"] + factor_names
    out: dict = {}
    for i, lab in enumerate(labels):
        if lab == "alpha_daily":
            continue
        out_name = name_map.get(lab, lab)
        out[out_name] = {
            "beta":       round(float(coef[i]), 3),
            "t_stat":     round(float(t_stats_nw[i]), 2),
            "t_stat_ols": round(float(t_stats_ols[i]), 2),
            "significant": bool(abs(t_stats_nw[i]) > 2.0),
        }

    out["alpha_annual"] = round(float(coef[0] * 252), 4)
    out["alpha_t_stat"] = round(float(t_stats_nw[0]), 2)
    out["alpha_t_stat_ols"] = round(float(t_stats_ols[0]), 2)
    out["alpha_significant"] = bool(abs(t_stats_nw[0]) > 2.0)
    out["r2"] = round(float(r2), 3) if r2 is not None else None
    out["n_obs"] = int(n_obs)
    out["period"] = period
    out["source"] = source
    # HAC 信息
    nw_lags = int(4 * (n / 100) ** (2.0 / 9.0))
    out["newey_west_lags"] = nw_lags
    return out


async def get_batch_factor_exposures(tickers: list[str]) -> dict[str, dict | None]:
    """批量多因子暴露（串行以控制 AV 限速，缓存命中时快）"""
    if not tickers:
        return {}
    # AV 限速严格，串行跑避免 Note/Information 触发
    out: dict[str, dict | None] = {}
    for t in tickers:
        try:
            out[t] = await get_factor_exposures(t)
        except Exception:
            out[t] = None
    return out


# ---------------------------------------------------------------------------
# 相关性矩阵（yfinance，服务器 429 时优雅降级）
# ---------------------------------------------------------------------------

async def get_correlation_matrix(tickers: list[str], period: str = "3y") -> dict:
    """全样本相关性 + 尾部相关性（SPY 底部 10% 下跌日）
    数据源 = Alpha Vantage 日线
    """
    import pandas as pd
    if len(tickers) < 2:
        return {"pairs": {}, "tail_pairs": {}, "tickers": tickers}

    # 只保留 FMP 支持的 ticker（排除 A 股 / 加密货币）
    supported = [t for t in tickers if _fmp_ticker(t)]
    if len(supported) < 2:
        return {"pairs": {}, "tail_pairs": {}, "tickers": tickers,
                "skipped": [t for t in tickers if t not in supported]}

    download = list({*supported, "SPY"})
    df = await _get_price_series(download)
    if df.empty:
        return {"pairs": {}, "tail_pairs": {}, "tickers": tickers}

    cutoff = pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=365 * 3)
    df = df[df.index >= cutoff]
    returns = df.pct_change().dropna()
    if returns.empty:
        return {"pairs": {}, "tail_pairs": {}, "tickers": tickers}

    supported_fmp = {_fmp_ticker(t): t for t in supported}  # fmp_sym → 原始 ticker
    asset_cols = [c for c in returns.columns if c in supported_fmp]
    if len(asset_cols) < 2:
        return {"pairs": {}, "tail_pairs": {}, "tickers": tickers}

    asset_returns = returns[asset_cols]
    full_corr = asset_returns.corr().round(2)

    tail_pairs = {}
    tail_days = 0
    if "SPY" in returns.columns:
        spy = returns["SPY"]
        threshold = spy.quantile(0.1)
        tail_mask = spy <= threshold
        tail_days = int(tail_mask.sum())
        if tail_days >= 20:
            tail_returns = asset_returns[tail_mask]
            tail_corr = tail_returns.corr().round(2)
            for i, t1 in enumerate(tail_corr.columns):
                for j, t2 in enumerate(tail_corr.columns):
                    if i < j:
                        v = tail_corr.loc[t1, t2]
                        if pd.notna(v):
                            tail_pairs[f"{t1}/{t2}"] = float(v)

    pairs = {}
    for i, t1 in enumerate(full_corr.columns):
        for j, t2 in enumerate(full_corr.columns):
            if i < j:
                v = full_corr.loc[t1, t2]
                if pd.notna(v):
                    pairs[f"{t1}/{t2}"] = float(v)

    return {
        "pairs": pairs,
        "tail_pairs": tail_pairs,
        "tail_days": tail_days,
        "tickers": asset_cols,
        "skipped": [t for t in tickers if _fmp_ticker(t) not in asset_cols],
        "period": period,
        "source": "fmp_daily",
    }


# ---------------------------------------------------------------------------
# ATR 止损位（yfinance，服务器 429 时优雅降级）
# ---------------------------------------------------------------------------

_ohlc_mem_cache: dict[str, list] = {}


async def _get_fmp_ohlc(ticker: str, days: int = 100) -> list[dict]:
    """OHLC 数据 → [{date, high, low, close}, ...]
    优先 AKShare（新浪），fallback FMP。仅内存缓存。
    """
    fmp_sym = _fmp_ticker(ticker)
    if not fmp_sym:
        return []
    if fmp_sym in _ohlc_mem_cache:
        return _ohlc_mem_cache[fmp_sym]

    # 1) 首选 AKShare
    def _ak_sync():
        try:
            import akshare as ak
            df = ak.stock_us_daily(symbol=fmp_sym, adjust="qfq")
            if df is None or df.empty:
                return []
            rows = []
            # 取最近 days 条（df 已按日期升序）
            for _, r in df.tail(days).iterrows():
                date = r["date"]
                date_str = date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else str(date)[:10]
                try:
                    rows.append({
                        "date":  date_str,
                        "high":  float(r["high"]),
                        "low":   float(r["low"]),
                        "close": float(r["close"]),
                    })
                except (TypeError, ValueError, KeyError):
                    continue
            return list(reversed(rows))  # 与 FMP 格式一致（倒序）
        except Exception:
            return []

    result = await asyncio.get_event_loop().run_in_executor(None, _ak_sync)

    # 2) Fallback FMP
    if not result:
        fmp_key = os.getenv("FMP_API_KEY", "")
        if fmp_key:
            import httpx
            try:
                async with httpx.AsyncClient(timeout=20.0) as c:
                    r = await c.get(
                        f"{_FMP_BASE}/stable/historical-price-eod/full",
                        params={"symbol": fmp_sym, "apikey": fmp_key},
                    )
                    data = r.json() if r.status_code == 200 else []
            except Exception:
                data = []
            if isinstance(data, list):
                result = data[: max(days, 50)]

    _ohlc_mem_cache[fmp_sym] = result
    return result


async def get_atr_stops(
    ticker: str,
    entry_price: float,
    atr_period: int = 14,
    stop_multiplier: float = 2.5,
    target_multiplier: float = 3.0,
) -> Optional[dict]:
    """ATR(14) 动态止损/目标位，FMP historical 提供 OHLC"""
    import pandas as pd
    rows = await _get_fmp_ohlc(ticker, days=100)
    if not rows or len(rows) < atr_period + 5:
        return None

    parsed = []
    for r in rows:
        try:
            parsed.append({
                "date":  pd.Timestamp(r["date"]),
                "high":  float(r["high"]),
                "low":   float(r["low"]),
                "close": float(r.get("adjClose") or r["close"]),
            })
        except (KeyError, ValueError, TypeError):
            continue
    if len(parsed) < atr_period + 5:
        return None

    df = pd.DataFrame(parsed).sort_values("date").set_index("date")
    prev = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"]  - prev).abs(),
    ], axis=1).max(axis=1)
    atr     = float(tr.rolling(atr_period).mean().iloc[-1])
    ma20    = float(df["close"].rolling(20).mean().iloc[-1])
    current = float(df["close"].iloc[-1])
    return {
        "atr":           round(atr, 4),
        "stop_loss":     round(entry_price - stop_multiplier * atr, 4),
        "take_profit":   round(entry_price + target_multiplier * atr, 4),
        "entry_valid":   current > ma20,
        "ma20":          round(ma20, 4),
        "current_price": round(current, 4),
    }


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
