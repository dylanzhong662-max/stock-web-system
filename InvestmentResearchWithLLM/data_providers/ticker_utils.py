import re

_TICKER_SUFFIX_RE = re.compile(r"\.(US|HK|SH|SZ)$", re.IGNORECASE)


def fmp_ticker(ticker: str) -> str | None:
    """持仓 ticker → FMP profile 格式，不支持的返回 None"""
    t = ticker.upper()
    if t in ("BTC", "ETH") or t.endswith(".SH") or t.endswith(".SZ"):
        return None
    return _TICKER_SUFFIX_RE.sub("", t)


def av_ticker(ticker: str) -> str | None:
    """持仓 ticker → Alpha Vantage OVERVIEW 格式，A 股 / 加密货币返回 None"""
    t = ticker.upper()
    if t in ("BTC", "ETH") or t.endswith(".SH") or t.endswith(".SZ"):
        return None
    return _TICKER_SUFFIX_RE.sub("", t)


def yf_ticker(ticker: str) -> str:
    """持仓 ticker → yfinance 格式"""
    t = ticker.upper()
    if t == "BTC":
        return "BTC-USD"
    if t.endswith(".SH"):
        return t.replace(".SH", ".SS")
    if t.endswith(".SZ"):
        return t
    return _TICKER_SUFFIX_RE.sub("", t)


def get_benchmark(ticker: str) -> str:
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


def safe_float(v) -> float | None:
    if v is None or str(v).strip() in ("None", "N/A", "-", ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
