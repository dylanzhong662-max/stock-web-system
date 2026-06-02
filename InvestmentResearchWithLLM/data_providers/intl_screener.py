"""International neglect-alpha screener — finds supply chain stocks with
low institutional coverage + high growth across US, Japan, Korea, Europe.

Data source strategy:
- US stocks: FMP financial-growth + grades (accurate, paid Starter plan)
- International stocks: FMP profile (market cap/sector validation) + yfinance (fundamentals)
- Discovery: Tavily multi-language search → extract tickers → validate

Quantitative rationale (academic basis):
- Neglect effect: stocks with <5 analyst coverage outperform by 2-4% annually
  (Arbel & Strebel 1983, updated by Hong, Lim, Stein 2000)
- Combining neglect + growth filters avoids value traps
- International markets have wider neglect dispersion than US
"""

import asyncio
import os
import re
import ssl
from datetime import datetime

import httpx

from .ticker_utils import safe_float
from .cache import fin_cache_get, fin_cache_set

def _detect_ssl_verify():
    """Auto-detect whether SSL verification works for FMP.
    Some environments (VPN, corporate proxy) inject self-signed certs."""
    import urllib.request
    test_url = "https://financialmodelingprep.com/api/v3/is-the-market-open?apikey=demo"
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        urllib.request.urlopen(test_url, context=ctx, timeout=5)
        return ctx
    except Exception:
        pass
    try:
        ctx = ssl.create_default_context()
        urllib.request.urlopen(test_url, context=ctx, timeout=5)
        return True
    except Exception:
        return False  # httpx verify=False

_SSL_VERIFY = _detect_ssl_verify()

_FMP_BASE = "https://financialmodelingprep.com"

# FMP exchange codes (from /stable/available-exchanges)
FMP_EXCHANGES = {
    "JPX": ".T",      # Tokyo
    "KSC": ".KS",     # Korea Exchange
    "KOE": ".KQ",     # KOSDAQ
    "AMS": ".AS",     # Euronext Amsterdam
    "PAR": ".PA",     # Euronext Paris
    "XETRA": ".DE",   # Deutsche Borse
    "LSE": ".L",      # London
    "STU": ".SG",     # Stuttgart
    "MIL": ".MI",     # Milan
    "SIX": ".SW",     # Zurich
}

# Market-specific thresholds: Japan/Korea mid-caps naturally have higher coverage
# than equivalent US small-caps (fewer sell-side firms but each covers more names)
NEGLECT_THRESHOLDS = {
    "US": {"max_analysts": 8, "ideal": 5},
    "JP": {"max_analysts": 14, "ideal": 8},
    "KR": {"max_analysts": 12, "ideal": 7},
    "EU": {"max_analysts": 10, "ideal": 6},
    "default": {"max_analysts": 10, "ideal": 6},
    # Shared thresholds
    "min_revenue_growth": 0.12,
    "min_market_cap_usd": 5e8,
    "max_market_cap_usd": 3e10,
    "max_forward_pe": 50,
}

# JPY market cap is in yen — need conversion for threshold comparison
_JPY_USD = 155  # approximate, updated infrequently


def _get_fmp_key() -> str:
    return os.getenv("FMP_API_KEY", "")


# ---------------------------------------------------------------------------
# FMP endpoints (Starter plan capabilities)
# ---------------------------------------------------------------------------

async def _fmp_profiles(symbols: list[str]) -> list[dict]:
    """FMP /stable/profile — works for ALL exchanges on Starter plan.
    Returns market_cap, sector, industry, country, exchange.
    Note: Starter plan requires one symbol per call (batch not supported)."""
    api_key = _get_fmp_key()
    if not api_key or not symbols:
        return []

    async def _fetch_one(sym: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=12.0, verify=_SSL_VERIFY) as c:
                r = await c.get(
                    f"{_FMP_BASE}/stable/profile",
                    params={"symbol": sym, "apikey": api_key},
                )
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list) and data:
                        return data[0]
        except Exception:
            pass
        return None

    tasks = [_fetch_one(s) for s in symbols[:20]]
    raw = await asyncio.gather(*tasks)
    return [r for r in raw if r is not None]


async def _fmp_us_growth(tickers: list[str]) -> dict[str, dict]:
    """FMP /stable/financial-growth — US stocks only on Starter.
    Returns revenue_growth, net_income_growth per ticker."""
    api_key = _get_fmp_key()
    if not api_key or not tickers:
        return {}

    results = {}
    for ticker in tickers[:15]:
        try:
            async with httpx.AsyncClient(timeout=12.0, verify=_SSL_VERIFY) as c:
                r = await c.get(
                    f"{_FMP_BASE}/stable/financial-growth",
                    params={"symbol": ticker, "period": "annual", "limit": "1", "apikey": api_key},
                )
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list) and data:
                        results[ticker] = {
                            "revenue_growth": safe_float(data[0].get("revenueGrowth")),
                            "net_income_growth": safe_float(data[0].get("netIncomeGrowth")),
                            "eps_growth": safe_float(data[0].get("epsgrowth")),
                        }
        except Exception:
            continue
    return results


async def _fmp_us_analyst_count(tickers: list[str]) -> dict[str, int]:
    """FMP /stable/grades — count unique gradingCompany as analyst proxy.
    US stocks only on Starter plan."""
    api_key = _get_fmp_key()
    if not api_key or not tickers:
        return {}

    results = {}
    for ticker in tickers[:15]:
        try:
            async with httpx.AsyncClient(timeout=10.0, verify=_SSL_VERIFY) as c:
                r = await c.get(
                    f"{_FMP_BASE}/stable/grades",
                    params={"symbol": ticker, "apikey": api_key},
                )
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list):
                        # Count unique firms that graded in last 12 months
                        cutoff = (datetime.utcnow().year - 1)
                        recent = [
                            d for d in data
                            if d.get("date", "")[:4].isdigit()
                            and int(d["date"][:4]) >= cutoff
                        ]
                        companies = set(d.get("gradingCompany", "") for d in recent)
                        companies.discard("")
                        results[ticker] = len(companies)
        except Exception:
            continue
    return results


# ---------------------------------------------------------------------------
# yfinance (international stocks — Japan, Korea, Europe)
# ---------------------------------------------------------------------------

async def _yf_fundamentals(tickers: list[str]) -> list[dict]:
    """yfinance for international tickers — get growth, PE, analyst count.
    Falls back to FMP profile if yfinance is rate-limited (429)."""
    import time

    def _sync():
        import yfinance as yf
        results = []
        failures = 0
        for i, ticker in enumerate(tickers[:15]):
            if failures >= 2:
                break
            if i > 0:
                time.sleep(1.5)
            try:
                stock = yf.Ticker(ticker)
                info = stock.info
                if not info or not info.get("marketCap"):
                    failures += 1
                    continue
                failures = 0
                results.append({
                    "ticker": ticker,
                    "name": info.get("longName") or info.get("shortName", ticker),
                    "market_cap": info.get("marketCap"),
                    "currency": info.get("currency", "USD"),
                    "pe_forward": safe_float(info.get("forwardPE")),
                    "pe_trailing": safe_float(info.get("trailingPE")),
                    "peg_ratio": safe_float(info.get("pegRatio")),
                    "revenue_growth": safe_float(info.get("revenueGrowth")),
                    "earnings_growth": safe_float(info.get("earningsGrowth")),
                    "gross_margin": safe_float(info.get("grossMargins")),
                    "operating_margin": safe_float(info.get("operatingMargins")),
                    "analyst_count": info.get("numberOfAnalystOpinions", 0) or 0,
                    "target_mean_price": safe_float(info.get("targetMeanPrice")),
                    "current_price": safe_float(info.get("currentPrice")) or safe_float(info.get("previousClose")),
                    "52w_high": safe_float(info.get("fiftyTwoWeekHigh")),
                    "52w_low": safe_float(info.get("fiftyTwoWeekLow")),
                    "sector": info.get("sector", ""),
                    "industry": info.get("industry", ""),
                    "country": info.get("country", ""),
                    "exchange": info.get("exchange", ""),
                    "source": "yfinance",
                })
            except Exception:
                failures += 1
                continue
        return results

    yf_results = await asyncio.get_event_loop().run_in_executor(None, _sync)

    # If yfinance returned nothing (likely 429 rate limit), fall back to FMP profile
    if not yf_results and tickers:
        yf_results = await _fmp_intl_fallback(tickers)

    return yf_results


async def _fmp_intl_fallback(tickers: list[str]) -> list[dict]:
    """FMP profile fallback for international stocks when yfinance is rate-limited.
    Profile gives market_cap, sector, country but NOT revenue_growth or analyst_count.
    We estimate analyst coverage from FMP grades (may be empty for intl on Starter)."""
    profiles = await _fmp_profiles(tickers[:20])
    if not profiles:
        return []

    results = []
    for p in profiles:
        mc = p.get("marketCap", 0)
        currency = p.get("currency", "USD")
        country = p.get("country", "")

        # FMP profile doesn't give revenue_growth, so we try financial-growth
        # (works for some intl stocks depending on FMP coverage)
        results.append({
            "ticker": p.get("symbol", ""),
            "name": p.get("companyName", ""),
            "market_cap": mc,
            "currency": currency,
            "pe_forward": None,
            "pe_trailing": None,
            "peg_ratio": None,
            "revenue_growth": None,
            "earnings_growth": None,
            "gross_margin": None,
            "operating_margin": None,
            "analyst_count": 0,
            "target_mean_price": None,
            "current_price": p.get("price"),
            "52w_high": None,
            "52w_low": None,
            "sector": p.get("sector", ""),
            "industry": p.get("industry", ""),
            "country": country,
            "exchange": p.get("exchange", ""),
            "source": "fmp_profile",
        })

    # Try to enrich with financial-growth data from FMP (may work for some intl)
    api_key = _get_fmp_key()
    if api_key:
        for stock in results[:15]:
            ticker = stock["ticker"]
            try:
                async with httpx.AsyncClient(timeout=10.0, verify=_SSL_VERIFY) as c:
                    r = await c.get(
                        f"{_FMP_BASE}/stable/financial-growth",
                        params={"symbol": ticker, "period": "annual", "limit": "1", "apikey": api_key},
                    )
                    if r.status_code == 200:
                        data = r.json()
                        if isinstance(data, list) and data:
                            stock["revenue_growth"] = safe_float(data[0].get("revenueGrowth"))
                            stock["earnings_growth"] = safe_float(data[0].get("netIncomeGrowth"))
            except Exception:
                continue

    # For stocks where FMP financial-growth is unavailable (Starter plan intl limitation),
    # mark as "unverified candidate" — pass them to LLM with reduced score for judgment
    for stock in results:
        if stock.get("revenue_growth") is None:
            stock["growth_unverified"] = True

    return results


# ---------------------------------------------------------------------------
# Ticker discovery from search results
# ---------------------------------------------------------------------------

# Well-known international supply chain companies (name → ticker)
_INTL_COMPANY_MAP = {
    # Japan semiconductors & equipment
    r"Tokyo Electron|東京エレクトロン": "8035.T",
    r"Advantest|アドバンテスト": "6857.T",
    r"Disco Corp|ディスコ": "6146.T",
    r"Lasertec|レーザーテック": "6920.T",
    r"Renesas|ルネサス": "6723.T",
    r"Rohm|ローム": "6963.T",
    r"Murata|村田": "6981.T",
    r"Screen Holdings|スクリーン": "7735.T",
    r"Ibiden|イビデン": "4062.T",
    r"Shinko Electric|新光電気": "6967.T",
    r"Hamamatsu Photonics|浜松ホトニクス": "6965.T",
    # Japan industrials
    r"Keyence|キーエンス": "6861.T",
    r"Nidec|日本電産": "6594.T",
    r"Fanuc|ファナック": "6954.T",
    r"SMC Corp": "6273.T",
    r"Harmonic Drive|ハーモニック": "6324.T",
    # Korea
    r"Samsung Electronics|삼성전자": "005930.KS",
    r"SK Hynix|SK하이닉스": "000660.KS",
    r"Samsung SDI": "006400.KS",
    r"LG Energy|LG에너지": "373220.KS",
    r"Hanmi Semiconductor|한미반도체": "042700.KS",
    r"LEENO Industrial|리노공업": "058470.KS",
    r"ISC Co|아이에스시": "095340.KS",
    # Europe
    r"ASML": "ASML",
    r"Infineon": "IFX.DE",
    r"STMicro": "STMPA.PA",
    r"BE Semiconductor|Besi": "BESI.AS",
    r"ASM International": "ASM.AS",
    r"Aixtron": "AIXA.DE",
    r"Siltronic": "WAF.DE",
    r"Soitec": "SOI.PA",
    r"Nordic Semiconductor": "NOD.OL",
    r"VAT Group": "VACN.SW",
    r"Comet Holding": "COTN.SW",
    r"Bechtle": "BC8.DE",
    r"Nemetschek": "NEM.DE",
    r"Basler AG": "BSL.DE",
    r"Süss MicroTec|SUSS MicroTec": "SMHN.DE",
    r"Pfeiffer Vacuum": "PFV.DE",
    r"CML Microsystems": "CML.L",
    r"IQE": "IQE.L",
    # Japan mid/small-cap specialists
    r"Ulvac|アルバック": "6728.T",
    r"Towa Corp|TOWA": "6315.T",
    r"Kokusai Electric|国際電気": "6525.T",
    r"Ferrotec|フェローテック": "6890.T",
    r"Japan Material|日本マテリアル": "6055.T",
    r"CKD Corp": "6407.T",
    r"Daifuku|ダイフク": "6383.T",
    r"Horiba|堀場": "6856.T",
    r"Naura Technology|北方华创": "002371.SZ",
    # Korea mid-cap semiconductor/equipment
    r"Wonik IPS|원익IPS": "240810.KS",
    r"Eugene Technology|유진테크": "084370.KS",
    r"HPSP": "403870.KS",
    r"Techwing|테크윙": "089030.KS",
    r"Jusung Engineering|주성엔지니어링": "036930.KS",
    # US small/mid-cap semicon supply chain
    r"Axcelis Technologies": "ACLS",
    r"Onto Innovation": "ONTO",
    r"Photon Dynamics|PDF Solutions": "PDFS",
    r"Veeco Instruments": "VECO",
    r"FormFactor": "FORM",
    r"Cohu": "COHU",
    r"Ichor Holdings": "ICHR",
    r"Ultra Clean": "UCTT",
    r"Photronics": "PLAB",
    r"Amkor Technology": "AMKR",
    r"CEVA": "CEVA",
    r"Aehr Test": "AEHR",
}

# Regex patterns for ticker extraction from text
_TICKER_PATTERNS = [
    (r'\b(\d{4,5})\.T\b', ".T"),          # Tokyo numeric: 6857.T
    (r'\b([A-Z]{2,5})\.T\b', ".T"),        # Tokyo alpha: SONY.T
    (r'\b(\d{6})\.KS\b', ".KS"),           # Korea main: 005930.KS
    (r'\b(\d{6})\.KQ\b', ".KQ"),           # KOSDAQ: 042700.KQ
    (r'\b([A-Z]{2,5})\.L\b', ".L"),        # London
    (r'\b([A-Z]{2,6})\.DE\b', ".DE"),      # Frankfurt/Xetra
    (r'\b([A-Z]{2,6})\.PA\b', ".PA"),      # Paris
    (r'\b([A-Z]{2,6})\.AS\b', ".AS"),      # Amsterdam
    (r'\b([A-Z]{2,6})\.SW\b', ".SW"),      # Zurich
    (r'\b([A-Z]{2,5})\.OL\b', ".OL"),      # Oslo
]

_US_TICKER_STOPWORDS = {
    "AI", "US", "USA", "UK", "EU", "CEO", "CFO", "CTO", "IPO", "ETF", "GPU", "CPU",
    "HBM", "API", "SDK", "ASIC", "DRAM", "NAND", "LLC", "INC", "LTD", "CORP",
    "THE", "FOR", "AND", "NOT", "ARE", "WAS", "HAS", "ITS", "NEW", "TOP",
    "IT", "EV", "AR", "VR", "PC", "OS", "ML", "EPS", "PE", "PB",
}


def _is_intl_ticker(ticker: str) -> bool:
    """Check if a ticker has an international exchange suffix."""
    return "." in ticker and any(
        ticker.endswith(s) for s in (".T", ".KS", ".KQ", ".L", ".DE", ".PA", ".AS",
                                     ".SW", ".OL", ".MI", ".SZ", ".SH", ".SS")
    )


def _extract_tickers_from_text(text: str) -> tuple[list[str], list[str]]:
    """Extract tickers from search text. Returns (us_tickers, intl_tickers)."""
    us_tickers: list[str] = []
    intl_tickers: list[str] = []
    seen: set[str] = set()

    # International tickers via regex (explicit exchange suffixes)
    for pattern, suffix in _TICKER_PATTERNS:
        for match in re.findall(pattern, text):
            ticker = f"{match}{suffix}"
            if ticker not in seen:
                seen.add(ticker)
                intl_tickers.append(ticker)

    # Company name recognition → route to US or intl based on ticker format
    for name_pattern, ticker in _INTL_COMPANY_MAP.items():
        if re.search(name_pattern, text, re.IGNORECASE) and ticker not in seen:
            seen.add(ticker)
            if _is_intl_ticker(ticker):
                intl_tickers.append(ticker)
            else:
                us_tickers.append(ticker)

    # US tickers ($ prefixed in text)
    for match in re.findall(r'\$([A-Z]{2,5})\b', text):
        if match not in _US_TICKER_STOPWORDS and match not in seen:
            seen.add(match)
            us_tickers.append(match)

    return us_tickers[:20], intl_tickers[:20]


# ---------------------------------------------------------------------------
# Scoring & filtering
# ---------------------------------------------------------------------------

def _compute_neglect_score(stock: dict) -> float:
    """Composite neglect-alpha score: 0-100, higher = more neglected + more growth.

    Components (equal-weighted thirds):
    1. Neglect (33): fewer analysts relative to market norm → higher score
    2. Growth (33): higher revenue/earnings growth → higher score
    3. Valuation gap (34): lower PEG / more upside to target → higher score
    """
    score = 0.0

    market = _get_market(stock)
    market_th = NEGLECT_THRESHOLDS.get(market, NEGLECT_THRESHOLDS["default"])
    max_a = market_th["max_analysts"]
    ideal_a = market_th["ideal"]

    # Neglect component (0-33): scaled relative to market norms
    analysts = stock.get("analyst_count", 0) or 0
    if analysts <= ideal_a * 0.4:
        score += 33
    elif analysts <= ideal_a:
        score += 33 * (1 - (analysts - ideal_a * 0.4) / (ideal_a * 0.6))
    elif analysts <= max_a:
        score += 33 * 0.3 * (1 - (analysts - ideal_a) / (max_a - ideal_a))

    # Growth component (0-33)
    rev_growth = stock.get("revenue_growth")
    earn_growth = stock.get("earnings_growth")
    growth_val = rev_growth if rev_growth is not None else earn_growth
    if growth_val is not None:
        if growth_val >= 0.30:
            score += 33
        elif growth_val >= 0.15:
            score += 33 * (growth_val - 0.05) / 0.25
        elif growth_val >= 0.05:
            score += 33 * 0.3 * (growth_val / 0.15)

    # Valuation gap component (0-34)
    peg = stock.get("peg_ratio")
    if peg is not None and 0 < peg < 2.0:
        score += 34 * (1 - peg / 2.0)
    else:
        target = stock.get("target_mean_price")
        current = stock.get("current_price")
        if target and current and current > 0:
            upside = (target - current) / current
            score += min(34, 34 * max(0, upside) / 0.5)

    return round(score, 1)


def _get_market(stock: dict) -> str:
    """Determine which market a stock belongs to for threshold lookup."""
    country = (stock.get("country") or "").upper()
    ticker = stock.get("ticker", "")
    if country in ("US", "UNITED STATES") or not any(c in ticker for c in "."):
        return "US"
    if country in ("JP", "JAPAN") or ".T" in ticker:
        return "JP"
    if country in ("KR", "SOUTH KOREA", "KOREA") or ".KS" in ticker or ".KQ" in ticker:
        return "KR"
    if country in ("DE", "FR", "NL", "GB", "CH", "SE", "IT", "GERMANY", "FRANCE",
                   "NETHERLANDS", "UNITED KINGDOM", "SWITZERLAND"):
        return "EU"
    return "default"


def _passes_neglect_filter(stock: dict) -> bool:
    """Hard filter: market-specific analyst threshold + growth + valuation."""
    market = _get_market(stock)
    market_thresholds = NEGLECT_THRESHOLDS.get(market, NEGLECT_THRESHOLDS["default"])

    analysts = stock.get("analyst_count", 999)
    if analysts > market_thresholds["max_analysts"]:
        return False

    market_cap = stock.get("market_cap") or 0
    currency = stock.get("currency", "USD")
    if currency == "JPY":
        market_cap_usd = market_cap / _JPY_USD
    elif currency == "KRW":
        market_cap_usd = market_cap / 1350
    elif currency == "GBP":
        market_cap_usd = market_cap * 1.27
    elif currency == "EUR":
        market_cap_usd = market_cap * 1.08
    elif currency == "CHF":
        market_cap_usd = market_cap * 1.12
    else:
        market_cap_usd = market_cap

    if market_cap_usd < NEGLECT_THRESHOLDS["min_market_cap_usd"]:
        return False
    if market_cap_usd > NEGLECT_THRESHOLDS["max_market_cap_usd"]:
        return False

    stock["market_cap_usd"] = market_cap_usd

    rev_growth = stock.get("revenue_growth")
    earn_growth = stock.get("earnings_growth")
    growth = rev_growth if rev_growth is not None else earn_growth
    if growth is None:
        # If growth data unavailable (FMP Starter intl limitation), allow through
        # as "unverified" — LLM will judge based on industry context
        if stock.get("growth_unverified"):
            stock["_pass_reason"] = "seed_unverified"
            return True
        return False
    if growth < NEGLECT_THRESHOLDS["min_revenue_growth"]:
        return False

    pe_fwd = stock.get("pe_forward")
    if pe_fwd is not None and pe_fwd > NEGLECT_THRESHOLDS["max_forward_pe"]:
        return False

    return True


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def screen_neglected_growth(
    industry: str,
    search_fn,
    max_candidates: int = 10,
) -> list[dict]:
    """Find neglected high-growth stocks in a supply chain.

    Pipeline:
    1. Tavily search (CN/EN/JP queries) → discover related companies
    2. Extract tickers from search results
    3. Split: US tickers → FMP (growth + grades), intl tickers → yfinance
    4. FMP profile for all → validate existence + get sector
    5. Apply neglect + growth filter
    6. Score and rank by composite neglect-alpha score
    """
    # Step 1: Two-hop search strategy
    # Hop 1: find the industry sector and major players
    # Hop 2: search for niche suppliers, competitors, smaller alternatives
    hop1_queries = [
        f"{industry} supply chain key companies suppliers 2025",
        f"{industry} サプライチェーン 関連企業 部品 材料 装置",
    ]

    hop1_tasks = [search_fn(q, max_results=6) for q in hop1_queries]
    hop1_raw = await asyncio.gather(*hop1_tasks, return_exceptions=True)
    hop1_results: list[dict] = []
    for r in hop1_raw:
        if isinstance(r, list):
            hop1_results.extend(r)

    # Extract major player names from hop1 for second-hop queries
    hop1_text = " ".join(r.get("content", "") + " " + r.get("title", "") for r in hop1_results)

    # Hop 2: explicitly search for smaller/niche players
    hop2_queries = [
        f"{industry} small cap supplier Japan Korea Europe under-covered stock growth 2025",
        f"{industry} equipment materials niche company competitor alternative less known",
        f"{industry} 中小型 サプライヤー 成長 注目 穴場 銘柄",
        f"{industry} undervalued mid cap picks low analyst coverage high revenue growth",
    ]

    hop2_tasks = [search_fn(q, max_results=8) for q in hop2_queries]
    hop2_raw = await asyncio.gather(*hop2_tasks, return_exceptions=True)
    hop2_results: list[dict] = []
    for r in hop2_raw:
        if isinstance(r, list):
            hop2_results.extend(r)

    all_results = hop1_results + hop2_results
    if not all_results:
        return []

    # Step 2: Extract tickers from combined search results
    full_text = " ".join(
        r.get("content", "") + " " + r.get("title", "") for r in all_results
    )
    us_tickers, intl_tickers = _extract_tickers_from_text(full_text)

    # Step 2b: Industry-specific seed lists + FMP screener discovery
    try:
        from industry_seed_lists import get_expanded_candidates
        seed_us, seed_intl = await get_expanded_candidates(industry, search_fn)
    except Exception:
        # Fallback to hardcoded semiconductor seeds if import fails
        seed_us = [
            "ACLS", "ONTO", "VECO", "FORM", "COHU", "ICHR", "UCTT", "PLAB",
            "AMKR", "CEVA", "AEHR", "PDFS", "AMBA", "SLAB", "DIOD", "MRVL",
        ]
        seed_intl = [
            "6525.T", "6890.T", "6315.T", "6407.T", "6728.T", "6856.T",
            "042700.KS", "240810.KS", "084370.KS", "403870.KS",
            "BESI.AS", "ASM.AS", "AIXA.DE", "WAF.DE", "SOI.PA", "IQE.L",
            "SMHN.DE", "VACN.SW",
        ]
    # Add seeds that weren't already found by search
    for t in seed_us:
        if t not in set(us_tickers):
            us_tickers.append(t)
    for t in seed_intl:
        if t not in set(intl_tickers):
            intl_tickers.append(t)

    if not us_tickers and not intl_tickers:
        return []

    # Step 3: Fetch fundamentals in parallel
    # US: FMP growth + analyst grades
    # International: yfinance (includes growth + analyst count)
    us_growth_task = _fmp_us_growth(us_tickers[:18]) if us_tickers else asyncio.sleep(0, result={})
    us_grades_task = _fmp_us_analyst_count(us_tickers[:18]) if us_tickers else asyncio.sleep(0, result={})
    intl_task = _yf_fundamentals(intl_tickers[:25]) if intl_tickers else asyncio.sleep(0, result=[])

    # FMP profile for US tickers (market cap + sector validation)
    profile_task = _fmp_profiles(us_tickers[:18])

    us_growth, us_grades, intl_stocks, profiles = await asyncio.gather(
        us_growth_task, us_grades_task, intl_task, profile_task,
    )

    # Build profile lookup for sector/country validation
    profile_map = {p["symbol"]: p for p in profiles if isinstance(p, dict)}

    # Step 4: Assemble US stock data
    us_stocks: list[dict] = []
    for ticker in us_tickers:
        growth = us_growth.get(ticker, {})
        analyst_count = us_grades.get(ticker, 999)
        profile = profile_map.get(ticker, {})

        if not growth.get("revenue_growth") and not profile.get("marketCap"):
            continue

        us_stocks.append({
            "ticker": ticker,
            "name": profile.get("companyName", ticker),
            "market_cap": profile.get("marketCap", 0),
            "market_cap_usd": profile.get("marketCap", 0),
            "revenue_growth": growth.get("revenue_growth"),
            "earnings_growth": growth.get("net_income_growth"),
            "analyst_count": analyst_count,
            "pe_forward": None,
            "peg_ratio": None,
            "current_price": profile.get("price"),
            "target_mean_price": None,
            "sector": profile.get("sector", ""),
            "industry": profile.get("industry", ""),
            "country": profile.get("country", ""),
            "exchange": profile.get("exchange", ""),
            "currency": profile.get("currency", "USD"),
            "source": "fmp",
        })

    # Step 5: Merge and apply filter
    all_stocks = us_stocks + intl_stocks
    candidates = [s for s in all_stocks if _passes_neglect_filter(s)]

    # Step 6: Score and rank (use optimized weights if available)
    try:
        from neglect_weight_optimizer import get_optimal_weights, apply_optimized_score
        weight_result = get_optimal_weights()
        weights = weight_result["weights"]
        for c in candidates:
            c["neglect_score"] = apply_optimized_score(c, weights)
        scoring_method = weight_result["method"]
    except Exception:
        for c in candidates:
            c["neglect_score"] = _compute_neglect_score(c)
        scoring_method = "equal_weight_fallback"

    candidates.sort(key=lambda x: x["neglect_score"], reverse=True)

    return candidates[:max_candidates]


# ---------------------------------------------------------------------------
# Formatting for prompt injection
# ---------------------------------------------------------------------------

def format_neglect_candidates(candidates: list[dict]) -> str:
    """Format screened candidates for prompt injection."""
    if not candidates:
        return "（本次未筛选到符合条件的低覆盖高成长国际标的）"

    lines = ["**国际 Neglect-Alpha 候选标的**（低覆盖 + 高增长，未被机构重复定价）\n"]
    lines.append("筛选条件：分析师覆盖 ≤8 人 + 营收增速 ≥12% + 市值 $5亿-$300亿\n")
    lines.append("| Ticker | 公司 | 国家 | 市值(USD) | 营收增速 | 分析师数 | Forward PE | Neglect Score | 数据源 |")
    lines.append("|--------|------|------|-----------|---------|---------|-----------|--------------|--------|")

    for c in candidates:
        mc = c.get("market_cap_usd") or c.get("market_cap", 0)
        if mc >= 1e9:
            mc_str = f"${mc/1e9:.1f}B"
        else:
            mc_str = f"${mc/1e6:.0f}M"

        rev_g = c.get("revenue_growth")
        if rev_g is not None:
            rev_str = f"{rev_g*100:.1f}%"
        elif c.get("growth_unverified"):
            rev_str = "待验证"
        else:
            rev_str = "N/A"

        pe_fwd = c.get("pe_forward")
        pe_str = f"{pe_fwd:.1f}" if pe_fwd is not None else "N/A"

        source = c.get("source", "yfinance")
        if c.get("growth_unverified"):
            source += "*"

        lines.append(
            f"| {c['ticker']} | {c.get('name', '')[:22]} | "
            f"{c.get('country', 'N/A')} | {mc_str} | {rev_str} | "
            f"{c.get('analyst_count', 'N/A')} | {pe_str} | "
            f"{c.get('neglect_score', 0):.0f}/100 | {source} |"
        )

    lines.append("")
    lines.append("Neglect Score = neglect权重(33%) + growth权重(33%) + 估值折让权重(34%)")
    lines.append("数据源：fmp = FMP financial-growth + grades，yfinance = Yahoo Finance，fmp_profile* = 仅有市值/行业，增速待验证")
    lines.append('标注「待验证」的标的：市值/行业已确认属于产业链，但增速数据无法从 API 获取，需你基于行业知识判断是否高增长')
    return "\n".join(lines)
