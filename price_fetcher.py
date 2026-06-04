import json
import os
import requests
from datetime import datetime
from typing import Optional, List, Dict, Tuple

_cache: dict = {}
_cache_ts: dict = {}
CACHE_TTL = 300

_FMP_API_KEY = os.environ.get("FMP_API_KEY", "KJdh1OAPcCWP8cRveZjXZG64ibk8iGHt")
_FMP_BASE = "https://financialmodelingprep.com/stable"

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

_FALLBACK_FILE = os.path.join(os.path.dirname(__file__), "data", "price_fallback.json")
_FALLBACK_PRICES: Dict[str, float] = {}


def _load_fallback():
    global _FALLBACK_PRICES
    try:
        with open(_FALLBACK_FILE, "r") as f:
            _FALLBACK_PRICES = json.load(f)
    except Exception:
        _FALLBACK_PRICES = {}


def _save_fallback():
    try:
        os.makedirs(os.path.dirname(_FALLBACK_FILE), exist_ok=True)
        with open(_FALLBACK_FILE, "w") as f:
            json.dump(_FALLBACK_PRICES, f, ensure_ascii=False)
    except Exception:
        pass


_load_fallback()


def set_fallback_price(asset: str, price: float):
    """截图解析时将 current_price 持久化写入兜底文件。"""
    if price and price > 0:
        _FALLBACK_PRICES[asset] = price
        _save_fallback()


def _to_fmp_ticker(raw_ticker: str) -> str:
    """将各种格式的 ticker 转换为 FMP 格式。"""
    t = raw_ticker.strip().upper()
    if t == "BTC" or t == "BTC-USD":
        return "BTCUSD"
    if t == "ETH" or t == "ETH-USD":
        return "ETHUSD"
    if t == "SOL" or t == "SOL-USD":
        return "SOLUSD"
    if t.endswith("-USD"):
        return t.replace("-USD", "USD")
    if t.endswith(".SS") or t.endswith(".SZ"):
        return t
    if t.endswith(".SH"):
        return t[:-3] + ".SS"
    if t.endswith(".HK"):
        return t
    if t.endswith(".US"):
        return t[:-3]
    return t


def _fetch_single(ticker: str) -> Optional[float]:
    """从 FMP 获取单个 ticker 的最新价格。"""
    fmp_ticker = _to_fmp_ticker(ticker)
    try:
        resp = requests.get(
            f"{_FMP_BASE}/quote",
            params={"symbol": fmp_ticker, "apikey": _FMP_API_KEY},
            headers=_HEADERS,
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and data:
                price = data[0].get("price")
                if price is not None:
                    return float(price)
    except Exception:
        pass
    return None


def get_prices_batch(positions: List[Tuple[str, str]]) -> Dict[str, Optional[float]]:
    """positions: [(asset_key, raw_ticker), ...] → {asset_key: price}

    FMP /stable/quote 支持逗号分隔批量查询。
    """
    now = datetime.now()
    result: Dict[str, Optional[float]] = {}
    to_fetch: List[Tuple[str, str]] = []

    for asset, raw_ticker in positions:
        cached_at = _cache_ts.get(asset)
        if cached_at and (now - cached_at).total_seconds() < CACHE_TTL and asset in _cache:
            result[asset] = _cache[asset]
        else:
            to_fetch.append((asset, raw_ticker))

    if not to_fetch:
        return result

    # FMP 支持批量查询（逗号分隔，最多 50 个）
    asset_map: Dict[str, str] = {}  # fmp_ticker → asset_key
    fmp_tickers = []
    for asset, raw_ticker in to_fetch:
        fmp_t = _to_fmp_ticker(raw_ticker)
        asset_map[fmp_t] = asset
        fmp_tickers.append(fmp_t)

    # 分批，每批最多 50 个
    for i in range(0, len(fmp_tickers), 50):
        batch = fmp_tickers[i:i + 50]
        symbols = ",".join(batch)
        try:
            resp = requests.get(
                f"{_FMP_BASE}/quote",
                params={"symbol": symbols, "apikey": _FMP_API_KEY},
                headers=_HEADERS,
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    for item in data:
                        sym = item.get("symbol", "")
                        price = item.get("price")
                        if sym in asset_map and price is not None:
                            asset_key = asset_map[sym]
                            _cache[asset_key] = float(price)
                            _cache_ts[asset_key] = now
                            result[asset_key] = float(price)
        except Exception:
            pass

    # 未获取到的用 fallback
    for asset, raw_ticker in to_fetch:
        if asset not in result:
            price = _FALLBACK_PRICES.get(asset)
            if price is not None:
                _cache[asset] = price
                _cache_ts[asset] = now
            result[asset] = price

    return result


def get_current_price(asset: str, ticker: str = "") -> Optional[float]:
    return get_prices_batch([(asset, ticker or asset)]).get(asset)


def get_macro_prices() -> dict:
    """获取宏观指标：VIX / DXY / 10Y 国债收益率。"""
    macro_map = {"VIX": "^VIX", "DXY": "DX-Y.NYB", "TNX": "^TNX"}
    result = {}
    for key, ticker in macro_map.items():
        price = _fetch_single(ticker)
        result[key] = round(price, 3) if price is not None else None
    return result
