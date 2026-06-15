"""交易分析模块 — 解析嘉信 CSV，FIFO 匹配买卖，生成量化诊断报告"""
import csv
import io
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import data_fetcher
from llm_client import get_client, resolve_model, has_reasoning, build_extra_params


@dataclass
class Trade:
    symbol: str
    side: str  # Buy / Sell
    date: datetime
    quantity: float
    price: float
    fees: float
    amount: float
    description: str = ""


@dataclass
class RoundTrip:
    """一次完整的开仓→平仓"""
    symbol: str
    entry_date: datetime
    exit_date: datetime
    quantity: float
    entry_price: float
    exit_price: float
    entry_cost: float
    exit_proceeds: float
    fees: float
    pnl_usd: float = 0.0
    pnl_pct: float = 0.0
    holding_days: int = 0

    def __post_init__(self):
        self.pnl_usd = self.exit_proceeds - self.entry_cost - self.fees
        self.pnl_pct = self.pnl_usd / self.entry_cost if self.entry_cost > 0 else 0.0
        self.holding_days = (self.exit_date - self.entry_date).days


def parse_schwab_csv(content: str) -> list[Trade]:
    """解析嘉信 CSV 交易记录"""
    trades = []
    reader = csv.DictReader(io.StringIO(content))
    for row in reader:
        action = row.get("Action", "").strip()
        if action not in ("Buy", "Sell"):
            continue
        symbol = row.get("Symbol", "").strip()
        if not symbol:
            continue

        date_str = row.get("Date", "").strip()
        try:
            date = datetime.strptime(date_str, "%m/%d/%Y")
        except ValueError:
            continue

        quantity = _parse_number(row.get("Quantity", ""))
        price = _parse_dollar(row.get("Price", ""))
        fees = _parse_dollar(row.get("Fees & Comm", ""))
        amount = _parse_dollar(row.get("Amount", ""))

        if quantity is None or price is None:
            continue

        trades.append(Trade(
            symbol=symbol,
            side=action,
            date=date,
            quantity=abs(quantity),
            price=price,
            fees=fees or 0.0,
            amount=amount or 0.0,
            description=row.get("Description", "").strip(),
        ))

    trades.sort(key=lambda t: t.date)
    return trades


def match_round_trips(trades: list[Trade]) -> tuple[list[RoundTrip], dict[str, list[Trade]]]:
    """FIFO 匹配：返回已完成的 round trips + 剩余未平仓头寸"""
    by_symbol: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        by_symbol[t.symbol].append(t)

    round_trips: list[RoundTrip] = []
    open_positions: dict[str, list[Trade]] = {}

    for symbol, sym_trades in by_symbol.items():
        queue: list[dict] = []  # FIFO queue of {date, qty_remaining, price, cost_per_unit}
        for t in sym_trades:
            if t.side == "Buy":
                queue.append({
                    "date": t.date,
                    "qty_remaining": t.quantity,
                    "price": t.price,
                    "cost_per_unit": abs(t.amount) / t.quantity if t.quantity > 0 else t.price,
                })
            elif t.side == "Sell":
                sell_qty = t.quantity
                sell_proceeds_per_unit = abs(t.amount) / t.quantity if t.quantity > 0 else t.price
                while sell_qty > 0 and queue:
                    lot = queue[0]
                    matched_qty = min(sell_qty, lot["qty_remaining"])
                    entry_cost = matched_qty * lot["cost_per_unit"]
                    exit_proceeds = matched_qty * sell_proceeds_per_unit

                    round_trips.append(RoundTrip(
                        symbol=symbol,
                        entry_date=lot["date"],
                        exit_date=t.date,
                        quantity=matched_qty,
                        entry_price=lot["price"],
                        exit_price=t.price,
                        entry_cost=entry_cost,
                        exit_proceeds=exit_proceeds,
                        fees=t.fees * (matched_qty / t.quantity) if t.quantity > 0 else 0,
                    ))

                    lot["qty_remaining"] -= matched_qty
                    sell_qty -= matched_qty
                    if lot["qty_remaining"] <= 0.001:
                        queue.pop(0)

        if queue:
            open_positions[symbol] = [
                Trade(symbol=symbol, side="Buy", date=lot["date"],
                      quantity=lot["qty_remaining"], price=lot["price"],
                      fees=0, amount=-lot["qty_remaining"] * lot["cost_per_unit"])
                for lot in queue if lot["qty_remaining"] > 0.001
            ]

    round_trips.sort(key=lambda r: r.exit_date)
    return round_trips, open_positions


def compute_analytics(round_trips: list[RoundTrip], open_positions: dict[str, list[Trade]]) -> dict:
    """生成量化分析指标"""
    if not round_trips:
        return {"error": "no_closed_trades"}

    # --- 基础统计 ---
    total = len(round_trips)
    winners = [r for r in round_trips if r.pnl_usd > 0]
    losers = [r for r in round_trips if r.pnl_usd <= 0]
    win_rate = len(winners) / total

    gross_profit = sum(r.pnl_usd for r in winners)
    gross_loss = abs(sum(r.pnl_usd for r in losers))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    avg_win = gross_profit / len(winners) if winners else 0
    avg_loss = gross_loss / len(losers) if losers else 0
    expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

    # --- 持仓时间分析 ---
    avg_holding_all = sum(r.holding_days for r in round_trips) / total
    avg_holding_win = sum(r.holding_days for r in winners) / len(winners) if winners else 0
    avg_holding_loss = sum(r.holding_days for r in losers) / len(losers) if losers else 0

    # --- 连续亏损 ---
    max_consec_loss = 0
    curr_consec = 0
    for r in round_trips:
        if r.pnl_usd <= 0:
            curr_consec += 1
            max_consec_loss = max(max_consec_loss, curr_consec)
        else:
            curr_consec = 0

    # --- 按 ticker 分析 ---
    by_symbol: dict[str, list[RoundTrip]] = defaultdict(list)
    for r in round_trips:
        by_symbol[r.symbol].append(r)

    symbol_stats = {}
    for sym, trips in by_symbol.items():
        sym_winners = [r for r in trips if r.pnl_usd > 0]
        sym_total_pnl = sum(r.pnl_usd for r in trips)
        sym_total_cost = sum(r.entry_cost for r in trips)
        symbol_stats[sym] = {
            "trades": len(trips),
            "win_rate": len(sym_winners) / len(trips) if trips else 0,
            "total_pnl_usd": round(sym_total_pnl, 2),
            "avg_pnl_pct": round(sum(r.pnl_pct for r in trips) / len(trips) * 100, 2),
            "avg_holding_days": round(sum(r.holding_days for r in trips) / len(trips), 1),
            "total_capital_deployed": round(sym_total_cost, 2),
        }

    # --- 日线 PnL 曲线 ---
    cumulative_pnl = []
    running = 0.0
    for r in round_trips:
        running += r.pnl_usd
        cumulative_pnl.append({
            "date": r.exit_date.strftime("%Y-%m-%d"),
            "symbol": r.symbol,
            "pnl": round(r.pnl_usd, 2),
            "cumulative": round(running, 2),
        })

    # --- 最大回撤 ---
    peak = 0.0
    max_drawdown = 0.0
    running = 0.0
    for r in round_trips:
        running += r.pnl_usd
        peak = max(peak, running)
        dd = (peak - running) / peak if peak > 0 else 0
        max_drawdown = max(max_drawdown, dd)

    # --- 未平仓持仓 ---
    open_summary = {}
    for sym, lots in open_positions.items():
        total_qty = sum(t.quantity for t in lots)
        avg_cost = sum(t.quantity * t.price for t in lots) / total_qty if total_qty > 0 else 0
        open_summary[sym] = {
            "quantity": round(total_qty, 4),
            "avg_cost": round(avg_cost, 4),
            "total_invested": round(total_qty * avg_cost, 2),
        }

    return {
        "summary": {
            "total_trades": total,
            "win_rate": round(win_rate * 100, 1),
            "profit_factor": round(profit_factor, 2),
            "expectancy_per_trade": round(expectancy, 2),
            "total_pnl": round(sum(r.pnl_usd for r in round_trips), 2),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(-gross_loss, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(-avg_loss, 2),
            "avg_win_pct": round(sum(r.pnl_pct for r in winners) / len(winners) * 100, 2) if winners else 0,
            "avg_loss_pct": round(sum(r.pnl_pct for r in losers) / len(losers) * 100, 2) if losers else 0,
            "max_consecutive_losses": max_consec_loss,
            "max_drawdown_pct": round(max_drawdown * 100, 1),
        },
        "holding_time": {
            "avg_all_days": round(avg_holding_all, 1),
            "avg_winners_days": round(avg_holding_win, 1),
            "avg_losers_days": round(avg_holding_loss, 1),
        },
        "by_symbol": symbol_stats,
        "open_positions": open_summary,
        "pnl_curve": cumulative_pnl,
    }


async def generate_review(analytics: dict, model: str | None = None) -> str:
    """用 DeepSeek R1 生成交易行为诊断"""
    model = resolve_model(model)
    prompt = _build_review_prompt(analytics)

    client = get_client(model)
    chunks = []
    stream = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=6000,
        temperature=0.3,
        stream=True,
        **build_extra_params(model),
    )
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if has_reasoning(model) and hasattr(delta, "reasoning_content") and delta.reasoning_content:
            continue
        if delta.content:
            chunks.append(delta.content)
    return "".join(chunks)


def _build_review_prompt(analytics: dict) -> str:
    import json
    stats = json.dumps(analytics, ensure_ascii=False, indent=2)
    return f"""你是一位有 20 年经验的量化交易教练。请基于以下交易统计数据，生成一份简洁的交易行为诊断报告。

## 统计数据
```json
{stats}
```

## 报告要求

请输出以下章节（中文）：

### 1. 核心指标评价
- 胜率、盈亏比、期望值是否健康
- Profit Factor 在什么水平

### 2. 行为模式诊断
- 是否存在"截断利润、放飞亏损"的问题（对比 avg_win vs avg_loss、持仓天数差异）
- 是否存在过度交易（频繁短线进出同一标的）
- 仓位管理是否合理

### 3. 按标的分析
- 哪些标的贡献了正收益，哪些在消耗账户
- 每个标的的交易风格是否一致

### 4. 具体改进建议
- 给出 3-5 条可执行的具体建议
- 每条建议要有量化依据（引用上面的数据）

### 5. 未平仓持仓风险评估
- 当前持仓集中度
- 需要关注的风险点

注意：
- 语言简洁有力，不要客套话
- 有问题直接指出，不要回避
- 建议要可操作，不是泛泛而谈
"""


def _parse_number(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.strip().replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _parse_dollar(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.strip().replace("$", "").replace(",", "")
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None
