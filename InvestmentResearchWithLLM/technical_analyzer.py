"""技术分析模块 — 多维度指标计算 + DeepSeek R1 综合研判

指标设计原则（参考 Technical Analysis skill）：
- 每类只选一个指标，不堆叠同类（趋势:EMA / 动量:RSI+MACD / 波动:ATR+BB / 量:Volume Profile）
- 多时间框架对照（日线+周线）
- 量价验证
- 所有信号附带止损和失效条件
"""
import asyncio
import os
from typing import AsyncGenerator, Optional

import numpy as np
import pandas as pd

from data_providers.quant import _get_fmp_ohlc
from data_providers.ticker_utils import fmp_ticker
from llm_client import get_client, resolve_model, build_extra_params, has_reasoning
from scaling_advisor import _read_open_positions
import rag_client
import report_generator


CACHE_TYPE = "technical"


# =============================================================================
# 指标计算（纯量化，不依赖 LLM）
# =============================================================================

def _to_dataframe(rows: list[dict]) -> pd.DataFrame:
    """OHLCV dicts → DataFrame，升序排列"""
    records = []
    for r in rows:
        try:
            rec = {
                "date": pd.Timestamp(r["date"]),
                "open": float(r.get("open", r.get("close", 0))),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r.get("volume", 0)),
            }
            records.append(rec)
        except (KeyError, ValueError, TypeError):
            continue
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records).sort_values("date").reset_index(drop=True)
    df.set_index("date", inplace=True)
    return df


def _resample_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """日线聚合为周线"""
    if df.empty:
        return df
    weekly = df.resample("W").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()
    return weekly


# --- 趋势指标 ---

def calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def calc_sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def trend_analysis(df: pd.DataFrame) -> dict:
    """EMA 20/50/200 排列 + 趋势方向判定"""
    if len(df) < 200:
        periods = [p for p in [20, 50, 200] if len(df) >= p]
    else:
        periods = [20, 50, 200]

    close = df["close"]
    emas = {}
    for p in periods:
        emas[f"ema{p}"] = round(float(calc_ema(close, p).iloc[-1]), 4)

    current_price = float(close.iloc[-1])

    # MA 排列判定
    if len(periods) >= 3:
        e20, e50, e200 = emas["ema20"], emas["ema50"], emas["ema200"]
        if e20 > e50 > e200:
            arrangement = "多头排列（EMA20 > EMA50 > EMA200）"
            trend = "bullish"
        elif e20 < e50 < e200:
            arrangement = "空头排列（EMA20 < EMA50 < EMA200）"
            trend = "bearish"
        else:
            arrangement = "交织/震荡"
            trend = "neutral"

        # 趋势强度：价格偏离 EMA50 的程度
        deviation = (current_price - e50) / e50 * 100
        if abs(deviation) > 10:
            strength = "strong"
        elif abs(deviation) > 3:
            strength = "moderate"
        else:
            strength = "weak"
    elif len(periods) >= 2:
        e20 = emas.get("ema20", emas.get("ema50"))
        e50 = emas.get("ema50", e20)
        if current_price > e20 > e50:
            arrangement = "短期多头"
            trend = "bullish"
        elif current_price < e20 < e50:
            arrangement = "短期空头"
            trend = "bearish"
        else:
            arrangement = "震荡"
            trend = "neutral"
        strength = "moderate"
        deviation = 0
    else:
        arrangement = "数据不足"
        trend = "neutral"
        strength = "weak"
        deviation = 0

    return {
        "emas": emas,
        "arrangement": arrangement,
        "trend": trend,
        "strength": strength,
        "price_vs_ema50_pct": round(deviation, 2) if deviation else None,
        "current_price": current_price,
    }


# --- 动量指标 ---

def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_macd(close: pd.Series, fast=12, slow=26, signal=9) -> dict:
    ema_fast = calc_ema(close, fast)
    ema_slow = calc_ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = calc_ema(macd_line, signal)
    histogram = macd_line - signal_line
    return {
        "macd": macd_line,
        "signal": signal_line,
        "histogram": histogram,
    }


def momentum_analysis(df: pd.DataFrame) -> dict:
    """RSI(14) + MACD(12,26,9)"""
    close = df["close"]

    # RSI
    rsi = calc_rsi(close, 14)
    rsi_value = float(rsi.iloc[-1]) if not rsi.empty else None

    if rsi_value is not None:
        if rsi_value > 70:
            rsi_state = "超买"
        elif rsi_value < 30:
            rsi_state = "超卖"
        elif rsi_value > 60:
            rsi_state = "偏强"
        elif rsi_value < 40:
            rsi_state = "偏弱"
        else:
            rsi_state = "中性"
    else:
        rsi_state = "无数据"

    # MACD
    macd_data = calc_macd(close)
    macd_val = float(macd_data["macd"].iloc[-1])
    signal_val = float(macd_data["signal"].iloc[-1])
    hist_val = float(macd_data["histogram"].iloc[-1])
    hist_prev = float(macd_data["histogram"].iloc[-2]) if len(macd_data["histogram"]) > 1 else 0

    if macd_val > signal_val and hist_prev <= 0 < hist_val:
        macd_state = "金叉（刚发生）"
    elif macd_val < signal_val and hist_prev >= 0 > hist_val:
        macd_state = "死叉（刚发生）"
    elif macd_val > signal_val:
        macd_state = "多头（MACD > Signal）"
    elif macd_val < signal_val:
        macd_state = "空头（MACD < Signal）"
    else:
        macd_state = "交叉中"

    # 柱状图方向
    if hist_val > hist_prev:
        hist_direction = "放大（动量增强）"
    else:
        hist_direction = "收缩（动量减弱）"

    return {
        "rsi": round(rsi_value, 2) if rsi_value else None,
        "rsi_state": rsi_state,
        "macd": round(macd_val, 4),
        "macd_signal": round(signal_val, 4),
        "macd_histogram": round(hist_val, 4),
        "macd_state": macd_state,
        "histogram_direction": hist_direction,
    }


# --- 波动率指标 ---

def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def calc_bollinger(close: pd.Series, period: int = 20, num_std: float = 2.0) -> dict:
    sma = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = sma + num_std * std
    lower = sma - num_std * std
    return {"upper": upper, "middle": sma, "lower": lower}


def volatility_analysis(df: pd.DataFrame) -> dict:
    """ATR(14) + Bollinger Bands(20,2)"""
    atr = calc_atr(df, 14)
    atr_value = float(atr.iloc[-1]) if not atr.empty else None

    close = df["close"]
    current_price = float(close.iloc[-1])

    bb = calc_bollinger(close, 20, 2.0)
    bb_upper = float(bb["upper"].iloc[-1]) if not bb["upper"].isna().iloc[-1] else None
    bb_middle = float(bb["middle"].iloc[-1]) if not bb["middle"].isna().iloc[-1] else None
    bb_lower = float(bb["lower"].iloc[-1]) if not bb["lower"].isna().iloc[-1] else None

    # %B 位置（0=下轨, 1=上轨）
    if bb_upper and bb_lower and (bb_upper - bb_lower) > 0:
        pct_b = (current_price - bb_lower) / (bb_upper - bb_lower)
    else:
        pct_b = None

    # Bollinger 带宽（波动率高低）
    if bb_middle and bb_middle > 0:
        bandwidth = (bb_upper - bb_lower) / bb_middle * 100 if bb_upper and bb_lower else None
    else:
        bandwidth = None

    return {
        "atr": round(atr_value, 4) if atr_value else None,
        "atr_pct": round(atr_value / current_price * 100, 2) if atr_value and current_price else None,
        "bollinger_upper": round(bb_upper, 4) if bb_upper else None,
        "bollinger_middle": round(bb_middle, 4) if bb_middle else None,
        "bollinger_lower": round(bb_lower, 4) if bb_lower else None,
        "pct_b": round(pct_b, 3) if pct_b is not None else None,
        "bandwidth": round(bandwidth, 2) if bandwidth else None,
    }


# --- 成交量分析 ---

def volume_analysis(df: pd.DataFrame) -> dict:
    """Volume Profile（POC/VAH/VAL）+ 20日均量比"""
    if df.empty or "volume" not in df.columns:
        return {}

    vol = df["volume"]
    current_vol = float(vol.iloc[-1])
    avg_vol_20 = float(vol.rolling(20).mean().iloc[-1]) if len(vol) >= 20 else float(vol.mean())
    vol_ratio = current_vol / avg_vol_20 if avg_vol_20 > 0 else 1.0

    if vol_ratio > 2.0:
        vol_state = "极度放量"
    elif vol_ratio > 1.5:
        vol_state = "放量"
    elif vol_ratio > 0.7:
        vol_state = "正常"
    else:
        vol_state = "缩量"

    # 简化 Volume Profile（最近 50 根 K 线）
    recent = df.tail(50)
    num_bins = 30
    price_range = np.linspace(float(recent["low"].min()), float(recent["high"].max()), num_bins + 1)
    volume_profile = np.zeros(num_bins)

    for _, row in recent.iterrows():
        r_low, r_high, r_vol = float(row["low"]), float(row["high"]), float(row["volume"])
        mask = (price_range[:-1] >= r_low) & (price_range[1:] <= r_high)
        if mask.any():
            volume_profile[mask] += r_vol / mask.sum()

    if volume_profile.sum() > 0:
        poc_idx = int(volume_profile.argmax())
        poc_price = (price_range[poc_idx] + price_range[poc_idx + 1]) / 2

        # Value Area (70%)
        sorted_idx = np.argsort(volume_profile)[::-1]
        cumsum = np.cumsum(volume_profile[sorted_idx])
        va_threshold = volume_profile.sum() * 0.70
        va_idx = sorted_idx[cumsum <= va_threshold]

        if len(va_idx) > 0:
            vah = float(price_range[int(va_idx.max()) + 1])
            val = float(price_range[int(va_idx.min())])
        else:
            vah = float(price_range[-1])
            val = float(price_range[0])
    else:
        poc_price = float(recent["close"].iloc[-1])
        vah = float(recent["high"].max())
        val = float(recent["low"].min())

    return {
        "current_volume": int(current_vol),
        "avg_volume_20": int(avg_vol_20),
        "volume_ratio": round(vol_ratio, 2),
        "volume_state": vol_state,
        "poc": round(poc_price, 4),
        "vah": round(vah, 4),
        "val": round(val, 4),
    }


# --- 市场结构分析 ---

def _find_swing_points(series: pd.Series, order: int = 5) -> list[dict]:
    """检测 swing highs/lows"""
    points = []
    values = series.values
    for i in range(order, len(values) - order):
        # Swing high
        if all(values[i] >= values[i - j] for j in range(1, order + 1)) and \
           all(values[i] >= values[i + j] for j in range(1, order + 1)):
            points.append({"index": i, "price": float(values[i]), "type": "high"})
        # Swing low
        if all(values[i] <= values[i - j] for j in range(1, order + 1)) and \
           all(values[i] <= values[i + j] for j in range(1, order + 1)):
            points.append({"index": i, "price": float(values[i]), "type": "low"})
    return points


def structure_analysis(df: pd.DataFrame) -> dict:
    """Market Structure Break + Swing High/Low 检测"""
    if len(df) < 30:
        return {"structure": "数据不足", "swings": []}

    swing_highs = _find_swing_points(df["high"], order=5)
    swing_lows = _find_swing_points(df["low"], order=5)

    # 判断结构趋势
    recent_highs = [s for s in swing_highs if s["index"] > len(df) - 60][-3:]
    recent_lows = [s for s in swing_lows if s["index"] > len(df) - 60][-3:]

    structure = "neutral"
    structure_detail = ""

    if len(recent_highs) >= 2 and len(recent_lows) >= 2:
        hh = recent_highs[-1]["price"] > recent_highs[-2]["price"]
        hl = recent_lows[-1]["price"] > recent_lows[-2]["price"]
        lh = recent_highs[-1]["price"] < recent_highs[-2]["price"]
        ll = recent_lows[-1]["price"] < recent_lows[-2]["price"]

        if hh and hl:
            structure = "bullish"
            structure_detail = "Higher Highs + Higher Lows（上升结构）"
        elif lh and ll:
            structure = "bearish"
            structure_detail = "Lower Highs + Lower Lows（下降结构）"
        elif hh and not hl:
            structure_detail = "Higher High but Equal/Lower Low（潜在顶部）"
        elif ll and not lh:
            structure_detail = "Lower Low but Equal/Higher High（潜在底部）"
        else:
            structure_detail = "无明确结构（震荡区间）"

    # 检测 Structure Break
    current_price = float(df["close"].iloc[-1])
    msb_signal = None

    if recent_lows and structure == "bullish":
        last_hl = recent_lows[-1]["price"]
        if current_price < last_hl:
            msb_signal = {
                "type": "bearish_break",
                "broken_level": last_hl,
                "description": f"价格跌破最近 Higher Low ({last_hl:.2f})，上升结构被破坏",
            }

    if recent_highs and structure == "bearish":
        last_lh = recent_highs[-1]["price"]
        if current_price > last_lh:
            msb_signal = {
                "type": "bullish_break",
                "broken_level": last_lh,
                "description": f"价格突破最近 Lower High ({last_lh:.2f})，下降结构被破坏",
            }

    return {
        "structure": structure,
        "structure_detail": structure_detail,
        "recent_swing_highs": [{"price": s["price"], "bars_ago": len(df) - 1 - s["index"]} for s in recent_highs[-3:]],
        "recent_swing_lows": [{"price": s["price"], "bars_ago": len(df) - 1 - s["index"]} for s in recent_lows[-3:]],
        "msb_signal": msb_signal,
    }


# --- Fibonacci 回撤 ---

def fibonacci_levels(df: pd.DataFrame) -> dict:
    """计算最近一段趋势的 Fibonacci 回撤位"""
    if len(df) < 30:
        return {}

    swing_highs = _find_swing_points(df["high"], order=5)
    swing_lows = _find_swing_points(df["low"], order=5)

    if not swing_highs or not swing_lows:
        return {}

    # 取最近的 swing high 和 swing low
    last_high = max(swing_highs[-3:], key=lambda x: x["price"])
    last_low = min(swing_lows[-3:], key=lambda x: x["price"])

    high_price = last_high["price"]
    low_price = last_low["price"]
    price_range = high_price - low_price

    if price_range <= 0:
        return {}

    # 判断趋势方向（高点在后=上升趋势回撤，低点在后=下降趋势回撤）
    if last_high["index"] > last_low["index"]:
        direction = "pullback_in_uptrend"
        levels = {
            "0.236": round(high_price - price_range * 0.236, 4),
            "0.382": round(high_price - price_range * 0.382, 4),
            "0.500": round(high_price - price_range * 0.500, 4),
            "0.618": round(high_price - price_range * 0.618, 4),
            "0.786": round(high_price - price_range * 0.786, 4),
        }
    else:
        direction = "pullback_in_downtrend"
        levels = {
            "0.236": round(low_price + price_range * 0.236, 4),
            "0.382": round(low_price + price_range * 0.382, 4),
            "0.500": round(low_price + price_range * 0.500, 4),
            "0.618": round(low_price + price_range * 0.618, 4),
            "0.786": round(low_price + price_range * 0.786, 4),
        }

    return {
        "direction": direction,
        "swing_high": high_price,
        "swing_low": low_price,
        "levels": levels,
    }


# --- 信号检测 ---

def detect_signals(df: pd.DataFrame) -> list[dict]:
    """检测可靠的技术信号（仅有统计依据的形态）"""
    signals = []
    if len(df) < 30:
        return signals

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]
    current_price = float(close.iloc[-1])

    # 1. RSI 背离 + 结构确认
    rsi = calc_rsi(close, 14)
    if len(rsi) >= 30:
        swing_lows_price = _find_swing_points(low, order=3)
        swing_lows_rsi = _find_swing_points(-rsi, order=3)  # 负号找低点

        if len(swing_lows_price) >= 2 and len(swing_lows_rsi) >= 2:
            p1, p2 = swing_lows_price[-2], swing_lows_price[-1]
            r1_idx, r2_idx = swing_lows_rsi[-2]["index"], swing_lows_rsi[-1]["index"]

            if (p2["price"] < p1["price"] and
                r2_idx < len(rsi) and r1_idx < len(rsi) and
                float(rsi.iloc[r2_idx]) > float(rsi.iloc[r1_idx])):
                signals.append({
                    "name": "RSI 看涨背离",
                    "direction": "bullish",
                    "win_rate": "55-60%",
                    "confirmation": "待价格突破前摆动高点确认",
                    "description": "价格创新低但 RSI 走高，动量减弱",
                })

    # 2. 放量突破
    if len(df) >= 20:
        recent_high = float(high.tail(20).max())
        avg_vol = float(volume.rolling(20).mean().iloc[-1])
        current_vol = float(volume.iloc[-1])

        if current_price >= recent_high and current_vol > avg_vol * 1.5:
            signals.append({
                "name": "放量突破 20日新高",
                "direction": "bullish",
                "win_rate": "60-65%（需量>1.5x均量）",
                "confirmation": "已确认（量价配合）",
                "description": f"价格突破 20日高点，成交量为均量 {current_vol/avg_vol:.1f} 倍",
            })
        elif current_price >= recent_high and current_vol < avg_vol * 0.8:
            signals.append({
                "name": "缩量突破（警告）",
                "direction": "neutral",
                "win_rate": "40-45%（缺乏量能确认）",
                "confirmation": "未确认",
                "description": "价格创新高但量能不足，假突破概率较高",
            })

    # 3. 放量跌破支撑
    if len(df) >= 20:
        recent_low = float(low.tail(20).min())
        if current_price <= recent_low and current_vol > avg_vol * 1.5:
            signals.append({
                "name": "放量跌破 20日低点",
                "direction": "bearish",
                "win_rate": "60-65%",
                "confirmation": "已确认（量价配合）",
                "description": f"价格跌破 20日低点，成交量为均量 {current_vol/avg_vol:.1f} 倍",
            })

    # 4. Bollinger Band Squeeze（带宽收窄→即将爆发）
    bb = calc_bollinger(close, 20, 2.0)
    if not bb["upper"].isna().iloc[-1]:
        bb_width = (float(bb["upper"].iloc[-1]) - float(bb["lower"].iloc[-1])) / float(bb["middle"].iloc[-1])
        bb_width_20 = ((bb["upper"] - bb["lower"]) / bb["middle"]).rolling(20).mean()
        if not bb_width_20.isna().iloc[-1]:
            avg_width = float(bb_width_20.iloc[-1])
            if bb_width < avg_width * 0.6:
                signals.append({
                    "name": "Bollinger Squeeze（波动率收缩）",
                    "direction": "neutral",
                    "win_rate": "方向未定，但波动率即将扩大",
                    "confirmation": "等待方向突破",
                    "description": f"当前带宽仅为平均的 {bb_width/avg_width*100:.0f}%，大幅波动即将到来",
                })

    # 5. 均线金叉/死叉（仅在趋势确认时才有意义）
    if len(df) >= 50:
        ema20 = calc_ema(close, 20)
        ema50 = calc_ema(close, 50)
        if len(ema20) >= 2 and len(ema50) >= 2:
            cross_up = float(ema20.iloc[-2]) < float(ema50.iloc[-2]) and float(ema20.iloc[-1]) > float(ema50.iloc[-1])
            cross_down = float(ema20.iloc[-2]) > float(ema50.iloc[-2]) and float(ema20.iloc[-1]) < float(ema50.iloc[-1])
            if cross_up:
                signals.append({
                    "name": "EMA20/50 金叉",
                    "direction": "bullish",
                    "win_rate": "55%（滞后指标，需趋势过滤）",
                    "confirmation": "需 ADX>25 + 量能确认",
                    "description": "EMA20 上穿 EMA50，中期动量转多",
                })
            elif cross_down:
                signals.append({
                    "name": "EMA20/50 死叉",
                    "direction": "bearish",
                    "win_rate": "55%（滞后指标）",
                    "confirmation": "需确认非震荡市假信号",
                    "description": "EMA20 下穿 EMA50，中期动量转空",
                })

    return signals


# =============================================================================
# 综合分析入口
# =============================================================================

def compute_all_indicators(df_daily: pd.DataFrame) -> dict:
    """计算日线所有指标"""
    return {
        "trend": trend_analysis(df_daily),
        "momentum": momentum_analysis(df_daily),
        "volatility": volatility_analysis(df_daily),
        "volume": volume_analysis(df_daily),
        "structure": structure_analysis(df_daily),
        "fibonacci": fibonacci_levels(df_daily),
        "signals": detect_signals(df_daily),
    }


def format_indicators(indicators: dict) -> str:
    """格式化指标为 prompt 可读文本"""
    lines = []

    # 趋势
    t = indicators["trend"]
    lines.append(f"**趋势：** {t['arrangement']}")
    for k, v in t["emas"].items():
        lines.append(f"  - {k.upper()}: {v}")
    lines.append(f"  - 当前价格: {t['current_price']}")
    lines.append(f"  - 趋势方向: {t['trend']} / 强度: {t['strength']}")
    if t.get("price_vs_ema50_pct") is not None:
        lines.append(f"  - 价格偏离EMA50: {t['price_vs_ema50_pct']:+.2f}%")

    # 动量
    m = indicators["momentum"]
    lines.append(f"\n**动量：**")
    lines.append(f"  - RSI(14): {m['rsi']} — {m['rsi_state']}")
    lines.append(f"  - MACD: {m['macd']} / Signal: {m['macd_signal']} / Hist: {m['macd_histogram']}")
    lines.append(f"  - MACD 状态: {m['macd_state']}")
    lines.append(f"  - 柱状图: {m['histogram_direction']}")

    # 波动
    v = indicators["volatility"]
    lines.append(f"\n**波动率：**")
    lines.append(f"  - ATR(14): {v['atr']} ({v['atr_pct']}% of price)")
    lines.append(f"  - Bollinger Bands: Upper={v['bollinger_upper']} / Middle={v['bollinger_middle']} / Lower={v['bollinger_lower']}")
    lines.append(f"  - %B: {v['pct_b']} (0=下轨, 0.5=中轨, 1=上轨)")
    lines.append(f"  - 带宽: {v['bandwidth']}%")

    # 成交量
    vol = indicators["volume"]
    if vol:
        lines.append(f"\n**成交量：**")
        lines.append(f"  - 当日量: {vol.get('current_volume', 'N/A'):,}")
        lines.append(f"  - 20日均量: {vol.get('avg_volume_20', 'N/A'):,}")
        lines.append(f"  - 量比: {vol.get('volume_ratio', 'N/A')}x — {vol.get('volume_state', '')}")
        lines.append(f"  - Volume Profile POC: {vol.get('poc')}")
        lines.append(f"  - Value Area: {vol.get('val')} - {vol.get('vah')}")

    # 结构
    s = indicators["structure"]
    lines.append(f"\n**市场结构：**")
    lines.append(f"  - 结构方向: {s.get('structure', 'neutral')} — {s.get('structure_detail', '')}")
    if s.get("recent_swing_highs"):
        highs_str = ", ".join([f"{h['price']:.2f}({h['bars_ago']}根前)" for h in s["recent_swing_highs"]])
        lines.append(f"  - 近期摆动高点: {highs_str}")
    if s.get("recent_swing_lows"):
        lows_str = ", ".join([f"{l['price']:.2f}({l['bars_ago']}根前)" for l in s["recent_swing_lows"]])
        lines.append(f"  - 近期摆动低点: {lows_str}")
    if s.get("msb_signal"):
        lines.append(f"  - ⚠️ 结构突破: {s['msb_signal']['description']}")

    # Fibonacci
    fib = indicators["fibonacci"]
    if fib:
        lines.append(f"\n**Fibonacci 回撤：**")
        lines.append(f"  - 方向: {fib.get('direction', '')}")
        lines.append(f"  - Swing High: {fib.get('swing_high')}, Swing Low: {fib.get('swing_low')}")
        if fib.get("levels"):
            for level, price in fib["levels"].items():
                lines.append(f"  - {level} 回撤: {price}")

    return "\n".join(lines)


def format_signals(signals: list[dict]) -> str:
    if not signals:
        return "当前无明显技术信号。"
    lines = []
    for s in signals:
        emoji = "🟢" if s["direction"] == "bullish" else "🔴" if s["direction"] == "bearish" else "🟡"
        lines.append(f"{emoji} **{s['name']}** ({s['direction']})")
        lines.append(f"   胜率: {s['win_rate']} | 确认: {s['confirmation']}")
        lines.append(f"   {s['description']}")
    return "\n".join(lines)


# =============================================================================
# LLM 综合研判
# =============================================================================

class TechnicalAnalyzer:
    def __init__(self):
        self._prompt_template = self._load_prompt()

    def _load_prompt(self) -> str:
        path = os.path.join(os.path.dirname(__file__), "prompts", "technical_analysis.md")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    async def _fetch_data(self, ticker: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        """获取日线和周线数据"""
        rows = await _get_fmp_ohlc(ticker, days=250)
        if not rows:
            return pd.DataFrame(), pd.DataFrame()
        df_daily = _to_dataframe(rows)
        df_weekly = _resample_weekly(df_daily)
        return df_daily, df_weekly

    async def analyze(self, ticker: str, model: str = None) -> tuple[str, dict]:
        """完整分析：返回 (report_text, indicators_dict)"""
        model = resolve_model(model)
        cache_key = ticker.upper()

        cached = report_generator.get_cached(CACHE_TYPE, cache_key, model)
        if cached:
            return cached, {"cached": True}

        df_daily, df_weekly = await self._fetch_data(ticker)
        if df_daily.empty or len(df_daily) < 20:
            return f"数据不足：{ticker} 只有 {len(df_daily)} 天数据，无法进行技术分析。", {}

        daily_ind = compute_all_indicators(df_daily)
        weekly_ind = compute_all_indicators(df_weekly) if len(df_weekly) >= 10 else {}

        daily_text = format_indicators(daily_ind)
        weekly_text = format_indicators(weekly_ind) if weekly_ind else "周线数据不足，无法计算。"
        structure_text = format_indicators({"structure": daily_ind["structure"], "fibonacci": daily_ind["fibonacci"],
                                            "trend": daily_ind["trend"], "momentum": {}, "volatility": {}, "volume": {}})
        signals_text = format_signals(daily_ind["signals"])

        rag_results = await rag_client.search_news(query=f"{ticker} stock price technical", top_k=3, hours=72)
        rag_context = rag_client.fmt_news_context(rag_results) or "无近期新闻。"

        atr_val = daily_ind["volatility"].get("atr", "N/A")

        prompt = self._prompt_template.format(
            ticker=ticker.upper(),
            daily_indicators=daily_text,
            weekly_indicators=weekly_text,
            structure_analysis=structure_text,
            signals_detected=signals_text,
            rag_news_context=rag_context,
            atr_value=atr_val,
        )

        from llm_client import get_client, build_extra_params, has_reasoning
        resp = await get_client(model).chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            **build_extra_params(model),
        )

        content = resp.choices[0].message.content or ""
        content = report_generator.format_report(content, source_note="FMP/AKShare OHLCV")

        report_generator.save_cache(CACHE_TYPE, cache_key, content, model)

        return content, {
            "cached": False,
            "daily_indicators": daily_ind,
            "weekly_summary": {
                "trend": weekly_ind.get("trend", {}).get("trend") if weekly_ind else None,
                "rsi": weekly_ind.get("momentum", {}).get("rsi") if weekly_ind else None,
            },
        }

    async def stream(self, ticker: str, model: str) -> AsyncGenerator[str, None]:
        """流式输出技术分析"""
        cache_key = ticker.upper()
        cached = report_generator.get_cached(CACHE_TYPE, cache_key, model)
        if cached:
            yield cached
            return

        df_daily, df_weekly = await self._fetch_data(ticker)
        if df_daily.empty or len(df_daily) < 20:
            yield f"数据不足：{ticker} 只有 {len(df_daily)} 天数据，无法进行技术分析。"
            return

        daily_ind = compute_all_indicators(df_daily)
        weekly_ind = compute_all_indicators(df_weekly) if len(df_weekly) >= 10 else {}

        daily_text = format_indicators(daily_ind)
        weekly_text = format_indicators(weekly_ind) if weekly_ind else "周线数据不足，无法计算。"
        structure_text = f"结构: {daily_ind['structure'].get('structure_detail', '')}\n"
        if daily_ind["structure"].get("msb_signal"):
            structure_text += f"⚠️ {daily_ind['structure']['msb_signal']['description']}\n"
        fib = daily_ind["fibonacci"]
        if fib and fib.get("levels"):
            structure_text += f"Fibonacci: {fib['direction']}\n"
            for level, price in fib["levels"].items():
                structure_text += f"  {level}: {price}\n"

        signals_text = format_signals(daily_ind["signals"])

        rag_results = await rag_client.search_news(query=f"{ticker} stock price technical", top_k=3, hours=72)
        rag_context = rag_client.fmt_news_context(rag_results) or "无近期新闻。"

        atr_val = daily_ind["volatility"].get("atr", "N/A")

        prompt = self._prompt_template.format(
            ticker=ticker.upper(),
            daily_indicators=daily_text,
            weekly_indicators=weekly_text,
            structure_analysis=structure_text,
            signals_detected=signals_text,
            rag_news_context=rag_context,
            atr_value=atr_val,
        )

        stream_resp = await get_client(model).chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            stream=True,
            **build_extra_params(model),
        )

        chunks = []
        async for chunk in stream_resp:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if has_reasoning(model) and hasattr(delta, "reasoning_content") and delta.reasoning_content:
                continue
            if delta.content:
                chunks.append(delta.content)
                yield delta.content

        if chunks:
            full = "".join(chunks)
            report_generator.save_cache(CACHE_TYPE, cache_key, full, model)

    async def quick_indicators(self, ticker: str) -> dict:
        """仅返回计算指标（不调 LLM），用于轻量 API 或前端展示"""
        df_daily, df_weekly = await self._fetch_data(ticker)
        if df_daily.empty:
            return {"error": f"无法获取 {ticker} 的价格数据"}

        daily_ind = compute_all_indicators(df_daily)
        weekly_ind = compute_all_indicators(df_weekly) if len(df_weekly) >= 10 else None

        return {
            "ticker": ticker.upper(),
            "daily": daily_ind,
            "weekly": weekly_ind,
            "data_points": len(df_daily),
        }

    # ==========================================================================
    # 持仓技术面综合分析
    # ==========================================================================

    def _load_portfolio_prompt(self) -> str:
        path = os.path.join(os.path.dirname(__file__), "prompts", "portfolio_technical.md")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    async def analyze_portfolio(self, model: str = None) -> tuple[str, list[dict]]:
        """对所有持仓逐一计算技术指标，然后一次性交给 LLM 综合研判"""
        model = resolve_model(model)

        cached = report_generator.get_cached(CACHE_TYPE, "portfolio", model)
        if cached:
            return cached, []

        positions = _read_open_positions()
        if not positions:
            return "暂无开仓持仓，无法生成技术面报告。", []

        # 并发拉取所有持仓的 OHLC 数据并计算指标
        async def _calc_one(pos: dict) -> dict:
            ticker = pos["ticker"]
            df_daily, df_weekly = await self._fetch_data(ticker)
            if df_daily.empty or len(df_daily) < 20:
                return {"ticker": ticker, "asset": pos.get("asset", ticker), "error": "数据不足"}
            daily_ind = compute_all_indicators(df_daily)
            weekly_ind = compute_all_indicators(df_weekly) if len(df_weekly) >= 10 else None
            return {
                "ticker": ticker,
                "asset": pos.get("asset", ticker),
                "entry_price": pos.get("entry_price"),
                "daily": daily_ind,
                "weekly": weekly_ind,
            }

        results = await asyncio.gather(*[_calc_one(p) for p in positions], return_exceptions=True)
        results = [r for r in results if isinstance(r, dict)]

        # 拼接每个持仓的技术面文本
        sections = []
        for r in results:
            if r.get("error"):
                sections.append(f"### {r['ticker']}（{r.get('asset', '')}）\n\n{r['error']}\n")
                continue
            header = f"### {r['ticker']}（{r.get('asset', '')}）| 入场价: {r.get('entry_price', 'N/A')}"
            daily_text = format_indicators(r["daily"])
            signals_text = format_signals(r["daily"]["signals"])
            weekly_summary = ""
            if r.get("weekly"):
                w = r["weekly"]
                weekly_summary = (
                    f"\n**周线概要：** 趋势={w['trend']['trend']}({w['trend']['strength']}) "
                    f"RSI={w['momentum']['rsi']} {w['momentum']['rsi_state']}"
                )
            sections.append(f"{header}\n\n{daily_text}{weekly_summary}\n\n**信号：**\n{signals_text}\n")

        positions_technical_data = "\n---\n\n".join(sections)

        prompt_template = self._load_portfolio_prompt()
        prompt = prompt_template.format(positions_technical_data=positions_technical_data)

        resp = await get_client(model).chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            **build_extra_params(model),
        )

        content = resp.choices[0].message.content or ""
        content = report_generator.format_report(content, source_note="FMP/AKShare OHLCV")
        report_generator.save_cache(CACHE_TYPE, "portfolio", content, model)

        return content, results

    async def stream_portfolio(self, model: str = None) -> AsyncGenerator[str, None]:
        """流式输出持仓技术面综合分析"""
        model = resolve_model(model)

        cached = report_generator.get_cached(CACHE_TYPE, "portfolio", model)
        if cached:
            yield cached
            return

        positions = _read_open_positions()
        if not positions:
            yield "暂无开仓持仓，无法生成技术面报告。"
            return

        async def _calc_one(pos: dict) -> dict:
            ticker = pos["ticker"]
            df_daily, df_weekly = await self._fetch_data(ticker)
            if df_daily.empty or len(df_daily) < 20:
                return {"ticker": ticker, "asset": pos.get("asset", ticker), "error": "数据不足"}
            daily_ind = compute_all_indicators(df_daily)
            weekly_ind = compute_all_indicators(df_weekly) if len(df_weekly) >= 10 else None
            return {
                "ticker": ticker,
                "asset": pos.get("asset", ticker),
                "entry_price": pos.get("entry_price"),
                "daily": daily_ind,
                "weekly": weekly_ind,
            }

        results = await asyncio.gather(*[_calc_one(p) for p in positions], return_exceptions=True)
        results = [r for r in results if isinstance(r, dict)]

        sections = []
        for r in results:
            if r.get("error"):
                sections.append(f"### {r['ticker']}（{r.get('asset', '')}）\n\n{r['error']}\n")
                continue
            header = f"### {r['ticker']}（{r.get('asset', '')}）| 入场价: {r.get('entry_price', 'N/A')}"
            daily_text = format_indicators(r["daily"])
            signals_text = format_signals(r["daily"]["signals"])
            weekly_summary = ""
            if r.get("weekly"):
                w = r["weekly"]
                weekly_summary = (
                    f"\n**周线概要：** 趋势={w['trend']['trend']}({w['trend']['strength']}) "
                    f"RSI={w['momentum']['rsi']} {w['momentum']['rsi_state']}"
                )
            sections.append(f"{header}\n\n{daily_text}{weekly_summary}\n\n**信号：**\n{signals_text}\n")

        positions_technical_data = "\n---\n\n".join(sections)

        prompt_template = self._load_portfolio_prompt()
        prompt = prompt_template.format(positions_technical_data=positions_technical_data)

        stream_resp = await get_client(model).chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            stream=True,
            **build_extra_params(model),
        )

        chunks = []
        async for chunk in stream_resp:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if has_reasoning(model) and hasattr(delta, "reasoning_content") and delta.reasoning_content:
                continue
            if delta.content:
                chunks.append(delta.content)
                yield delta.content

        if chunks:
            full = "".join(chunks)
            report_generator.save_cache(CACHE_TYPE, "portfolio", full, model)
