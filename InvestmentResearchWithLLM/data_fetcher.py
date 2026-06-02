"""兼容层 — 原 data_fetcher.py 已拆分到 data_providers/ 包

所有外部模块仍可通过 `import data_fetcher` 访问原有接口，无需改动。

拆分后的实际实现：
  data_providers/ticker_utils.py   — ticker 格式转换
  data_providers/cache.py          — SQLite + 内存缓存
  data_providers/financial_data.py — AV/FMP 财务快照
  data_providers/price_series.py   — 日线序列 + FF 因子
  data_providers/quant.py          — Beta/因子/相关性/ATR
  data_providers/search.py         — Tavily 搜索
"""

from data_providers import (
    # ticker_utils
    fmp_ticker as _fmp_ticker,
    av_ticker as _av_ticker,
    yf_ticker as _yf_ticker,
    get_benchmark as _get_benchmark,
    safe_float as _safe_float,
    # cache
    price_cache_get as _price_cache_get,
    price_cache_set as _price_cache_set,
    fin_cache_get as _fin_cache_get,
    fin_cache_set as _fin_cache_set,
    # financial_data
    get_batch_stock_data,
    get_stock_data,
    get_cn_stock,
    fallback_overview_beta as _fallback_overview_beta,
    # price_series
    get_av_daily as _get_av_daily,
    get_price_series as _get_price_series,
    get_ff_factors as _get_ff_factors,
    # quant
    audit_price_data,
    get_beta,
    get_factor_exposures,
    get_batch_factor_exposures,
    get_correlation_matrix,
    get_atr_stops,
    # search
    search,
    # intl_screener
    screen_neglected_growth,
    format_neglect_candidates,
)

__all__ = [
    "search",
    "get_batch_stock_data",
    "get_stock_data",
    "get_cn_stock",
    "get_beta",
    "get_factor_exposures",
    "get_batch_factor_exposures",
    "get_correlation_matrix",
    "get_atr_stops",
    "audit_price_data",
    "screen_neglected_growth",
    "format_neglect_candidates",
    # private but used by other modules
    "_fmp_ticker",
    "_av_ticker",
    "_yf_ticker",
    "_get_benchmark",
    "_safe_float",
    "_price_cache_get",
    "_price_cache_set",
    "_fin_cache_get",
    "_fin_cache_set",
    "_fallback_overview_beta",
    "_get_av_daily",
    "_get_price_series",
    "_get_ff_factors",
]
