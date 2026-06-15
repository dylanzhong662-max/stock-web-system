"""量化分析：Beta / 多因子回归 / 相关性矩阵 / ATR 止损"""
import os
import asyncio
from typing import Optional

import httpx

from .ticker_utils import fmp_ticker, get_benchmark
from .price_series import get_price_series, get_av_daily, get_ff_factors, _FF_PROXIES
from .financial_data import fallback_overview_beta

_FMP_BASE = "https://financialmodelingprep.com"

_BETA_WINDOW = "3y"
_BETA_MIN_OBS = 200

_ohlc_mem_cache: dict[str, list] = {}


def audit_price_data(returns: "object") -> dict:
    """检测价格序列质量问题"""
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


def _newey_west_se(X: "object", residuals: "object", n_lags: int | None = None) -> "object":
    """Newey-West HAC 标准误"""
    import numpy as np
    n, k = X.shape
    if n_lags is None:
        n_lags = int(4 * (n / 100) ** (2.0 / 9.0))
    n_lags = max(1, n_lags)

    xtx_inv = np.linalg.inv(X.T @ X)
    S = np.zeros((k, k))
    e = residuals.reshape(-1, 1)
    xe = X * e

    S += xe.T @ xe

    for lag in range(1, n_lags + 1):
        weight = 1.0 - lag / (n_lags + 1.0)
        gamma = xe[lag:].T @ xe[:-lag]
        S += weight * (gamma + gamma.T)

    cov = xtx_inv @ S @ xtx_inv
    se = np.sqrt(np.maximum(np.diag(cov), 0))
    return se


def _run_factor_regression(
    y: "object",
    X_factors: "object",
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

    se_nw = _newey_west_se(Xm, resid)
    k = Xm.shape[1]
    sigma2 = (resid @ resid) / (n - k)
    se_ols = np.sqrt(np.maximum(np.diag(xtx_inv) * sigma2, 0))

    t_stats_nw = coef / np.where(se_nw > 0, se_nw, 1e-10)
    t_stats_ols = coef / np.where(se_ols > 0, se_ols, 1e-10)

    ss_tot = ((y - y.mean()) ** 2).sum()
    ss_res = (resid ** 2).sum()
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else None

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
    nw_lags = int(4 * (n / 100) ** (2.0 / 9.0))
    out["newey_west_lags"] = nw_lags
    return out


async def get_beta(
    ticker: str,
    period: str = _BETA_WINDOW,
    benchmark: str | None = None,
) -> Optional[dict]:
    """3y Beta（vs 基准），数据源 = Alpha Vantage 日线"""
    import numpy as np
    import pandas as pd

    _benchmark = benchmark or get_benchmark(ticker)

    if not fmp_ticker(ticker):
        return await fallback_overview_beta(ticker)

    fmp_ticker_sym = fmp_ticker(ticker)
    fmp_bench = fmp_ticker(_benchmark) or _benchmark

    df = await get_price_series([ticker, _benchmark])
    if df.empty or fmp_ticker_sym not in df.columns or fmp_bench not in df.columns:
        return await fallback_overview_beta(ticker)

    cutoff = pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=365 * 3)
    df = df[df.index >= cutoff]
    returns = df[[fmp_ticker_sym, fmp_bench]].pct_change().dropna()
    if len(returns) < _BETA_MIN_OBS:
        return await fallback_overview_beta(ticker)

    y = returns[fmp_ticker_sym].values
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


async def get_factor_exposures(ticker: str, period: str = _BETA_WINDOW) -> Optional[dict]:
    """Fama-French 4 因子暴露 + Newey-West HAC 标准误"""
    import numpy as np
    import pandas as pd

    fmp_ticker_sym = fmp_ticker(ticker)
    if not fmp_ticker_sym:
        return None

    price_df = await get_price_series([ticker])
    if price_df.empty or fmp_ticker_sym not in price_df.columns:
        return None

    cutoff = pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=365 * 3)
    price_df = price_df[price_df.index >= cutoff]
    stock_returns = price_df[fmp_ticker_sym].pct_change().dropna()
    if len(stock_returns) < _BETA_MIN_OBS:
        return None

    ff_data = await get_ff_factors()
    source = "kenneth_french"

    if ff_data is not None and not ff_data.empty:
        aligned = pd.DataFrame({"y": stock_returns}).join(ff_data, how="inner")
        aligned = aligned.dropna(subset=["y", "mkt_rf", "smb", "hml"])
        if len(aligned) >= _BETA_MIN_OBS:
            factor_cols = ["mkt_rf", "smb", "hml"]
            if "umd" in aligned.columns and aligned["umd"].notna().sum() > _BETA_MIN_OBS * 0.8:
                factor_cols.append("umd")
                aligned = aligned.dropna(subset=factor_cols)
            else:
                aligned = aligned.dropna(subset=factor_cols)

            if len(aligned) >= _BETA_MIN_OBS:
                return _run_factor_regression(
                    aligned["y"].values,
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
    df = await get_price_series([ticker, *proxies])
    if df.empty or fmp_ticker_sym not in df.columns:
        return None
    missing = [p for p in proxies if p not in df.columns]
    if missing:
        return None

    df = df[df.index >= cutoff]
    returns = df.pct_change().dropna()

    audit = audit_price_data(returns[[fmp_ticker_sym]])
    if not audit["is_clean"]:
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


async def get_batch_factor_exposures(tickers: list[str]) -> dict[str, dict | None]:
    """批量多因子暴露（串行以控制 AV 限速）"""
    if not tickers:
        return {}
    out: dict[str, dict | None] = {}
    for t in tickers:
        try:
            out[t] = await get_factor_exposures(t)
        except Exception:
            out[t] = None
    return out


async def get_correlation_matrix(tickers: list[str], period: str = "3y") -> dict:
    """全样本相关性 + 尾部相关性（SPY 底部 10% 下跌日）"""
    import pandas as pd
    if len(tickers) < 2:
        return {"pairs": {}, "tail_pairs": {}, "tickers": tickers}

    supported = [t for t in tickers if fmp_ticker(t)]
    if len(supported) < 2:
        return {"pairs": {}, "tail_pairs": {}, "tickers": tickers,
                "skipped": [t for t in tickers if t not in supported]}

    download = list({*supported, "SPY"})
    df = await get_price_series(download)
    if df.empty:
        return {"pairs": {}, "tail_pairs": {}, "tickers": tickers}

    cutoff = pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=365 * 3)
    df = df[df.index >= cutoff]
    returns = df.pct_change().dropna()
    if returns.empty:
        return {"pairs": {}, "tail_pairs": {}, "tickers": tickers}

    supported_fmp = {fmp_ticker(t): t for t in supported}
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
        "skipped": [t for t in tickers if fmp_ticker(t) not in asset_cols],
        "period": period,
        "source": "fmp_daily",
    }


async def _get_fmp_ohlc(ticker: str, days: int = 100) -> list[dict]:
    """OHLC 数据，FMP 优先，AKShare fallback"""
    fmp_sym = fmp_ticker(ticker)
    if not fmp_sym:
        return []
    if fmp_sym in _ohlc_mem_cache:
        return _ohlc_mem_cache[fmp_sym]

    result = []
    fmp_key = os.getenv("FMP_API_KEY", "")
    if fmp_key:
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

    if result:
        # FMP returns descending (newest first) — reverse to ascending for ATR/MA calcs
        result = list(reversed(result))
    else:
        def _ak_sync():
            try:
                import akshare as ak
                df = ak.stock_us_daily(symbol=fmp_sym, adjust="qfq")
                if df is None or df.empty:
                    return []
                rows = []
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
                return rows  # already ascending from iterrows()
            except Exception:
                return []

        result = await asyncio.get_event_loop().run_in_executor(None, _ak_sync)

    _ohlc_mem_cache[fmp_sym] = result
    return result


async def get_atr_stops(
    ticker: str,
    entry_price: float,
    atr_period: int = 14,
    stop_multiplier: float = 2.5,
    target_multiplier: float = 3.0,
) -> Optional[dict]:
    """ATR(14) 动态止损/目标位"""
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
                "close": float(r["close"]),
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
        (df["low"] - prev).abs(),
    ], axis=1).max(axis=1)
    atr = float(tr.rolling(atr_period).mean().iloc[-1])
    ma20 = float(df["close"].rolling(20).mean().iloc[-1])
    current = float(df["close"].iloc[-1])
    return {
        "atr":           round(atr, 4),
        "stop_loss":     round(entry_price - stop_multiplier * atr, 4),
        "take_profit":   round(entry_price + target_multiplier * atr, 4),
        "entry_valid":   current > ma20,
        "ma20":          round(ma20, 4),
        "current_price": round(current, 4),
    }
