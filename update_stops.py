"""每日定时任务：基于 ATR 计算止盈止损，写入 positions 表

用法：
  python update_stops.py          # 直接运行
  cron: 0 9 * * 1-5 cd /opt/holder-action && .venv/bin/python update_stops.py

算法 (Plan A)：
  止损锚点 = entry_price（非 current_price，防止下移）
  基础止损 = entry_price - 2.0 × ATR(14)
  Hard cap  = entry_price × 0.92（单仓最大亏 8%）
  Trailing  = 浮盈超 1R 后，使用 chandelier exit: current_price - 2.0 × ATR
  Ratchet   = 止损只能收紧，不能放松（新值 < 旧值时保留旧值）

  止盈分两级：
    target_1 = entry_price + 2.0 × ATR (1R，建议减半仓)
    target_2 = entry_price + 4.0 × ATR (2R，清仓或转 trailing)
"""
import os
import sys
import sqlite3
import requests
import numpy as np
from datetime import datetime

DB_PATH = os.environ.get(
    "HOLDER_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "trading.db"),
)

_HEADERS = {"User-Agent": "Mozilla/5.0"}
_PROXY = {"https": "socks5h://127.0.0.1:1080", "http": "socks5h://127.0.0.1:1080"}

# ── 参数 ──
ATR_PERIOD = 14
STOP_MULT = 2.0
TARGET_1_MULT = 2.0
TARGET_2_MULT = 4.0
MAX_LOSS_PCT = 0.08  # 单仓最大亏损 8%
TRAILING_THRESHOLD_R = 1.0  # 浮盈超 1R 后启动 trailing

# ── Polymarket regime 动态调整 ──
_RAG_API_URL = os.getenv("RAG_API_URL", "http://43.139.5.125:8080")
_RAG_API_KEY = os.getenv("RAG_API_KEY", "")


def _get_polymarket_regime() -> dict:
    """获取 Polymarket risk regime，用于动态调整止损参数。
    返回 {"regime": str, "position_multiplier": float, "stop_mult_adj": float}
    """
    default = {"regime": "neutral", "position_multiplier": 1.0, "stop_mult_adj": 0.0}
    if not _RAG_API_KEY:
        return default
    try:
        resp = requests.get(
            f"{_RAG_API_URL}/api/v1/risk/polymarket",
            headers={"X-API-Key": _RAG_API_KEY},
            params={"days": 7},
            timeout=5,
        )
        if resp.status_code != 200:
            return default
        data = resp.json()
        regime = data.get("regime", "neutral")
        mult = data.get("position_multiplier", 1.0)
        # defensive → 收紧止损 0.5 ATR; hawkish → 收紧 0.3 ATR
        adj = {"defensive": -0.5, "hawkish": -0.3}.get(regime, 0.0)
        return {"regime": regime, "position_multiplier": mult, "stop_mult_adj": adj}
    except Exception:
        return default


def fetch_ohlc(ticker: str, days: int = 100) -> list[dict]:
    yf_ticker = ticker.replace(".US", "").replace(".SH", ".SS")
    if ticker.upper() == "BTC":
        yf_ticker = "BTC-USD"
    # Yahoo Finance .HK tickers don't use leading zeros (07709.HK → 7709.HK)
    if yf_ticker.endswith(".HK"):
        code = yf_ticker.split(".")[0].lstrip("0")
        yf_ticker = f"{code}.HK" if code else yf_ticker
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_ticker}?interval=1d&range=6mo"
    attempts = [
        {"proxies": None, "verify": True},
        {"proxies": None, "verify": False},
        {"proxies": _PROXY, "verify": True},
    ]
    for kwargs in attempts:
        try:
            r = requests.get(url, headers=_HEADERS, timeout=15, **kwargs)
            data = r.json()
            result = data["chart"]["result"][0]
            timestamps = result["timestamp"]
            quotes = result["indicators"]["quote"][0]
            rows = []
            for i, ts in enumerate(timestamps):
                h = quotes["high"][i]
                l = quotes["low"][i]
                c = quotes["close"][i]
                if h is None or l is None or c is None:
                    continue
                rows.append({"high": h, "low": l, "close": c})
            return rows[-days:]
        except Exception:
            continue
    return []


def calc_atr(ohlc: list[dict], period: int = ATR_PERIOD) -> float | None:
    if len(ohlc) < period + 1:
        return None
    highs = np.array([r["high"] for r in ohlc])
    lows = np.array([r["low"] for r in ohlc])
    closes = np.array([r["close"] for r in ohlc])
    prev_close = np.roll(closes, 1)
    prev_close[0] = closes[0]
    tr = np.maximum(highs - lows, np.maximum(np.abs(highs - prev_close), np.abs(lows - prev_close)))
    return float(np.mean(tr[-period:]))


def compute_stops(
    current_price: float,
    entry_price: float,
    direction: str,
    ohlc: list[dict],
    existing_stop: float | None = None,
    stop_mult_adj: float = 0.0,
) -> dict | None:
    """Plan A 止盈止损算法
    stop_mult_adj: Polymarket regime 导致的 ATR 倍数调整（负值 = 收紧止损）
    """
    atr = calc_atr(ohlc)
    if atr is None or atr == 0:
        return None

    effective_stop_mult = max(1.0, STOP_MULT + stop_mult_adj)

    if direction == "long":
        # ── 止损 ──
        # 1. 基础止损：以 entry_price 为锚
        base_stop = entry_price - effective_stop_mult * atr

        # 2. Hard cap: 最大亏损不超过 MAX_LOSS_PCT
        max_loss_stop = entry_price * (1 - MAX_LOSS_PCT)

        # 取较紧者（离现价更近）
        stop_loss = max(base_stop, max_loss_stop)

        # 3. Chandelier trailing: 浮盈超 1R 后，用 current_price - mult*ATR
        r_value = effective_stop_mult * atr  # 1R = 止损距离
        if current_price > entry_price + TRAILING_THRESHOLD_R * r_value:
            trailing_stop = current_price - effective_stop_mult * atr
            stop_loss = max(stop_loss, trailing_stop)

        # 4. 价格已跌破常规止损位（仓位仍开着）→ 用 current_price 作锚的应急止损
        if stop_loss >= current_price:
            stop_loss = current_price - 1.0 * atr

        # 5. Ratchet: 止损只能收紧（上移），不能放松（下移）
        if existing_stop and existing_stop > stop_loss:
            stop_loss = existing_stop

        # ── 止盈（两级） ──
        target_1 = entry_price + TARGET_1_MULT * atr
        target_2 = entry_price + TARGET_2_MULT * atr

        # 如果现价已超过 T1，把 T1 上移（继续追踪）
        if current_price >= target_1:
            target_1 = current_price + 1.0 * atr
        # T2 始终 >= T1
        if target_2 < target_1:
            target_2 = target_1 + TARGET_1_MULT * atr

        # Sanity: stop 必须低于现价
        if stop_loss >= current_price:
            return None

    else:  # short
        base_stop = entry_price + effective_stop_mult * atr
        max_loss_stop = entry_price * (1 + MAX_LOSS_PCT)
        stop_loss = min(base_stop, max_loss_stop)

        r_value = effective_stop_mult * atr
        if current_price < entry_price - TRAILING_THRESHOLD_R * r_value:
            trailing_stop = current_price + effective_stop_mult * atr
            stop_loss = min(stop_loss, trailing_stop)

        if stop_loss <= current_price:
            stop_loss = current_price + 1.0 * atr

        if existing_stop and existing_stop < stop_loss:
            stop_loss = existing_stop

        target_1 = entry_price - TARGET_1_MULT * atr
        target_2 = entry_price - TARGET_2_MULT * atr

        if current_price <= target_1:
            target_1 = current_price - 1.0 * atr
        if target_2 > target_1:
            target_2 = target_1 - TARGET_1_MULT * atr

        if stop_loss <= current_price:
            return None

    return {
        "stop_loss": round(stop_loss, 2),
        "profit_target": round(target_1, 2),
        "profit_target_2": round(target_2, 2),
    }


def _ensure_column(conn, table: str, column: str, col_type: str = "REAL"):
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        print(f"  [migrate] added {table}.{column}")


def main():
    if not os.path.exists(DB_PATH):
        print(f"DB not found: {DB_PATH}")
        sys.exit(1)

    # 获取 Polymarket regime 动态调整止损
    regime_info = _get_polymarket_regime()
    regime = regime_info["regime"]
    stop_adj = regime_info["stop_mult_adj"]

    if regime != "neutral":
        print(f"  [polymarket] regime={regime}, stop_mult adjustment={stop_adj:+.1f} ATR")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_column(conn, "positions", "current_price")
    _ensure_column(conn, "positions", "profit_target_2")
    positions = conn.execute(
        "SELECT id, ticker, asset, entry_price, direction, current_price, stop_loss, profit_target_2 "
        "FROM positions WHERE status = 'open'"
    ).fetchall()

    if not positions:
        print("No open positions.")
        conn.close()
        return

    updated = 0
    failed = []

    for pos in positions:
        ticker = pos["ticker"] or pos["asset"]
        direction = pos["direction"] or "long"
        entry_price = float(pos["entry_price"]) if pos["entry_price"] else None

        if not entry_price:
            failed.append(f"{ticker}(no entry_price)")
            continue

        ohlc = fetch_ohlc(ticker)

        if ohlc:
            price_from_api = float(ohlc[-1]["close"])
        else:
            price_from_api = None

        current_price = price_from_api or pos["current_price"] or entry_price
        current_price = float(current_price)

        if not ohlc:
            failed.append(f"{ticker}(no ohlc)")
            continue

        existing_stop = float(pos["stop_loss"]) if pos["stop_loss"] else None

        result = compute_stops(
            current_price=current_price,
            entry_price=entry_price,
            direction=direction,
            ohlc=ohlc,
            existing_stop=existing_stop,
            stop_mult_adj=stop_adj,
        )
        if not result:
            failed.append(f"{ticker}(sanity fail)")
            continue

        conn.execute(
            "UPDATE positions SET current_price = ?, stop_loss = ?, profit_target = ?, profit_target_2 = ?, updated_at = ? WHERE id = ?",
            (current_price, result["stop_loss"], result["profit_target"], result["profit_target_2"], datetime.now().isoformat(), pos["id"]),
        )
        updated += 1

        if direction == "long":
            risk = current_price - result["stop_loss"]
            reward = result["profit_target"] - current_price
        else:
            risk = result["stop_loss"] - current_price
            reward = current_price - result["profit_target"]
        rr = reward / risk if risk > 0 else 0

        print(f"  {ticker}: price=${current_price:.2f} stop=${result['stop_loss']} T1=${result['profit_target']} T2=${result['profit_target_2']} R:R=1:{rr:.1f}")

    conn.commit()
    conn.close()

    print(f"\nDone: {updated} updated, {len(failed)} failed ({', '.join(failed) if failed else 'none'})")


if __name__ == "__main__":
    main()
