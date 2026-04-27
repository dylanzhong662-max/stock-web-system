import json
import os
import requests
from datetime import datetime
from typing import Optional, List, Dict, Tuple

_cache: dict = {}
_cache_ts: dict = {}
CACHE_TTL = 300

_PROXY = {"https": "socks5h://127.0.0.1:1080", "http": "socks5h://127.0.0.1:1080"}
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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


def _to_yf_ticker(raw_ticker: str) -> str:
    t = raw_ticker.strip()
    if t.upper() == "BTC":
        return "BTC-USD"
    if t.endswith(".SH"):
        return t[:-3] + ".SS"
    if t.endswith(".US"):
        return t[:-3]
    return t


def _fetch_single(yf_ticker: str) -> Optional[float]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_ticker}?interval=1d&range=2d"
    attempts = [
        {"proxies": None, "verify": True},
        {"proxies": None, "verify": False},
        {"proxies": _PROXY, "verify": True},
    ]
    for kwargs in attempts:
        try:
            r = requests.get(url, headers=_HEADERS, timeout=10, **kwargs)
            d = r.json()
            closes = d["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            closes = [c for c in closes if c is not None]
            if closes:
                return float(closes[-1])
        except Exception:
            continue
    return None


def get_prices_batch(positions: List[Tuple[str, str]]) -> Dict[str, Optional[float]]:
    """positions: [(asset_key, raw_ticker), ...] → {asset_key: price}"""
    now = datetime.now()
    result: Dict[str, Optional[float]] = {}
    to_fetch: List[Tuple[str, str]] = []

    for asset, raw_ticker in positions:
        cached_at = _cache_ts.get(asset)
        if cached_at and (now - cached_at).total_seconds() < CACHE_TTL and asset in _cache:
            result[asset] = _cache[asset]
        else:
            to_fetch.append((asset, raw_ticker))

    for asset, raw_ticker in to_fetch:
        yf_tick = _to_yf_ticker(raw_ticker)
        price = _fetch_single(yf_tick)
        if price is None:
            # 兜底：用截图解析时存入的 current_price
            price = _FALLBACK_PRICES.get(asset)
        if price is not None:
            _cache[asset] = price
            _cache_ts[asset] = now
        result[asset] = price

    return result


def get_current_price(asset: str, ticker: str = "") -> Optional[float]:
    return get_prices_batch([(asset, ticker or asset)]).get(asset)


def get_macro_prices() -> dict:
    macro_map = {"VIX": "^VIX", "DXY": "DX-Y.NYB", "TNX": "^TNX"}
    result = {}
    for key, ticker in macro_map.items():
        price = _fetch_single(ticker)
        result[key] = round(price, 3) if price is not None else None
    return result
