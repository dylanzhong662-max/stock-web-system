"""回测脚本：对比新旧止盈止损算法在历史交易中的表现

用法:
  python backtest_stops.py              # 用本地 DB 中已平仓交易回测
  python backtest_stops.py --live       # 用当前开仓持仓做 what-if 分析

算法对比:
  - OLD: 固定 2×ATR 止损, 8% hard cap, 固定 ATR 止盈
  - NEW: ATR(RMA) + 基本面止损/止盈天花板 + 波动率适配 + 财报风险

输出:
  - 每笔交易的新旧止损对比
  - 统计: 胜率、平均盈亏比、最大回撤改善
"""
import os
import sys
import sqlite3
import numpy as np
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from update_stops import fetch_ohlc, calc_atr, compute_stops, ATR_PERIOD, STOP_MULT, TARGET_1_MULT, TARGET_2_MULT, MAX_LOSS_PCT
from fundamental_stops import compute_fundamental_stops, merge_stops

DB_PATH = os.environ.get(
    "HOLDER_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "trading.db"),
)


def calc_atr_sma(ohlc: list, period: int = ATR_PERIOD) -> float:
    """旧算法: SMA-based ATR"""
    if len(ohlc) < period + 1:
        return 0
    highs = np.array([r["high"] for r in ohlc])
    lows = np.array([r["low"] for r in ohlc])
    closes = np.array([r["close"] for r in ohlc])
    prev_close = np.roll(closes, 1)
    prev_close[0] = closes[0]
    tr = np.maximum(highs - lows, np.maximum(np.abs(highs - prev_close), np.abs(lows - prev_close)))
    return float(np.mean(tr[-period:]))


def compute_old_stops(current_price, entry_price, direction, ohlc):
    """旧算法: 固定 8% cap + SMA ATR"""
    atr = calc_atr_sma(ohlc)
    if atr == 0:
        return None
    if direction == "long":
        base_stop = entry_price - STOP_MULT * atr
        max_loss_stop = entry_price * (1 - MAX_LOSS_PCT)
        stop_loss = max(base_stop, max_loss_stop)
        target_1 = entry_price + TARGET_1_MULT * atr
        target_2 = entry_price + TARGET_2_MULT * atr
    else:
        base_stop = entry_price + STOP_MULT * atr
        max_loss_stop = entry_price * (1 + MAX_LOSS_PCT)
        stop_loss = min(base_stop, max_loss_stop)
        target_1 = entry_price - TARGET_1_MULT * atr
        target_2 = entry_price - TARGET_2_MULT * atr
    return {"stop_loss": round(stop_loss, 2), "profit_target": round(target_1, 2), "profit_target_2": round(target_2, 2)}


def simulate_trade_outcome(entry_price, direction, stop_loss, profit_target, ohlc_after_entry):
    """模拟: 从入场后的 OHLC 数据中，看是先触止损还是先触止盈"""
    for bar in ohlc_after_entry:
        if direction == "long":
            if bar["low"] <= stop_loss:
                return "stop", stop_loss
            if bar["high"] >= profit_target:
                return "target", profit_target
        else:
            if bar["high"] >= stop_loss:
                return "stop", stop_loss
            if bar["low"] <= profit_target:
                return "target", profit_target
    # 未触发，用最后收盘价
    if ohlc_after_entry:
        return "open", ohlc_after_entry[-1]["close"]
    return "open", entry_price


def backtest_closed_trades(conn):
    """用已平仓交易回测新旧算法"""
    trades = conn.execute(
        "SELECT ticker, asset, direction, entry_price, exit_price, entry_date, exit_date, "
        "realized_pnl_pct, exit_reason FROM trades ORDER BY exit_date"
    ).fetchall()

    if not trades:
        print("没有已平仓交易记录，跳过历史回测。")
        return

    print(f"\n{'='*70}")
    print(f" 历史交易回测 — {len(trades)} 笔已平仓交易")
    print(f"{'='*70}\n")

    old_results = []
    new_results = []

    for t in trades:
        ticker = t["ticker"] or t["asset"]
        direction = t["direction"] or "long"
        entry_price = float(t["entry_price"])
        exit_price = float(t["exit_price"]) if t["exit_price"] else None
        pnl_pct = float(t["realized_pnl_pct"]) if t["realized_pnl_pct"] else None

        if not exit_price or not pnl_pct:
            continue

        ohlc = fetch_ohlc(ticker)
        if not ohlc or len(ohlc) < 20:
            print(f"  {ticker}: 无法获取 OHLC，跳过")
            continue

        # 回测用入场价模拟入场时的状态（非当前价）
        current_price = entry_price

        # 旧算法
        old = compute_old_stops(current_price, entry_price, direction, ohlc)
        if not old:
            continue

        # 新算法: ATR(RMA) + 基本面
        atr_new = compute_stops(
            current_price=current_price,
            entry_price=entry_price,
            direction=direction,
            ohlc=ohlc,
        )
        if not atr_new:
            continue

        closes = np.array([r["close"] for r in ohlc])
        rets = np.diff(closes) / closes[:-1]
        daily_std = float(np.std(rets))

        fundamental = None
        try:
            fundamental = compute_fundamental_stops(
                ticker=ticker,
                current_price=current_price,
                entry_price=entry_price,
                direction=direction,
                daily_returns_std=daily_std,
            )
        except Exception:
            pass

        new = merge_stops(
            atr_result=atr_new,
            fundamental_result=fundamental,
            direction=direction,
            entry_price=entry_price,
            current_price=current_price,
        )

        # 计算如果用新/旧止损，实际出场会怎样
        if direction == "long":
            old_risk = entry_price - old["stop_loss"]
            old_reward = old["profit_target"] - entry_price
            new_risk = entry_price - new["stop_loss"]
            new_reward = new["profit_target"] - entry_price
            # 实际 P&L vs 止损距离
            old_would_stop = exit_price <= old["stop_loss"]
            new_would_stop = exit_price <= new["stop_loss"]
        else:
            old_risk = old["stop_loss"] - entry_price
            old_reward = entry_price - old["profit_target"]
            new_risk = new["stop_loss"] - entry_price
            new_reward = entry_price - new["profit_target"]
            old_would_stop = exit_price >= old["stop_loss"]
            new_would_stop = exit_price >= new["stop_loss"]

        old_rr = old_reward / old_risk if old_risk > 0 else 0
        new_rr = new_reward / new_risk if new_risk > 0 else 0

        old_results.append({
            "ticker": ticker, "pnl_pct": pnl_pct, "rr": old_rr,
            "stop": old["stop_loss"], "target": old["profit_target"],
            "would_stop": old_would_stop,
        })
        new_results.append({
            "ticker": ticker, "pnl_pct": pnl_pct, "rr": new_rr,
            "stop": new["stop_loss"], "target": new["profit_target"],
            "would_stop": new_would_stop,
            "fund_notes": new.get("merge_notes", []),
        })

        # 对比输出
        fund_tag = ""
        if fundamental and fundamental.get("valuation_percentile"):
            fund_tag = f" [估值P{fundamental['valuation_percentile']:.0f}]"
        if fundamental and fundamental.get("earnings_risk"):
            fund_tag += " [⚠️财报]"

        print(f"  {ticker} (实际P&L: {pnl_pct:+.1f}%)")
        print(f"    旧: stop={old['stop_loss']:.2f} target={old['profit_target']:.2f} R:R=1:{old_rr:.1f}")
        print(f"    新: stop={new['stop_loss']:.2f} target={new['profit_target']:.2f} R:R=1:{new_rr:.1f}{fund_tag}")
        if new.get("merge_notes"):
            for note in new["merge_notes"][:2]:
                print(f"      → {note}")
        print()

    if not old_results:
        print("没有足够数据进行统计对比。")
        return

    # 统计汇总
    print(f"\n{'='*70}")
    print(f" 统计汇总 ({len(old_results)} 笔可对比交易)")
    print(f"{'='*70}\n")

    old_rrs = [r["rr"] for r in old_results]
    new_rrs = [r["rr"] for r in new_results]
    old_stops = sum(1 for r in old_results if r["would_stop"])
    new_stops = sum(1 for r in new_results if r["would_stop"])

    print(f"  {'指标':<20} {'旧算法':<15} {'新算法':<15} {'改善'}")
    print(f"  {'-'*60}")
    print(f"  {'平均R:R':<20} {np.mean(old_rrs):<15.2f} {np.mean(new_rrs):<15.2f} {np.mean(new_rrs)-np.mean(old_rrs):+.2f}")
    print(f"  {'中位R:R':<18} {np.median(old_rrs):<15.2f} {np.median(new_rrs):<15.2f} {np.median(new_rrs)-np.median(old_rrs):+.2f}")
    print(f"  {'触发止损次数':<16} {old_stops:<15} {new_stops:<15} {new_stops-old_stops:+d}")
    print(f"  {'止损触发率':<18} {old_stops/len(old_results)*100:<15.1f}% {new_stops/len(new_results)*100:<13.1f}% {(new_stops-old_stops)/len(old_results)*100:+.1f}pp")
    print()


def backtest_open_positions(conn):
    """对当前开仓持仓做 what-if 分析"""
    positions = conn.execute(
        "SELECT id, ticker, asset, entry_price, direction, current_price, stop_loss "
        "FROM positions WHERE status = 'open'"
    ).fetchall()

    if not positions:
        print("\n没有开仓持仓。")
        return

    print(f"\n{'='*70}")
    print(f" 当前持仓 What-If 分析 — {len(positions)} 个开仓")
    print(f"{'='*70}\n")
    print(f"  {'Ticker':<10} {'方向':<6} {'入场':<10} {'现价':<10} {'旧止损':<10} {'新止损':<10} {'旧目标':<10} {'新目标':<10} {'基本面'}")
    print(f"  {'-'*90}")

    for pos in positions:
        ticker = pos["ticker"] or pos["asset"]
        direction = pos["direction"] or "long"
        entry_price = float(pos["entry_price"]) if pos["entry_price"] else None
        if not entry_price:
            continue

        ohlc = fetch_ohlc(ticker)
        if not ohlc or len(ohlc) < 20:
            print(f"  {ticker:<10} 无 OHLC 数据")
            continue

        current_price = float(ohlc[-1]["close"])

        # 旧算法
        old = compute_old_stops(current_price, entry_price, direction, ohlc)
        if not old:
            continue

        # 新算法
        atr_new = compute_stops(
            current_price=current_price,
            entry_price=entry_price,
            direction=direction,
            ohlc=ohlc,
        )
        if not atr_new:
            continue

        closes = np.array([r["close"] for r in ohlc])
        rets = np.diff(closes) / closes[:-1]
        daily_std = float(np.std(rets))

        fundamental = None
        try:
            fundamental = compute_fundamental_stops(
                ticker=ticker,
                current_price=current_price,
                entry_price=entry_price,
                direction=direction,
                daily_returns_std=daily_std,
            )
        except Exception:
            pass

        new = merge_stops(
            atr_result=atr_new,
            fundamental_result=fundamental,
            direction=direction,
            entry_price=entry_price,
            current_price=current_price,
        )

        fund_info = ""
        if fundamental:
            parts = []
            if fundamental.get("valuation_percentile"):
                parts.append(f"P{fundamental['valuation_percentile']:.0f}")
            if fundamental.get("earnings_risk"):
                parts.append(f"⚠️ER:{fundamental['earnings_date']}")
            if fundamental.get("pe_ratio"):
                parts.append(f"PE:{fundamental['pe_ratio']:.0f}")
            if fundamental.get("vol_adjusted_max_loss"):
                parts.append(f"vol:{fundamental['vol_adjusted_max_loss']*100:.0f}%")
            fund_info = " ".join(parts)

        print(f"  {ticker:<10} {direction:<6} ${entry_price:<9.2f} ${current_price:<9.2f} "
              f"${old['stop_loss']:<9.2f} ${new['stop_loss']:<9.2f} "
              f"${old['profit_target']:<9.2f} ${new['profit_target']:<9.2f} {fund_info}")

    print()


def main():
    if not os.path.exists(DB_PATH):
        print(f"DB not found: {DB_PATH}")
        print("请在服务器上运行此脚本，或设置 HOLDER_DB_PATH 环境变量")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 检查是否有表
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if "trades" not in tables and "positions" not in tables:
        print("数据库为空（无 positions/trades 表），无法回测。")
        print("请在有数据的服务器上运行：")
        print("  cd /opt/holder-action && source .env && .venv/bin/python backtest_stops.py")
        conn.close()
        sys.exit(1)

    live_mode = "--live" in sys.argv

    if not live_mode and "trades" in tables:
        backtest_closed_trades(conn)

    if "positions" in tables:
        backtest_open_positions(conn)

    conn.close()


if __name__ == "__main__":
    main()
