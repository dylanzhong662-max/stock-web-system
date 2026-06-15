"""盈利加仓顾问 — 基于 ATR + 倒金字塔规则生成加仓/止损建议

核心原则：只在浮盈覆盖加仓风险时才允许加仓，加仓后最坏结果仍为盈利或平手。
"""
import asyncio
import os
import sqlite3
from datetime import datetime, date
from typing import Optional

from data_providers.quant import _get_fmp_ohlc
from data_providers.ticker_utils import fmp_ticker

HOLDER_DB_PATH = os.getenv(
    "HOLDER_DB_PATH",
    os.path.expanduser("~/Desktop/holderAndAction/data/trading.db"),
)

PYRAMID_RATIOS = [5, 3, 2]  # 建仓:首次加仓:末次加仓
ATR_PERIOD = 14
SCALE_TRIGGER_ATR = 1.0  # 浮盈 >= 1×ATR 才触发
MA_PERIOD = 5
MAX_SCALE_DAYS = 5  # 建仓超过5天不加仓
UPPER_SHADOW_RATIO = 0.6  # 上影线占振幅比例阈值
VOLUME_SPIKE_RATIO = 1.5  # 放量定义：当日成交量 > 20日均量×1.5


def _read_open_positions() -> list[dict]:
    if not os.path.exists(HOLDER_DB_PATH):
        return []
    conn = sqlite3.connect(HOLDER_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT id, ticker, asset, entry_price, entry_date, quantity, "
            "stop_loss, profit_target, direction, notes "
            "FROM positions WHERE status = 'open'"
        )
        return [dict(row) for row in cur.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def _parse_entry_date(entry_date_str: str) -> Optional[date]:
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(entry_date_str[:10], fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _days_since_entry(entry_date_str: str) -> Optional[int]:
    d = _parse_entry_date(entry_date_str)
    if d is None:
        return None
    return (date.today() - d).days


def _compute_atr(rows: list[dict], period: int = ATR_PERIOD) -> Optional[float]:
    """从 OHLC rows 计算 ATR"""
    import pandas as pd
    parsed = []
    for r in rows:
        try:
            parsed.append({
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
            })
        except (KeyError, ValueError, TypeError):
            continue
    if len(parsed) < period + 5:
        return None
    df = pd.DataFrame(parsed)
    prev = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"] - prev).abs(),
    ], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def _compute_ma5(rows: list[dict]) -> Optional[float]:
    closes = []
    for r in rows:
        try:
            closes.append(float(r["close"]))
        except (KeyError, ValueError, TypeError):
            continue
    if len(closes) < MA_PERIOD:
        return None
    return sum(closes[-MA_PERIOD:]) / MA_PERIOD


def _detect_upper_shadow_with_volume(rows: list[dict]) -> bool:
    """检测最近一根 K 线是否为放量长上影线"""
    if len(rows) < 21:
        return False
    latest = rows[-1]
    try:
        high = float(latest["high"])
        low = float(latest["low"])
        close = float(latest["close"])
        open_p = float(latest.get("open", close))
        vol = float(latest.get("volume", 0))
    except (KeyError, ValueError, TypeError):
        return False

    spread = high - low
    if spread <= 0:
        return False

    body_top = max(open_p, close)
    upper_shadow = high - body_top
    shadow_ratio = upper_shadow / spread

    vol_20 = []
    for r in rows[-21:-1]:
        try:
            vol_20.append(float(r.get("volume", 0)))
        except (ValueError, TypeError):
            continue
    avg_vol = sum(vol_20) / len(vol_20) if vol_20 else 0

    has_long_shadow = shadow_ratio >= UPPER_SHADOW_RATIO
    has_volume_spike = vol > avg_vol * VOLUME_SPIKE_RATIO if avg_vol > 0 else False

    return has_long_shadow and has_volume_spike


def _determine_scale_tier(pos: dict) -> int:
    """判断当前持仓处于第几档（基于 notes 或 quantity 推断）"""
    notes = (pos.get("notes") or "").lower()
    if "scale_tier=3" in notes or "tier3" in notes:
        return 3
    if "scale_tier=2" in notes or "tier2" in notes:
        return 2
    return 1


async def evaluate_scaling(positions: list[dict] = None) -> list[dict]:
    """评估所有开仓持仓的加仓条件，返回结构化建议"""
    if positions is None:
        positions = _read_open_positions()
    if not positions:
        return []

    async def _eval_one(pos: dict) -> dict:
        ticker = pos["ticker"]
        entry_price = float(pos["entry_price"])
        quantity = float(pos["quantity"])
        entry_date_str = pos.get("entry_date", "")
        direction = pos.get("direction", "long")
        current_tier = _determine_scale_tier(pos)

        result = {
            "ticker": ticker,
            "asset": pos.get("asset", ticker),
            "entry_price": entry_price,
            "quantity": quantity,
            "entry_date": entry_date_str,
            "direction": direction,
            "current_tier": current_tier,
            "conditions": {},
            "eligible": False,
            "recommendation": None,
        }

        if current_tier >= 3:
            result["recommendation"] = {
                "action": "hold",
                "reason": "已达最大加仓档位(3档)，不再加仓",
            }
            return result

        rows = await _get_fmp_ohlc(ticker, days=100)
        if not rows or len(rows) < ATR_PERIOD + 5:
            result["recommendation"] = {
                "action": "insufficient_data",
                "reason": f"OHLC 数据不足({len(rows) if rows else 0}天)，无法评估",
            }
            return result

        atr = _compute_atr(rows)
        if atr is None or atr <= 0:
            result["recommendation"] = {
                "action": "insufficient_data",
                "reason": "ATR 计算失败",
            }
            return result

        current_price = float(rows[-1].get("close", 0))
        if current_price <= 0:
            result["recommendation"] = {
                "action": "insufficient_data",
                "reason": "无法获取当前价格",
            }
            return result

        # --- 条件 1: 浮盈 >= 1×ATR ---
        if direction == "long":
            unrealized_profit = current_price - entry_price
        else:
            unrealized_profit = entry_price - current_price

        profit_in_atr = unrealized_profit / atr
        cond1 = profit_in_atr >= SCALE_TRIGGER_ATR
        result["conditions"]["profit_gte_1atr"] = {
            "met": cond1,
            "value": round(profit_in_atr, 2),
            "detail": f"浮盈 {unrealized_profit:+.2f} = {profit_in_atr:.2f}×ATR"
                      f"{'✓' if cond1 else '✗ (需≥1.0)'}",
        }

        # --- 条件 2: 价格在 5日均线之上 ---
        ma5 = _compute_ma5(rows)
        cond2 = current_price > ma5 if ma5 else False
        result["conditions"]["above_ma5"] = {
            "met": cond2,
            "value": round(ma5, 4) if ma5 else None,
            "detail": f"价格 {current_price:.2f} {'>' if cond2 else '<='} MA5 {ma5:.2f}"
                      f"{'✓' if cond2 else '✗'}" if ma5 else "MA5 无法计算",
        }

        # --- 条件 3: 无放量长上影线 ---
        has_shadow = _detect_upper_shadow_with_volume(rows)
        cond3 = not has_shadow
        result["conditions"]["no_volume_shadow"] = {
            "met": cond3,
            "detail": "无放量长上影线✓" if cond3 else "检测到放量长上影线✗ (有抛压)",
        }

        # --- 条件 4: 距离建仓 < 5天 ---
        days_held = _days_since_entry(entry_date_str)
        cond4 = days_held is not None and days_held < MAX_SCALE_DAYS
        result["conditions"]["within_5_days"] = {
            "met": cond4,
            "value": days_held,
            "detail": f"持仓 {days_held} 天{'✓' if cond4 else '✗ (超过5天动量不足)'}"
                      if days_held is not None else "入场日期无法解析",
        }

        # --- 综合判定 ---
        all_met = cond1 and cond2 and cond3 and cond4
        result["eligible"] = all_met
        result["market_data"] = {
            "current_price": round(current_price, 4),
            "atr": round(atr, 4),
            "ma5": round(ma5, 4) if ma5 else None,
            "profit_in_atr": round(profit_in_atr, 2),
            "days_held": days_held,
        }

        if all_met:
            result["recommendation"] = _build_scale_plan(
                pos, current_tier, entry_price, current_price, atr, quantity, direction
            )
        else:
            failed = [k for k, v in result["conditions"].items() if not v["met"]]
            if unrealized_profit < 0:
                result["recommendation"] = {
                    "action": "hold_or_stop",
                    "reason": "浮亏中，绝对禁止加仓（亏损加仓=灾难）",
                    "stop_loss": round(entry_price - 2 * atr, 4) if direction == "long"
                                 else round(entry_price + 2 * atr, 4),
                }
            else:
                result["recommendation"] = {
                    "action": "wait",
                    "reason": f"未满足条件: {', '.join(failed)}",
                    "current_stop": round(entry_price - 2 * atr, 4) if direction == "long"
                                    else round(entry_price + 2 * atr, 4),
                }

        return result

    results = await asyncio.gather(*[_eval_one(p) for p in positions], return_exceptions=True)
    return [r for r in results if isinstance(r, dict)]


def _build_scale_plan(
    pos: dict,
    current_tier: int,
    entry_price: float,
    current_price: float,
    atr: float,
    quantity: float,
    direction: str,
) -> dict:
    """生成加仓计划：比例、止损移动、最坏情景分析"""
    next_tier = current_tier + 1
    base_units = PYRAMID_RATIOS[0]
    scale_units = PYRAMID_RATIOS[next_tier - 1] if next_tier <= len(PYRAMID_RATIOS) else 0

    if scale_units == 0:
        return {"action": "hold", "reason": "已满仓，不再加仓"}

    scale_ratio = scale_units / base_units
    scale_quantity = round(quantity * scale_ratio, 2)

    if direction == "long":
        # 加仓后止损上移
        if next_tier == 2:
            new_stop = entry_price  # 移到成本线
        else:
            # 第3档加仓后，止损移到第2档入场价（≈entry + 1ATR）
            new_stop = entry_price + atr

        # 最坏情景（止损触发时）
        tier1_pnl = quantity * (new_stop - entry_price)
        tier2_pnl = scale_quantity * (new_stop - current_price)
        worst_case_pnl = tier1_pnl + tier2_pnl
    else:
        if next_tier == 2:
            new_stop = entry_price
        else:
            new_stop = entry_price - atr

        tier1_pnl = quantity * (entry_price - new_stop)
        tier2_pnl = scale_quantity * (current_price - new_stop)
        worst_case_pnl = tier1_pnl + tier2_pnl

    return {
        "action": "scale_in",
        "tier": next_tier,
        "scale_price": round(current_price, 4),
        "scale_quantity": scale_quantity,
        "scale_ratio": f"{scale_units}:{base_units} (倒金字塔第{next_tier}档)",
        "new_stop_loss": round(new_stop, 4),
        "stop_logic": f"止损上移至{'成本线' if next_tier == 2 else '第2档入场价'}",
        "worst_case": {
            "pnl_usd": round(worst_case_pnl, 2),
            "description": f"若止损触发@{new_stop:.2f}，"
                           f"净损益=${worst_case_pnl:+.2f}"
                           f"{'（仍盈利）' if worst_case_pnl >= 0 else '（小额亏损）'}",
        },
        "post_scale_position": {
            "total_quantity": round(quantity + scale_quantity, 2),
            "avg_cost": round(
                (quantity * entry_price + scale_quantity * current_price) /
                (quantity + scale_quantity), 4
            ),
        },
    }


def format_scaling_context(results: list[dict]) -> str:
    """格式化为注入 prompt 的上下文文本"""
    if not results:
        return ""

    lines = ["**盈利加仓评估（ATR 倒金字塔体系）：**\n"]

    eligible = [r for r in results if r.get("eligible")]
    not_eligible = [r for r in results if not r.get("eligible")]

    if eligible:
        lines.append("🟢 **满足加仓条件：**")
        for r in eligible:
            rec = r["recommendation"]
            md = r.get("market_data", {})
            lines.append(
                f"- **{r['ticker']}**: 浮盈 {md.get('profit_in_atr', 0):.1f}×ATR，"
                f"建议加仓 {rec['scale_quantity']} 股@{rec['scale_price']}，"
                f"止损移至 {rec['new_stop_loss']}，"
                f"最坏结果 {rec['worst_case']['description']}"
            )
        lines.append("")

    if not_eligible:
        lines.append("🔴 **未满足加仓条件（等待/持有）：**")
        for r in not_eligible:
            rec = r.get("recommendation", {})
            conds = r.get("conditions", {})
            failed = [v["detail"] for k, v in conds.items() if not v.get("met")]
            action = rec.get("action", "wait")
            if action == "hold_or_stop":
                lines.append(
                    f"- **{r['ticker']}**: ⚠️ 浮亏中，禁止加仓。"
                    f"止损位: {rec.get('stop_loss', 'N/A')}"
                )
            elif action == "insufficient_data":
                lines.append(f"- **{r['ticker']}**: 数据不足，{rec.get('reason', '')}")
            else:
                lines.append(
                    f"- **{r['ticker']}**: 等待 — {'; '.join(failed[:2])}"
                )
        lines.append("")

    lines.append(
        "> 加仓规则：浮盈≥1ATR + 价格>MA5 + 无放量上影 + 建仓<5天。"
        "比例 5:3:2 倒金字塔。加仓后止损必须上移，确保最坏结果不亏。"
    )
    return "\n".join(lines)
