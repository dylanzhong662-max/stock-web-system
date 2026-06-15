"""价格序列：FMP 日线（主）+ AKShare fallback + Kenneth French 因子"""
import os
import asyncio
import io
from typing import Optional

import httpx

from .ticker_utils import fmp_ticker
from .cache import price_cache_get, price_cache_set

_FMP_BASE = "https://financialmodelingprep.com"

_price_mem_cache: dict[str, dict] = {}
_av_daily_inflight: dict[str, asyncio.Future] = {}

_FF_PROXIES = {
    "market": "SPY",
    "smb_long": "IWM", "smb_short": "SPY",
    "hml_long": "IWD", "hml_short": "IWF",
    "umd_long": "MTUM", "umd_short": "SPY",
}

_ff_factor_cache: dict[str, "object"] = {}
_FF_DATA_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_Factors_daily_CSV.zip"
_MOM_DATA_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Momentum_Factor_daily_CSV.zip"

_fmp_price_sem = asyncio.Semaphore(5)


async def _get_fmp_daily(ticker: str) -> dict:
    """FMP historical-price-eod — Starter plan 无严格限速"""
    fmp_sym = fmp_ticker(ticker)
    if not fmp_sym:
        return {}

    fmp_key = os.getenv("FMP_API_KEY", "")
    if not fmp_key:
        return {}

    async with _fmp_price_sem:
        try:
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.get(
                    f"{_FMP_BASE}/stable/historical-price-eod/full",
                    params={"symbol": fmp_sym, "apikey": fmp_key},
                )
                data = r.json() if r.status_code == 200 else []
        except Exception:
            data = []

    if not isinstance(data, list):
        return {}

    series: dict[str, float] = {}
    for row in data:
        date = row.get("date")
        close = row.get("close")
        if date and close is not None:
            try:
                series[date] = float(close)
            except (TypeError, ValueError):
                continue
    return series


async def _get_akshare_daily(ticker: str) -> dict:
    """AKShare 日线 fallback（新浪数据源，无 key）"""
    fmp_sym = fmp_ticker(ticker)
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
                date_str = date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else str(date)[:10]
                try:
                    series[date_str] = float(row["close"])
                except (TypeError, ValueError, KeyError):
                    continue
            return series
        except Exception:
            return {}

    return await asyncio.get_event_loop().run_in_executor(None, _sync)


async def get_av_daily(ticker: str, outputsize: str = "full") -> dict:
    """日线价格 → {date: close}，24h 缓存。

    数据源优先级：FMP > AKShare
    进程级去重：并发请求同一 ticker 只打一次外部 API
    """
    fmp_sym = fmp_ticker(ticker)
    if not fmp_sym:
        return {}

    cached = price_cache_get(fmp_sym)
    if cached is not None:
        return cached

    if fmp_sym in _price_mem_cache:
        return _price_mem_cache[fmp_sym]

    if fmp_sym in _av_daily_inflight:
        try:
            return await _av_daily_inflight[fmp_sym]
        except Exception:
            pass

    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    _av_daily_inflight[fmp_sym] = fut

    try:
        series = await _get_fmp_daily(ticker)

        if not series:
            series = await _get_akshare_daily(ticker)

        if series:
            price_cache_set(fmp_sym, series)
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


async def get_price_series(tickers: list[str]) -> "object":
    """批量拉价格序列 → pandas DataFrame"""
    import pandas as pd
    seq = await asyncio.gather(*[get_av_daily(t) for t in tickers], return_exceptions=True)
    frames = {}
    for t, s in zip(tickers, seq):
        if isinstance(s, dict) and s:
            key = fmp_ticker(t) or t
            frames[key] = pd.Series(s).sort_index()
    if not frames:
        return pd.DataFrame()
    df = pd.DataFrame(frames)
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    return df


def _parse_ff_csv(text: str) -> "object":
    """解析 Kenneth French CSV（跳过头部描述行）"""
    import pandas as pd
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


async def get_ff_factors() -> "object | None":
    """下载 Kenneth French 日度因子数据（Mkt-RF, SMB, HML, UMD, RF）"""
    import pandas as pd
    import zipfile

    if "daily" in _ff_factor_cache:
        cached = _ff_factor_cache["daily"]
        if cached is not None and not cached.empty:
            return cached

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
