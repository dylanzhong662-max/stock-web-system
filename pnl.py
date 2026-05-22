"""持仓盈亏计算工具"""
from typing import Optional


def calc_pnl(
    direction: str,
    entry_price: float,
    current_price: float,
    quantity: float = 1.0,
) -> tuple[Optional[float], Optional[float]]:
    """计算浮动盈亏。返回 (pnl_usd, pnl_pct)，entry_price 为 0 时返回 (None, None)"""
    if not entry_price or entry_price == 0:
        return None, None
    if direction == "long":
        pnl_usd = (current_price - entry_price) * quantity
        pnl_pct = (current_price - entry_price) / entry_price * 100
    else:
        pnl_usd = (entry_price - current_price) * quantity
        pnl_pct = (entry_price - current_price) / entry_price * 100
    return round(pnl_usd, 2), round(pnl_pct, 2)
