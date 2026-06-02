"""data_providers — 拆分自 data_fetcher.py

子模块：
  ticker_utils   — ticker 格式转换
  cache          — SQLite + 内存缓存
  financial_data — AV/FMP 财务快照
  price_series   — 日线序列 + FF 因子
  quant          — Beta/因子/相关性/ATR
  search         — Tavily 搜索
"""
from .ticker_utils import fmp_ticker, av_ticker, yf_ticker, get_benchmark, safe_float
from .cache import fin_cache_get, fin_cache_set, price_cache_get, price_cache_set
from .financial_data import (
    get_batch_stock_data,
    get_stock_data,
    get_cn_stock,
    fallback_overview_beta,
)
from .price_series import get_av_daily, get_price_series, get_ff_factors
from .quant import (
    audit_price_data,
    get_beta,
    get_factor_exposures,
    get_batch_factor_exposures,
    get_correlation_matrix,
    get_atr_stops,
)
from .search import search
from .intl_screener import screen_neglected_growth, format_neglect_candidates
