import os
import json
import sqlite3
from datetime import datetime, timedelta

_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "reports.db"
)

_FIN_CACHE_TTL_DAYS = 3
_PRICE_CACHE_TTL_HOURS = 24


def _init():
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
            series_json TEXT NOT NULL,
            fetched_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def fin_cache_get(ticker: str) -> dict | None:
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


def fin_cache_set(ticker: str, data: dict):
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


def price_cache_get(ticker: str) -> dict | None:
    try:
        conn = sqlite3.connect(_DB_PATH)
        row = conn.execute(
            "SELECT series_json, expires_at FROM price_history_cache WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        conn.close()
        if row and row[1] > datetime.utcnow().isoformat():
            return json.loads(row[0])
    except Exception:
        pass
    return None


def price_cache_set(ticker: str, series: dict):
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
    except Exception:
        pass


try:
    _init()
except Exception:
    pass
