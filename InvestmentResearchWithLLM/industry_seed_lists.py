"""Industry-specific seed lists + FMP screener-based candidate discovery

替代原来 intl_screener.py 里硬编码的半导体 seed list。
按产业链维护多个 seed list，并新增 FMP Stock Screener 作为结构化发现源。

FMP Screener (Starter plan):
  /stable/stock-screener?marketCapMoreThan=5e8&marketCapLessThan=3e10
  &sector=Technology&limit=100&apikey=xxx

支持的 filters:
  sector, industry, marketCap range, country, exchange, isActivelyTrading
"""
import asyncio
import os
from typing import Optional

import httpx

from data_providers.ticker_utils import safe_float
from data_providers.intl_screener import _SSL_VERIFY


_FMP_BASE = "https://financialmodelingprep.com"


# ---------------------------------------------------------------------------
# Industry → Seed List mapping
# ---------------------------------------------------------------------------

INDUSTRY_SEEDS: dict[str, dict[str, list[str]]] = {
    # AI & 算力
    "AI": {
        "us": [
            "ACLS", "ONTO", "VECO", "FORM", "COHU", "ICHR", "UCTT", "PLAB",
            "AMKR", "CEVA", "AEHR", "PDFS", "AMBA", "SLAB", "DIOD", "SMCI",
            "AI", "PLTR", "PATH", "DDOG", "SNOW", "NET",
        ],
        "intl": [
            "6525.T", "6890.T", "6315.T", "6407.T", "6728.T", "6856.T",
            "8035.T", "6857.T", "6146.T", "6920.T",
            "042700.KS", "240810.KS", "084370.KS", "403870.KS",
            "BESI.AS", "ASM.AS", "AIXA.DE", "WAF.DE", "SOI.PA", "IQE.L",
            "SMHN.DE", "VACN.SW",
        ],
    },
    # 半导体
    "半导体": {
        "us": [
            "ACLS", "ONTO", "VECO", "FORM", "COHU", "ICHR", "UCTT", "PLAB",
            "AMKR", "CEVA", "AEHR", "PDFS", "MRVL", "AVGO", "KLAC", "LRCX",
        ],
        "intl": [
            "6525.T", "6890.T", "6315.T", "6407.T", "6728.T", "6856.T",
            "8035.T", "6857.T", "6146.T", "6920.T", "6723.T", "6963.T",
            "6981.T", "7735.T", "4062.T", "6967.T",
            "005930.KS", "000660.KS", "042700.KS", "240810.KS",
            "084370.KS", "403870.KS", "089030.KS", "036930.KS",
            "ASML", "IFX.DE", "STMPA.PA", "BESI.AS", "ASM.AS",
            "AIXA.DE", "WAF.DE", "SOI.PA", "SMHN.DE",
        ],
    },
    # 新能源 / 光伏 / 储能
    "新能源": {
        "us": [
            "ENPH", "SEDG", "FSLR", "RUN", "NOVA", "ARRY", "MAXN",
            "STEM", "FLUX", "AMPS", "QS", "FREYR", "AESC",
        ],
        "intl": [
            "6501.T", "6506.T",  # Hitachi, Yaskawa
            "373220.KS", "006400.KS",  # LG Energy, Samsung SDI
            "096770.KS", "247540.KS",  # SK Innovation, ECOPRO BM
            "EDPR.LS", "ORSTED.CO",    # EDP Renewables, Ørsted
            "VWS.CO",                   # Vestas
            "002459.SZ", "300750.SZ",  # 天合光能, 宁德时代
            "600438.SH",               # 通威股份
        ],
    },
    # 生物医药 / Biotech
    "生物医药": {
        "us": [
            "IONS", "ALNY", "SRPT", "BMRN", "RARE", "RCKT", "DAWN",
            "DNLI", "PRAX", "CRNX", "KRYS", "RYTM", "PCVX", "IMVT",
        ],
        "intl": [
            "4568.T", "4519.T", "4523.T",  # 第一三共, 中外制药, エーザイ
            "4503.T",                        # アステラス
            "207940.KS", "068270.KS",       # Samsung Biologics, Celltrion
            "NOVO-B.CO", "AZN.L",           # Novo Nordisk, AstraZeneca
            "ROG.SW", "NOVN.SW",            # Roche, Novartis
            "SAN.PA",                        # Sanofi
        ],
    },
    # 机器人 / 工业自动化
    "机器人": {
        "us": [
            "ISRG", "ROCK", "TER", "CGNX", "NOVT", "BRKS", "OFIX",
            "ANET", "ROK",
        ],
        "intl": [
            "6861.T", "6594.T", "6954.T", "6273.T", "6324.T",  # Keyence, Nidec, Fanuc, SMC, Harmonic
            "6383.T", "6506.T",  # Daifuku, Yaskawa
            "6902.T",            # DENSO
            "KUKA.DE",           # KUKA (if still listed)
            "ABB.SW",            # ABB
            "058470.KS",         # LEENO Industrial
        ],
    },
    # 电动车 / EV
    "电动车": {
        "us": [
            "TSLA", "RIVN", "LCID", "XPEV", "LI", "NIO",
            "APTV", "ALB", "LAC", "MP", "WOLF", "ON",
        ],
        "intl": [
            "7203.T", "7267.T",  # Toyota, Honda
            "373220.KS", "006400.KS", "051910.KS",  # LG Energy, Samsung SDI, LG Chem
            "002594.SZ", "300750.SZ",  # BYD, CATL
            "VOW3.DE",                  # VW
            "BMW.DE",                   # BMW
        ],
    },
    # 网络安全
    "网络安全": {
        "us": [
            "CRWD", "ZS", "PANW", "FTNT", "S", "CYBR", "TENB",
            "QLYS", "RPD", "VRNS", "NET", "OKTA",
        ],
        "intl": [
            "6702.T",    # Fujitsu
            "4704.T",    # Trend Micro
            "DRK.DE",    # Darktrace (may move)
            "CYBER.TA",  # CyberArk Israel
        ],
    },
    # SaaS / 云计算
    "云计算": {
        "us": [
            "DDOG", "SNOW", "MDB", "CFLT", "ESTC", "PATH", "BRZE",
            "DOCN", "FSLY", "GTLB", "HCP", "SUMO",
        ],
        "intl": [
            "4478.T",    # Freee
            "4776.T",    # Cybozu
            "BC8.DE",    # Bechtle
            "NEM.DE",    # Nemetschek
            "DNET.L",    # Darktrace
        ],
    },
}

# Keyword matching for fuzzy industry → seed list lookup
_INDUSTRY_ALIASES: dict[str, str] = {
    "算力": "AI",
    "人工智能": "AI",
    "芯片": "半导体",
    "semiconductor": "半导体",
    "chip": "半导体",
    "光伏": "新能源",
    "solar": "新能源",
    "储能": "新能源",
    "battery": "新能源",
    "energy storage": "新能源",
    "biotech": "生物医药",
    "pharma": "生物医药",
    "制药": "生物医药",
    "robot": "机器人",
    "automation": "机器人",
    "自动化": "机器人",
    "ev": "电动车",
    "electric vehicle": "电动车",
    "新能源汽车": "电动车",
    "cybersecurity": "网络安全",
    "security": "网络安全",
    "信息安全": "网络安全",
    "cloud": "云计算",
    "saas": "云计算",
}


def get_seed_list(industry: str) -> tuple[list[str], list[str]]:
    """Get US and international seed tickers for an industry.

    Returns (us_seeds, intl_seeds). Falls back to empty lists for unknown industries.
    """
    # Direct match
    if industry in INDUSTRY_SEEDS:
        seeds = INDUSTRY_SEEDS[industry]
        return seeds.get("us", []), seeds.get("intl", [])

    # Fuzzy match via aliases
    industry_lower = industry.lower()
    for alias, canonical in _INDUSTRY_ALIASES.items():
        if alias in industry_lower:
            seeds = INDUSTRY_SEEDS.get(canonical, {})
            return seeds.get("us", []), seeds.get("intl", [])

    # Partial match in INDUSTRY_SEEDS keys
    for key, seeds in INDUSTRY_SEEDS.items():
        if key in industry or industry in key:
            return seeds.get("us", []), seeds.get("intl", [])

    return [], []


# ---------------------------------------------------------------------------
# FMP Screener — structured candidate discovery
# ---------------------------------------------------------------------------

# Map industry keywords to FMP sector/industry strings
_SECTOR_MAP: dict[str, dict] = {
    "AI": {"sector": "Technology", "industry": "Semiconductors"},
    "半导体": {"sector": "Technology", "industry": "Semiconductors"},
    "新能源": {"sector": "Energy"},
    "生物医药": {"sector": "Healthcare"},
    "机器人": {"sector": "Industrials"},
    "电动车": {"sector": "Consumer Cyclical", "industry": "Auto Manufacturers"},
    "网络安全": {"sector": "Technology", "industry": "Software - Infrastructure"},
    "云计算": {"sector": "Technology", "industry": "Software - Application"},
}


async def fmp_screen_candidates(
    industry: str,
    min_market_cap: float = 5e8,
    max_market_cap: float = 3e10,
    country: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Use FMP Stock Screener to find candidates by sector + market cap range.

    This provides structured discovery beyond what text search can find.
    """
    api_key = os.getenv("FMP_API_KEY", "")
    if not api_key:
        return []

    # Resolve sector/industry filter
    sector_filter = None
    industry_lower = industry.lower()
    for key, mapping in _SECTOR_MAP.items():
        if key in industry or any(
            alias in industry_lower
            for alias, canonical in _INDUSTRY_ALIASES.items()
            if canonical == key
        ):
            sector_filter = mapping
            break

    if not sector_filter:
        sector_filter = {"sector": "Technology"}

    params = {
        "marketCapMoreThan": int(min_market_cap),
        "marketCapLessThan": int(max_market_cap),
        "isActivelyTrading": "true",
        "limit": limit,
        "apikey": api_key,
    }
    if sector_filter.get("sector"):
        params["sector"] = sector_filter["sector"]
    if sector_filter.get("industry"):
        params["industry"] = sector_filter["industry"]
    if country:
        params["country"] = country

    try:
        async with httpx.AsyncClient(timeout=15.0, verify=_SSL_VERIFY) as c:
            r = await c.get(f"{_FMP_BASE}/stable/stock-screener", params=params)
            if r.status_code != 200:
                return []
            data = r.json()
            if not isinstance(data, list):
                return []
    except Exception:
        return []

    results = []
    for item in data:
        ticker = item.get("symbol", "")
        if not ticker:
            continue
        results.append({
            "ticker": ticker,
            "name": item.get("companyName", ticker),
            "market_cap": item.get("marketCap"),
            "sector": item.get("sector", ""),
            "industry": item.get("industry", ""),
            "country": item.get("country", ""),
            "exchange": item.get("exchangeShortName", ""),
            "price": item.get("price"),
            "source": "fmp_screener",
        })

    return results


async def get_expanded_candidates(
    industry: str,
    search_fn=None,
) -> tuple[list[str], list[str]]:
    """Get expanded candidate list: seed list + FMP screener results.

    Returns (us_tickers, intl_tickers) with FMP screener results merged in.
    """
    us_seeds, intl_seeds = get_seed_list(industry)

    # FMP screener for US stocks (more comprehensive than seeds)
    screened = await fmp_screen_candidates(industry, limit=30)
    screened_us = [s["ticker"] for s in screened if s.get("ticker") and "." not in s["ticker"]]

    # Merge: seeds first (curated), then screener additions
    us_seen = set(us_seeds)
    for t in screened_us:
        if t not in us_seen:
            us_seeds.append(t)
            us_seen.add(t)

    return us_seeds, intl_seeds
