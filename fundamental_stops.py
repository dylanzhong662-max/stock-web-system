"""基本面止盈止损模块 — 与 ATR 技术止损取较紧者合并

数据源: Financial Modeling Prep (FMP) API
  - /profile: P/E, P/S, market cap, sector
  - /income-statement: 营收增速, 毛利率
  - /earning_calendar: 财报日期
  - /historical-price-full: 历史价格分位

设计原则:
  - 止盈天花板 = 基于历史估值分位的合理目标价（不追泡沫）
  - 止损地板   = 基于基本面恶化信号的动态止损（营收 miss、毛利下滑）
  - 财报风险   = 财报前 N 天自动收紧止损或标记风险
  - 波动率适配 = 用资产自身波动率替代固定 8% max loss
"""
import os
import requests
import numpy as np
from datetime import datetime, timedelta
from typing import Optional

FMP_API_KEY = os.environ.get("FMP_API_KEY", "")
FMP_BASE_URL = "https://financialmodelingprep.com/stable"

EARNINGS_TIGHTEN_DAYS = 5
VALUATION_HISTORY_YEARS = 5


def _fmp_ticker(ticker: str) -> str:
    """规范化为 FMP 格式的 ticker"""
    t = ticker.strip().upper()
    for suffix in (".US", ".SH", ".SS", ".SZ"):
        if t.endswith(suffix):
            t = t[: -len(suffix)]
            break
    # .HK tickers: FMP uses format like "0700.HK"
    if t.endswith(".HK"):
        return t
    return t


def _fmp_get(endpoint: str, params: dict = None) -> Optional[dict | list]:
    """FMP Stable API 通用请求"""
    if not FMP_API_KEY:
        return None
    url = f"{FMP_BASE_URL}/{endpoint}"
    p = {"apikey": FMP_API_KEY}
    if params:
        p.update(params)
    try:
        r = requests.get(url, params=p, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data:
                return data
    except Exception:
        pass
    return None


def _fetch_company_profile(ticker: str) -> Optional[dict]:
    """FMP /stable/profile — P/E, market cap, sector"""
    sym = _fmp_ticker(ticker)
    data = _fmp_get("profile", {"symbol": sym})
    if data and isinstance(data, list) and len(data) > 0:
        return data[0]
    return None


def _fetch_income_statements(ticker: str, limit: int = 4) -> Optional[list]:
    """FMP /stable/income-statement — 最近几个季度的营收、毛利"""
    sym = _fmp_ticker(ticker)
    data = _fmp_get("income-statement", {"symbol": sym, "period": "quarter", "limit": limit})
    if data and isinstance(data, list):
        return data
    return None


def _fetch_earnings_calendar(ticker: str) -> Optional[list]:
    """FMP /stable/earnings-calendar — 未来财报日期"""
    sym = _fmp_ticker(ticker)
    today = datetime.now().strftime("%Y-%m-%d")
    future = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")
    data = _fmp_get("earnings-calendar", {"from": today, "to": future})
    if data and isinstance(data, list):
        return [e for e in data if e.get("symbol", "").upper() == sym.upper()]
    return None


def _fetch_historical_prices(ticker: str, years: int = VALUATION_HISTORY_YEARS) -> Optional[dict]:
    """FMP /stable/historical-price-eod/full — 历史日线价格分布"""
    sym = _fmp_ticker(ticker)
    data = _fmp_get("historical-price-eod/full", {"symbol": sym, "serietype": "line"})
    if not data or not isinstance(data, list):
        return None
    cutoff = datetime.now() - timedelta(days=years * 365)
    closes = []
    for item in data:
        try:
            dt = datetime.strptime(item["date"], "%Y-%m-%d")
            if dt >= cutoff:
                closes.append(item["close"])
        except (KeyError, ValueError):
            continue
    if len(closes) < 60:
        return None
    closes = list(reversed(closes))
    return {
        "prices": closes,
        "p50": float(np.percentile(closes, 50)),
        "p75": float(np.percentile(closes, 75)),
        "p90": float(np.percentile(closes, 90)),
        "p95": float(np.percentile(closes, 95)),
        "p10": float(np.percentile(closes, 10)),
        "p25": float(np.percentile(closes, 25)),
        "max": float(np.max(closes)),
        "min": float(np.min(closes)),
    }


def compute_fundamental_stops(
    ticker: str,
    current_price: float,
    entry_price: float,
    direction: str,
    daily_returns_std: Optional[float] = None,
    holding_period_days: int = 30,
) -> Optional[dict]:
    """计算基本面止盈止损

    Returns:
        {
            "fundamental_stop": float,
            "fundamental_ceiling": float,
            "vol_adjusted_max_loss": float,
            "earnings_risk": bool,
            "earnings_date": str|None,
            "valuation_percentile": float,
            "pe_ratio": float|None,
            "revenue_growth": float|None,
            "profit_margin": float|None,
            "reason": str,
        }
    """
    if not FMP_API_KEY:
        return None

    # BTC/商品类 — 无基本面数据，返回波动率适配止损
    if ticker.upper() in ("BTC", "BTC-USD", "GLD", "SLV", "COPX", "REMX", "USO"):
        return _compute_commodity_stops(ticker, current_price, entry_price, direction, daily_returns_std)

    hist = _fetch_historical_prices(ticker)
    income = _fetch_income_statements(ticker)

    if not hist and not income:
        return None

    result = {
        "fundamental_stop": None,
        "fundamental_ceiling": None,
        "vol_adjusted_max_loss": None,
        "earnings_risk": False,
        "earnings_date": None,
        "valuation_percentile": None,
        "pe_ratio": None,
        "revenue_growth": None,
        "profit_margin": None,
        "reason": "",
    }

    reasons = []

    # ── 1. 估值分位 → 止盈天花板 ──
    if hist:
        prices = hist["prices"]
        percentile = float(np.searchsorted(np.sort(prices), current_price) / len(prices) * 100)
        result["valuation_percentile"] = round(percentile, 1)

        if direction == "long":
            if current_price >= hist["p90"]:
                ceiling = hist["p95"]
                # 天花板必须高于入场价，否则信号为"估值过高不宜入场"
                if ceiling <= entry_price:
                    ceiling = entry_price * 1.05
                    reasons.append(f"⚠️ 估值极端(P{percentile:.0f})，入场价已超P95({hist['p95']:.1f})，仅允许+5%止盈")
                else:
                    reasons.append(f"当前价已超历史P90({hist['p90']:.1f})，天花板锁定P95({ceiling:.1f})")
                result["fundamental_ceiling"] = ceiling
            elif current_price >= hist["p75"]:
                result["fundamental_ceiling"] = hist["p90"]
                reasons.append(f"估值偏高(P{percentile:.0f})，天花板设为P90({hist['p90']:.1f})")
            else:
                result["fundamental_ceiling"] = hist["p90"]

            if entry_price > hist["p50"]:
                result["fundamental_stop"] = hist["p50"] * 0.97
                reasons.append(f"基本面止损锚定历史中位数P50({hist['p50']:.1f})下方3%")
        else:
            result["fundamental_ceiling"] = hist["p25"]
            result["fundamental_stop"] = hist["p90"]
            reasons.append(f"做空天花板P25({hist['p25']:.1f})，止损P90({hist['p90']:.1f})")

    # ── 2. 基本面数据 → 调整止损 ──
    # 获取 P/E (从 key-metrics-ttm 的 earningsYield 反算)
    pe = None
    metrics = _fmp_get("key-metrics-ttm", {"symbol": _fmp_ticker(ticker)})
    if metrics and isinstance(metrics, list) and len(metrics) > 0:
        ey = metrics[0].get("earningsYieldTTM")
        if ey and ey > 0:
            pe = round(1.0 / ey, 1)
    result["pe_ratio"] = pe

    if pe and pe > 50 and direction == "long":
        if result["fundamental_ceiling"]:
            result["fundamental_ceiling"] = min(
                result["fundamental_ceiling"],
                current_price * 1.10
            )
        else:
            result["fundamental_ceiling"] = current_price * 1.10
        reasons.append(f"P/E极高({pe:.1f}x)，止盈压缩至+10%")

    if income and len(income) >= 2:
        latest = income[0]
        prev = income[1]
        latest_rev = latest.get("revenue", 0)
        prev_rev = prev.get("revenue", 0)

        if prev_rev and prev_rev > 0:
            rev_growth = (latest_rev - prev_rev) / prev_rev
            result["revenue_growth"] = round(rev_growth, 4)

            if rev_growth < 0:
                reasons.append(f"营收负增长({rev_growth*100:.1f}%)，基本面恶化风险")
                if result["fundamental_stop"] and direction == "long":
                    result["fundamental_stop"] = max(
                        result["fundamental_stop"],
                        entry_price * 0.95
                    )
                elif direction == "long":
                    result["fundamental_stop"] = entry_price * 0.95

        latest_gp = latest.get("grossProfit", 0)
        if latest_rev and latest_rev > 0:
            profit_margin = latest_gp / latest_rev
            result["profit_margin"] = round(profit_margin, 4)

            # 毛利率对比前季度
            prev_gp = prev.get("grossProfit", 0)
            prev_rev_val = prev.get("revenue", 1)
            if prev_rev_val > 0:
                prev_margin = prev_gp / prev_rev_val
                margin_drop = prev_margin - profit_margin
                if margin_drop > 0.05:
                    reasons.append(f"毛利率单季下降{margin_drop*100:.1f}个百分点，风险升高")
                    if direction == "long" and result["fundamental_stop"]:
                        result["fundamental_stop"] = max(
                            result["fundamental_stop"],
                            entry_price * 0.93
                        )

    # ── 3. 财报日期 → 事件风险 ──
    earnings = _fetch_earnings_calendar(ticker)
    if earnings:
        for e in earnings:
            edate_str = e.get("date")
            if not edate_str:
                continue
            try:
                edate = datetime.strptime(edate_str, "%Y-%m-%d")
                days_to = (edate - datetime.now()).days
                if 0 <= days_to <= EARNINGS_TIGHTEN_DAYS:
                    result["earnings_risk"] = True
                    result["earnings_date"] = edate_str
                    reasons.append(f"财报临近({days_to}天后)，建议收紧止损或减仓")
                    break
                elif days_to > EARNINGS_TIGHTEN_DAYS:
                    result["earnings_date"] = edate_str
                    break
            except ValueError:
                continue

    # ── 4. 波动率适配最大亏损 ──
    if daily_returns_std:
        vol_max_loss = min(0.20, 2.0 * daily_returns_std * np.sqrt(holding_period_days))
        result["vol_adjusted_max_loss"] = round(vol_max_loss, 4)
        reasons.append(f"波动率适配止损: σ_daily={daily_returns_std*100:.2f}%, max_loss={vol_max_loss*100:.1f}%")
    elif hist and len(hist["prices"]) > 60:
        prices_arr = np.array(hist["prices"])
        daily_rets = np.diff(prices_arr) / prices_arr[:-1]
        daily_vol_est = float(np.std(daily_rets))
        vol_max_loss = min(0.20, 2.0 * daily_vol_est * np.sqrt(holding_period_days))
        result["vol_adjusted_max_loss"] = round(vol_max_loss, 4)
        reasons.append(f"历史波动率估算止损: max_loss={vol_max_loss*100:.1f}%")

    result["reason"] = "；".join(reasons) if reasons else "基本面数据不足，仅使用技术止损"
    return result


def _compute_commodity_stops(
    ticker: str,
    current_price: float,
    entry_price: float,
    direction: str,
    daily_returns_std: Optional[float] = None,
) -> Optional[dict]:
    """商品/加密货币：无 P/E 等基本面，使用波动率 + 历史区间"""
    hist = _fetch_historical_prices(ticker)
    result = {
        "fundamental_stop": None,
        "fundamental_ceiling": None,
        "vol_adjusted_max_loss": None,
        "earnings_risk": False,
        "earnings_date": None,
        "valuation_percentile": None,
        "pe_ratio": None,
        "revenue_growth": None,
        "profit_margin": None,
        "reason": "",
    }

    reasons = []

    if hist:
        prices = hist["prices"]
        percentile = float(np.searchsorted(np.sort(prices), current_price) / len(prices) * 100)
        result["valuation_percentile"] = round(percentile, 1)

        if direction == "long":
            result["fundamental_ceiling"] = hist["p95"]
            result["fundamental_stop"] = hist["p25"] * 0.95
            reasons.append(f"历史区间止盈P95({hist['p95']:.1f})，止损P25下方({hist['p25']*0.95:.1f})")
        else:
            result["fundamental_ceiling"] = hist["p10"]
            result["fundamental_stop"] = hist["p75"] * 1.05
            reasons.append(f"做空目标P10({hist['p10']:.1f})，止损P75上方({hist['p75']*1.05:.1f})")

        prices_arr = np.array(prices)
        daily_rets = np.diff(prices_arr) / prices_arr[:-1]
        daily_vol_est = float(np.std(daily_rets))
        vol_max_loss = min(0.25, 2.5 * daily_vol_est * np.sqrt(30))
        result["vol_adjusted_max_loss"] = round(vol_max_loss, 4)
        reasons.append(f"商品波动率适配: max_loss={vol_max_loss*100:.1f}%")

    if daily_returns_std:
        vol_max_loss = min(0.25, 2.5 * daily_returns_std * np.sqrt(30))
        result["vol_adjusted_max_loss"] = round(vol_max_loss, 4)

    result["reason"] = "；".join(reasons) if reasons else "商品类资产，仅依赖历史区间"
    return result


def merge_stops(
    atr_result: dict,
    fundamental_result: Optional[dict],
    direction: str,
    entry_price: float,
    current_price: float,
) -> dict:
    """合并技术面 + 基本面止盈止损，取较紧者

    Returns merged dict with keys:
        stop_loss, profit_target, profit_target_2,
        fundamental_stop, fundamental_ceiling, earnings_risk, merge_notes
    """
    merged = {**atr_result, "fundamental_stop": None, "fundamental_ceiling": None,
              "earnings_risk": False, "earnings_date": None, "merge_notes": []}

    if not fundamental_result:
        merged["merge_notes"].append("基本面数据不可用，仅使用 ATR 技术止损")
        return merged

    notes = merged["merge_notes"]
    f_stop = fundamental_result.get("fundamental_stop")
    f_ceiling = fundamental_result.get("fundamental_ceiling")
    vol_max_loss = fundamental_result.get("vol_adjusted_max_loss")

    merged["fundamental_stop"] = f_stop
    merged["fundamental_ceiling"] = f_ceiling
    merged["earnings_risk"] = fundamental_result.get("earnings_risk", False)
    merged["earnings_date"] = fundamental_result.get("earnings_date")

    if direction == "long":
        # 止损: 取较紧者（较高的价格 = 更紧的止损）
        if f_stop and f_stop > atr_result["stop_loss"]:
            merged["stop_loss"] = round(f_stop, 2)
            notes.append(f"基本面止损({f_stop:.2f}) > ATR止损({atr_result['stop_loss']:.2f})，使用基本面止损")

        # 波动率适配的 max loss 替代固定 8%
        if vol_max_loss:
            vol_stop = entry_price * (1 - vol_max_loss)
            if vol_stop > merged["stop_loss"]:
                merged["stop_loss"] = round(vol_stop, 2)
                notes.append(f"波动率适配止损({vol_stop:.2f}, max_loss={vol_max_loss*100:.1f}%) 更紧")

        # 止盈: 取较低者（更保守的天花板），但必须高于现价
        if f_ceiling and f_ceiling > current_price and f_ceiling < atr_result["profit_target"]:
            merged["profit_target"] = round(f_ceiling, 2)
            notes.append(f"基本面天花板({f_ceiling:.2f}) < ATR目标({atr_result['profit_target']:.2f})，使用基本面天花板")
        if f_ceiling and f_ceiling > current_price and f_ceiling < atr_result.get("profit_target_2", float("inf")):
            merged["profit_target_2"] = round(f_ceiling, 2)
            notes.append("T2 被基本面天花板压制")
        elif f_ceiling and f_ceiling <= current_price:
            notes.append(f"⚠️ 基本面天花板({f_ceiling:.2f})已低于现价，估值极端偏高")

        # 财报风险: 收紧止损 50%
        if fundamental_result.get("earnings_risk"):
            risk_adjustment = (current_price - merged["stop_loss"]) * 0.5
            earnings_stop = current_price - risk_adjustment
            if earnings_stop > merged["stop_loss"]:
                merged["stop_loss"] = round(earnings_stop, 2)
                notes.append(f"财报临近，止损收紧至{earnings_stop:.2f}(距现价缩半)")

    else:  # short
        if f_stop and f_stop < atr_result["stop_loss"]:
            merged["stop_loss"] = round(f_stop, 2)
            notes.append(f"基本面止损({f_stop:.2f}) < ATR止损({atr_result['stop_loss']:.2f})，使用基本面止损")

        if vol_max_loss:
            vol_stop = entry_price * (1 + vol_max_loss)
            if vol_stop < merged["stop_loss"]:
                merged["stop_loss"] = round(vol_stop, 2)
                notes.append(f"波动率适配止损({vol_stop:.2f}) 更紧")

        if f_ceiling and f_ceiling > atr_result["profit_target"]:
            merged["profit_target"] = round(f_ceiling, 2)
            notes.append(f"基本面地板({f_ceiling:.2f}) > ATR目标({atr_result['profit_target']:.2f})")

        if fundamental_result.get("earnings_risk"):
            risk_adjustment = (merged["stop_loss"] - current_price) * 0.5
            earnings_stop = current_price + risk_adjustment
            if earnings_stop < merged["stop_loss"]:
                merged["stop_loss"] = round(earnings_stop, 2)
                notes.append(f"财报临近，止损收紧至{earnings_stop:.2f}")

    return merged
