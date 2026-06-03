"""每日定时任务：基于 ATR 计算止盈止损，写入 positions 表

用法：
  python update_stops.py          # 直接运行
  cron: 0 9 * * 1-5 cd /opt/holder-action && .venv/bin/python update_stops.py

算法 (Plan A - 技术面)：
  止损锚点 = entry_price（非 current_price，防止下移）
  基础止损 = entry_price - 2.0 × ATR(14)
  Hard cap  = entry_price × 0.92（单仓最大亏 8%）
  Trailing  = 浮盈超 1R 后，使用 chandelier exit: current_price - 2.0 × ATR
  Ratchet   = 止损只能收紧，不能放松（新值 < 旧值时保留旧值）

  止盈分两级：
    target_1 = entry_price + 2.0 × ATR (1R，建议减半仓)
    target_2 = entry_price + 4.0 × ATR (2R，清仓或转 trailing)

算法 (Plan B - 基本面 overlay)：
  估值分位: P/E vs 5年历史 → 偏贵收紧止盈，偏便宜放宽止损
  盈利趋势: EPS 连续下降 → 收紧止损
  财报日历: ≤7天 → 收紧止损
  分析师目标: consensus target 作为止盈上限参考
"""
import os
import sys
import sqlite3
import requests
import numpy as np
from datetime import datetime, date

DB_PATH = os.environ.get(
    "HOLDER_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "trading.db"),
)

_FMP_API_KEY = os.environ.get("FMP_API_KEY", "KJdh1OAPcCWP8cRveZjXZG64ibk8iGHt")
_FMP_BASE = "https://financialmodelingprep.com/stable"

# ── 参数 ──
ATR_PERIOD = 14
STOP_MULT = 2.0
TARGET_1_MULT = 2.0
TARGET_2_MULT = 4.0
MAX_LOSS_PCT = 0.08  # 单仓最大亏损 8%
TRAILING_THRESHOLD_R = 1.0  # 浮盈超 1R 后启动 trailing

# ── 基本面调整参数 ──
FUNDAMENTAL_MAX_ADJ = 1.0  # 基本面总调整上限: ±1.0 ATR
US_STOCK_TICKERS = {"AAPL", "MSFT", "NVDA", "META", "AMZN", "GOOGL", "GOOG", "TSLA"}

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


# ── 基本面数据获取 ──

_fundamental_cache: dict = {}  # ticker → {data, fetched_date}


def _is_us_stock(ticker: str) -> bool:
    t = ticker.upper().replace(".US", "")
    return t in US_STOCK_TICKERS


def fetch_fundamental_data(ticker: str) -> dict | None:
    """从 FMP 获取基本面数据：估值/EPS趋势/财报日期/分析师目标价。
    仅对美股个股有效，ETF/商品/加密币返回 None。
    同一 ticker 当天只拉一次（缓存）。
    """
    t = ticker.upper().replace(".US", "")
    if not _is_us_stock(ticker):
        return None

    today = date.today().isoformat()
    cached = _fundamental_cache.get(t)
    if cached and cached.get("fetched_date") == today:
        return cached.get("data")

    result = {}
    try:
        # 1. 当前估值 + 历史 P/E
        resp = requests.get(
            f"{_FMP_BASE}/ratios-ttm",
            params={"symbol": t, "apikey": _FMP_API_KEY},
            timeout=10,
        )
        if resp.status_code == 200:
            ratios = resp.json()
            if isinstance(ratios, list) and ratios:
                result["pe_ratio"] = ratios[0].get("peRatioTTM")

        # 历史 P/E (5年)
        resp2 = requests.get(
            f"{_FMP_BASE}/ratios",
            params={"symbol": t, "apikey": _FMP_API_KEY, "period": "annual", "limit": 5},
            timeout=10,
        )
        if resp2.status_code == 200:
            hist_ratios = resp2.json()
            if isinstance(hist_ratios, list) and len(hist_ratios) >= 3:
                pe_history = [r.get("priceEarningsRatio") for r in hist_ratios if r.get("priceEarningsRatio")]
                if pe_history:
                    result["pe_history"] = pe_history
                    current_pe = result.get("pe_ratio")
                    if current_pe and pe_history:
                        sorted_pe = sorted(pe_history)
                        rank = sum(1 for x in sorted_pe if x <= current_pe)
                        result["pe_percentile"] = rank / len(sorted_pe)

        # 2. EPS 趋势 (最近 4 季度)
        resp3 = requests.get(
            f"{_FMP_BASE}/income-statement",
            params={"symbol": t, "apikey": _FMP_API_KEY, "period": "quarter", "limit": 4},
            timeout=10,
        )
        if resp3.status_code == 200:
            income = resp3.json()
            if isinstance(income, list) and len(income) >= 2:
                eps_list = [q.get("eps") for q in income if q.get("eps") is not None]
                if len(eps_list) >= 2:
                    result["eps_trend"] = eps_list  # 最新在前
                    declining = all(eps_list[i] < eps_list[i+1] for i in range(min(2, len(eps_list)-1)))
                    result["eps_declining"] = declining

        # 3. 财报日期
        resp4 = requests.get(
            f"{_FMP_BASE}/earning-calendar",
            params={"symbol": t, "apikey": _FMP_API_KEY, "limit": 1},
            timeout=10,
        )
        if resp4.status_code == 200:
            earnings = resp4.json()
            if isinstance(earnings, list) and earnings:
                earn_date_str = earnings[0].get("date")
                if earn_date_str:
                    try:
                        earn_date = date.fromisoformat(earn_date_str)
                        days_to_earnings = (earn_date - date.today()).days
                        if days_to_earnings >= 0:
                            result["days_to_earnings"] = days_to_earnings
                            result["earnings_date"] = earn_date_str
                    except ValueError:
                        pass

        # 4. 分析师目标价
        resp5 = requests.get(
            f"{_FMP_BASE}/price-target-consensus",
            params={"symbol": t, "apikey": _FMP_API_KEY},
            timeout=10,
        )
        if resp5.status_code == 200:
            targets = resp5.json()
            if isinstance(targets, list) and targets:
                result["analyst_target"] = targets[0].get("targetConsensus")
                result["analyst_high"] = targets[0].get("targetHigh")
                result["analyst_low"] = targets[0].get("targetLow")

    except Exception as e:
        print(f"  [fundamental] {t}: fetch error: {e}")

    _fundamental_cache[t] = {"data": result if result else None, "fetched_date": today}
    return result if result else None


def compute_fundamental_adjustment(fundamental: dict | None, atr: float, direction: str) -> dict:
    """基于基本面数据计算止盈止损调整量（以价格为单位）。
    返回 {stop_adj, target_adj, target_cap, factors}
      stop_adj: 负值=收紧止损, 正值=放宽止损
      target_adj: 负值=提前止盈, 正值=推迟止盈
      target_cap: 分析师目标价上限 (None=不限)
      factors: 触发的因子列表（用于展示）
    """
    result = {"stop_adj": 0.0, "target_adj": 0.0, "target_cap": None, "factors": []}
    if not fundamental or atr == 0:
        return result

    stop_adj = 0.0
    target_adj = 0.0

    # 因子1: 估值分位数
    pe_pctl = fundamental.get("pe_percentile")
    if pe_pctl is not None:
        if pe_pctl >= 0.8:
            # 估值偏贵 → 提前止盈
            target_adj -= 0.5 * atr
            result["factors"].append(f"估值偏贵(P/E>{pe_pctl*100:.0f}th) → 止盈收紧0.5ATR")
        elif pe_pctl <= 0.2:
            # 估值便宜 → 放宽止损，推迟止盈
            stop_adj += 0.3 * atr
            target_adj += 1.0 * atr
            result["factors"].append(f"估值偏低(P/E<{pe_pctl*100:.0f}th) → 止损放宽0.3ATR, 止盈延伸1.0ATR")

    # 因子2: EPS 趋势恶化
    if fundamental.get("eps_declining"):
        stop_adj -= 0.5 * atr
        target_adj -= 0.5 * atr
        result["factors"].append("EPS连续下降 → 止损/止盈均收紧0.5ATR")

    # 因子3: 临近财报
    days_to_earn = fundamental.get("days_to_earnings")
    if days_to_earn is not None and days_to_earn <= 7:
        stop_adj -= 0.5 * atr
        result["factors"].append(f"距财报仅{days_to_earn}天 → 止损收紧0.5ATR")

    # 因子4: 分析师目标价
    analyst_target = fundamental.get("analyst_target")
    if analyst_target and analyst_target > 0:
        result["target_cap"] = analyst_target * 1.05
        result["factors"].append(f"分析师共识目标${analyst_target:.2f} → T2上限${analyst_target*1.05:.2f}")

    # Clamp: 单因子已各自限制，总调整也做 cap
    stop_adj = max(-FUNDAMENTAL_MAX_ADJ * atr, min(FUNDAMENTAL_MAX_ADJ * atr, stop_adj))
    target_adj = max(-FUNDAMENTAL_MAX_ADJ * atr, min(FUNDAMENTAL_MAX_ADJ * atr, target_adj))

    # short 方向取反
    if direction == "short":
        stop_adj = -stop_adj
        target_adj = -target_adj

    result["stop_adj"] = round(stop_adj, 4)
    result["target_adj"] = round(target_adj, 4)
    return result


def _fetch_ohlc_akshare(ticker: str, days: int = 100) -> list[dict]:
    """Fallback: akshare for A-share ETFs (.SH/.SS) and HK stocks (.HK)."""
    try:
        import akshare as ak
    except ImportError:
        return []

    t = ticker.upper()

    try:
        # A-share ETF: 518800.SH / 518800.SS → sh518800
        if t.endswith(".SH") or t.endswith(".SS"):
            code = t.split(".")[0]
            df = ak.fund_etf_hist_sina(symbol=f"sh{code}")
            if df is None or df.empty:
                return []
            df = df.tail(days)
            return [{"high": float(row["high"]), "low": float(row["low"]), "close": float(row["close"])} for _, row in df.iterrows()]

        # HK stock: 07709.HK / 7709.HK → 07709
        if t.endswith(".HK"):
            code = t.split(".")[0].zfill(5)
            df = ak.stock_hk_daily(symbol=code, adjust="hfq")
            if df is None or df.empty:
                return []
            df = df.tail(days)
            return [{"high": float(row["high"]), "low": float(row["low"]), "close": float(row["close"])} for _, row in df.iterrows()]

    except Exception as e:
        print(f"  [akshare] {ticker}: {e}")

    return []


def _normalize_ticker_fmp(ticker: str) -> str:
    """Convert internal ticker format to FMP symbol."""
    t = ticker.upper()
    # Remove .US suffix (FMP uses bare symbols for US stocks)
    if t.endswith(".US"):
        return t[:-3]
    # A-share ETFs: 518800.SH → 518800.SS (FMP uses .SS for Shanghai)
    if t.endswith(".SH"):
        return t[:-3] + ".SS"
    # Hong Kong: strip leading zeros (07709.HK → 7709.HK)
    if t.endswith(".HK"):
        code = t.split(".")[0].lstrip("0")
        return f"{code}.HK" if code else t
    # Crypto
    if t == "BTC":
        return "BTCUSD"
    if t == "ETH":
        return "ETHUSD"
    return t


def fetch_ohlc(ticker: str, days: int = 100) -> list[dict]:
    t = ticker.upper()
    # A-share / HK: akshare first (FMP requires paid plan)
    if t.endswith(".SH") or t.endswith(".SS") or t.endswith(".HK"):
        rows = _fetch_ohlc_akshare(ticker, days)
        if rows:
            return rows

    fmp_symbol = _normalize_ticker_fmp(ticker)
    url = f"{_FMP_BASE}/historical-price-eod/full"
    params = {"symbol": fmp_symbol, "apikey": _FMP_API_KEY, "limit": days + 20}
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        if not isinstance(data, list) or not data:
            # FMP failed, try akshare as final fallback
            return _fetch_ohlc_akshare(ticker, days)
        rows = []
        for bar in reversed(data):
            h, l, c = bar.get("high"), bar.get("low"), bar.get("close")
            if h is None or l is None or c is None:
                continue
            rows.append({"high": h, "low": l, "close": c})
        return rows[-days:]
    except Exception:
        return _fetch_ohlc_akshare(ticker, days)


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
    fundamental_adj: dict | None = None,
) -> dict | None:
    """Plan A+B 止盈止损算法
    stop_mult_adj: Polymarket regime 导致的 ATR 倍数调整（负值 = 收紧止损）
    fundamental_adj: 基本面调整 {stop_adj, target_adj, target_cap}（由 compute_fundamental_adjustment 计算）
    """
    atr = calc_atr(ohlc)
    if atr is None or atr == 0:
        return None

    effective_stop_mult = max(1.0, STOP_MULT + stop_mult_adj)

    # 基本面调整值
    f_stop_adj = fundamental_adj["stop_adj"] if fundamental_adj else 0.0
    f_target_adj = fundamental_adj["target_adj"] if fundamental_adj else 0.0
    f_target_cap = fundamental_adj["target_cap"] if fundamental_adj else None

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

        # 3.5 基本面止损调整（正值=放宽=下移，负值=收紧=上移）
        stop_loss += f_stop_adj

        # 4. 价格已跌破常规止损位（仓位仍开着）→ 用 current_price 作锚的应急止损
        if stop_loss >= current_price:
            stop_loss = current_price - 1.0 * atr

        # 5. Ratchet: 止损只能收紧（上移），不能放松（下移）
        if existing_stop and existing_stop > stop_loss:
            stop_loss = existing_stop

        # ── 止盈（两级） ──
        target_1 = entry_price + TARGET_1_MULT * atr + f_target_adj
        target_2 = entry_price + TARGET_2_MULT * atr + f_target_adj

        # 如果现价已超过 T1，把 T1 上移（继续追踪）
        if current_price >= target_1:
            target_1 = current_price + 1.0 * atr
        # T2 始终 >= T1
        if target_2 < target_1:
            target_2 = target_1 + TARGET_1_MULT * atr

        # 基本面 target cap: T2 不超过分析师共识
        if f_target_cap and target_2 > f_target_cap:
            target_2 = f_target_cap

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

        # 基本面止损调整 (short: 正值=收紧=下移)
        stop_loss += f_stop_adj

        if stop_loss <= current_price:
            stop_loss = current_price + 1.0 * atr

        if existing_stop and existing_stop < stop_loss:
            stop_loss = existing_stop

        target_1 = entry_price - TARGET_1_MULT * atr + f_target_adj
        target_2 = entry_price - TARGET_2_MULT * atr + f_target_adj

        if current_price <= target_1:
            target_1 = current_price - 1.0 * atr
        if target_2 > target_1:
            target_2 = target_1 - TARGET_1_MULT * atr

        if f_target_cap and target_2 < f_target_cap:
            target_2 = f_target_cap

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

        # 基本面调整
        fundamental = fetch_fundamental_data(ticker)
        atr = calc_atr(ohlc)
        f_adj = compute_fundamental_adjustment(fundamental, atr or 0, direction)
        if f_adj["factors"]:
            print(f"  [{ticker}] 基本面因子: {'; '.join(f_adj['factors'])}")

        result = compute_stops(
            current_price=current_price,
            entry_price=entry_price,
            direction=direction,
            ohlc=ohlc,
            existing_stop=existing_stop,
            stop_mult_adj=stop_adj,
            fundamental_adj=f_adj,
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


def compare_stops() -> list[dict]:
    """对比新旧算法：对每个持仓分别计算纯技术面 vs 技术面+基本面的止盈止损。
    返回对比列表，供 API 调用。
    """
    if not os.path.exists(DB_PATH):
        return []

    regime_info = _get_polymarket_regime()
    stop_adj = regime_info["stop_mult_adj"]

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_column(conn, "positions", "current_price")
    _ensure_column(conn, "positions", "profit_target_2")
    positions = conn.execute(
        "SELECT id, ticker, asset, entry_price, direction, current_price, stop_loss, profit_target_2 "
        "FROM positions WHERE status = 'open'"
    ).fetchall()
    conn.close()

    results = []
    for pos in positions:
        ticker = pos["ticker"] or pos["asset"]
        direction = pos["direction"] or "long"
        entry_price = float(pos["entry_price"]) if pos["entry_price"] else None
        if not entry_price:
            continue

        ohlc = fetch_ohlc(ticker)
        if not ohlc:
            continue

        price_from_api = float(ohlc[-1]["close"])
        current_price = price_from_api or (float(pos["current_price"]) if pos["current_price"] else entry_price)

        atr = calc_atr(ohlc)
        if not atr:
            continue

        # Plan A: 纯技术面
        tech_only = compute_stops(
            current_price=current_price,
            entry_price=entry_price,
            direction=direction,
            ohlc=ohlc,
            existing_stop=None,  # 不用 ratchet，纯计算对比
            stop_mult_adj=stop_adj,
            fundamental_adj=None,
        )

        # Plan A+B: 技术面 + 基本面
        fundamental = fetch_fundamental_data(ticker)
        f_adj = compute_fundamental_adjustment(fundamental, atr, direction)

        tech_fundamental = compute_stops(
            current_price=current_price,
            entry_price=entry_price,
            direction=direction,
            ohlc=ohlc,
            existing_stop=None,
            stop_mult_adj=stop_adj,
            fundamental_adj=f_adj,
        )

        results.append({
            "ticker": ticker,
            "asset": pos["asset"],
            "direction": direction,
            "entry_price": entry_price,
            "current_price": round(current_price, 2),
            "atr": round(atr, 2),
            "has_fundamental": fundamental is not None,
            "fundamental_data": fundamental,
            "fundamental_factors": f_adj["factors"],
            "tech_only": tech_only,
            "tech_fundamental": tech_fundamental,
            "pe_ratio": fundamental.get("pe_ratio") if fundamental else None,
            "pe_percentile": fundamental.get("pe_percentile") if fundamental else None,
            "eps_declining": fundamental.get("eps_declining") if fundamental else None,
            "days_to_earnings": fundamental.get("days_to_earnings") if fundamental else None,
            "analyst_target": fundamental.get("analyst_target") if fundamental else None,
        })

    return results


if __name__ == "__main__":
    main()
