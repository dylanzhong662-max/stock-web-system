"""交易成本模型

估算每笔建议的往返成本（round-trip）：
  1. 佣金（commission）
  2. 买卖价差（bid-ask spread）
  3. 市场冲击（market impact，Almgren-Chriss 简化）

对外暴露 estimate_cost() 和 annotate_recommendations()。
后者从持仓报告中为每条 加仓/减仓 建议附加实际成本估算。
"""
import asyncio
from dataclasses import dataclass
from typing import Optional


@dataclass
class CostEstimate:
    ticker: str
    trade_value_usd: float
    commission_bps: float
    spread_bps: float
    impact_bps: float
    total_bps: float
    total_usd: float
    annualized_drag_pct: float  # 假设月度调仓的年化成本
    note: str

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "trade_value_usd": round(self.trade_value_usd, 2),
            "commission_bps": round(self.commission_bps, 1),
            "spread_bps": round(self.spread_bps, 1),
            "impact_bps": round(self.impact_bps, 1),
            "total_bps": round(self.total_bps, 1),
            "total_usd": round(self.total_usd, 2),
            "annualized_drag_pct": round(self.annualized_drag_pct, 3),
            "note": self.note,
        }


# 资产类型推断
def _asset_class(ticker: str) -> str:
    t = ticker.upper()
    if t in ("BTC", "ETH") or t.endswith("-USD"):
        return "crypto"
    if t.endswith((".SH", ".SZ", ".SS")):
        return "cn_equity"
    if t.endswith(".HK"):
        return "hk_equity"
    return "us_equity"


# 各资产类型默认参数
_PARAMS = {
    "us_equity": {
        "commission_bps": 0.5,   # IB/Schwab ~$0.005/share ≈ 0.5bps
        "spread_bps": 3.0,       # 大盘 1-2bps, 中盘 3-5bps, 取中
        "daily_vol": 0.02,       # SPY 级别日波动率
        "participation_safe": 0.01,
    },
    "hk_equity": {
        "commission_bps": 3.0,   # 港股佣金较高
        "spread_bps": 5.0,
        "daily_vol": 0.02,
        "participation_safe": 0.008,
    },
    "cn_equity": {
        "commission_bps": 3.0,   # A 股印花税 + 佣金
        "spread_bps": 3.0,
        "daily_vol": 0.025,
        "participation_safe": 0.005,
    },
    "crypto": {
        "commission_bps": 10.0,  # CEX maker 费
        "spread_bps": 8.0,
        "daily_vol": 0.04,
        "participation_safe": 0.005,
    },
}


def estimate_cost(
    ticker: str,
    trade_value_usd: float,
    avg_daily_volume_usd: float | None = None,
    holding_period_days: int = 30,
    turnover_per_year: int = 12,
) -> CostEstimate:
    """估算单笔交易的往返成本（买入+卖出）

    Args:
        ticker: 资产代码
        trade_value_usd: 交易金额（美元）
        avg_daily_volume_usd: 日均成交金额（美元），None 则用默认假设
        holding_period_days: 预期持有天数
        turnover_per_year: 年调仓次数（用于计算年化拖累）
    """
    asset = _asset_class(ticker)
    params = _PARAMS.get(asset, _PARAMS["us_equity"])

    commission_bps = params["commission_bps"]
    spread_bps = params["spread_bps"]

    # 市场冲击：Almgren-Chriss 简化 impact = σ_daily * √(participation)
    if avg_daily_volume_usd and avg_daily_volume_usd > 0:
        participation = trade_value_usd / avg_daily_volume_usd
    else:
        # 无成交量数据：按中盘股假设 ADV $50M
        default_adv = {
            "us_equity": 50_000_000,
            "hk_equity": 20_000_000,
            "cn_equity": 30_000_000,
            "crypto": 100_000_000,
        }
        participation = trade_value_usd / default_adv.get(asset, 50_000_000)

    impact_raw = params["daily_vol"] * (participation ** 0.5)
    # 持仓越短，impact 比重越大（短期交易的 alpha 需要覆盖更多成本）
    impact_scaled = impact_raw * (20 / max(holding_period_days, 1)) ** 0.3
    impact_bps = impact_scaled * 10000

    # 往返成本 = 2 * (佣金 + 半个 spread + impact)
    one_way_bps = commission_bps + spread_bps / 2 + impact_bps
    total_bps = one_way_bps * 2
    total_usd = trade_value_usd * total_bps / 10000

    # 年化拖累 = 单次往返 × 年调仓次数
    annual_drag = total_bps * turnover_per_year / 10000

    note = ""
    if total_bps > 50:
        note = "成本较高，需确认 alpha 足以覆盖"
    elif total_bps > 100:
        note = "成本极高，建议降低仓位或延长持有期"

    return CostEstimate(
        ticker=ticker,
        trade_value_usd=trade_value_usd,
        commission_bps=commission_bps * 2,
        spread_bps=spread_bps,
        impact_bps=impact_bps * 2,
        total_bps=total_bps,
        total_usd=total_usd,
        annualized_drag_pct=annual_drag * 100,
        note=note,
    )


def estimate_portfolio_costs(
    positions: list[dict],
    rebalance_pct: float = 0.2,
    turnover_per_year: int = 12,
) -> dict:
    """估算组合级别的年化交易成本

    Args:
        positions: enriched 持仓列表（含 financial.current_price, quantity）
        rebalance_pct: 每次调仓平均动用总仓位比例
        turnover_per_year: 年调仓次数
    """
    costs = []
    total_value = 0.0

    for p in positions:
        fin = p.get("financial", {}) or {}
        price = fin.get("current_price") or p.get("entry_price")
        qty = p.get("quantity", 1)
        if not price:
            continue
        pos_value = float(price) * float(qty)
        total_value += pos_value

        trade_value = pos_value * rebalance_pct
        adv = fin.get("avg_volume_usd")  # 如果数据源有提供
        cost = estimate_cost(
            ticker=p["ticker"],
            trade_value_usd=trade_value,
            avg_daily_volume_usd=adv,
            holding_period_days=30,
            turnover_per_year=turnover_per_year,
        )
        costs.append(cost)

    if not costs:
        return {"total_value": 0, "costs": [], "portfolio_annual_drag_bps": 0}

    # 加权年化拖累
    weighted_drag = sum(
        c.annualized_drag_pct * c.trade_value_usd / rebalance_pct
        for c in costs
    )
    portfolio_drag = weighted_drag / total_value if total_value > 0 else 0

    return {
        "total_value_usd": round(total_value, 2),
        "costs": [c.to_dict() for c in costs],
        "portfolio_annual_drag_bps": round(portfolio_drag * 100, 1),
        "turnover_assumption": turnover_per_year,
        "rebalance_assumption_pct": rebalance_pct * 100,
    }


def format_cost_section(costs_data: dict) -> str:
    """格式化为 Markdown 段落，注入持仓分析 prompt"""
    if not costs_data or not costs_data.get("costs"):
        return "（交易成本数据不可用）"

    lines = [
        f"- 组合总市值：${costs_data['total_value_usd']:,.0f}",
        f"- 年化调仓假设：{costs_data['turnover_assumption']}次/年，"
        f"每次动用{costs_data['rebalance_assumption_pct']:.0f}%仓位",
        f"- **组合年化交易拖累：{costs_data['portfolio_annual_drag_bps']:.1f} bps**",
        "",
        "  逐持仓成本估算：",
    ]
    for c in costs_data["costs"]:
        lines.append(
            f"    - {c['ticker']}: 往返 {c['total_bps']:.1f}bps "
            f"（佣金{c['commission_bps']:.0f} + 价差{c['spread_bps']:.0f} + "
            f"冲击{c['impact_bps']:.0f}）"
            f"{' ⚠️' + c['note'] if c['note'] else ''}"
        )

    return "\n".join(lines)
