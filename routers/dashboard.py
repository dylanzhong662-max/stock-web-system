import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import get_db
from models import Position
from schemas import DashboardSummary, MacroData, Alert
import signal_reader
import price_fetcher

router = APIRouter()


@router.get("/summary", response_model=DashboardSummary)
def get_summary(db: Session = Depends(get_db)):
    positions = db.query(Position).filter(Position.status == "open").all()
    alerts = []
    needs_action = 0
    total_cost = sum(pos.cost_basis_usd for pos in positions)

    for pos in positions:
        sig = signal_reader.extract_signal_summary(pos.ticker or pos.asset) or \
              signal_reader.extract_signal_summary(pos.asset)
        if sig:
            sig_action = sig.get("action")
            if (pos.direction == "long" and sig_action == "short") or \
               (pos.direction == "short" and sig_action == "long"):
                needs_action += 1
                alerts.append(Alert(
                    asset=pos.asset, status="SIGNAL_REVERSED",
                    message=f"{pos.asset} LLM 信号方向已反转，建议平仓",
                    severity="warning",
                ))

    all_signals = signal_reader.read_all_signals()
    active_signals = sum(
        1 for s in all_signals.values()
        if s and s.get("action") not in (None, "no_trade")
    )

    return DashboardSummary(
        portfolio_value=round(total_cost, 2),
        total_unrealized_pnl_usd=0.0,
        total_unrealized_pnl_pct=0.0,
        active_signals=active_signals,
        open_positions=len(positions),
        needs_action=needs_action,
        alerts=alerts,
    )


@router.get("/macro", response_model=MacroData)
def get_macro():
    macro = price_fetcher.get_macro_prices()
    scan = signal_reader.read_market_scan() or {}

    sentiment = "Neutral"
    for asset in ["GOLD", "GOOGL", "NVDA"]:
        sig = signal_reader.extract_signal_summary(asset)
        if sig and sig.get("market_sentiment"):
            sentiment = sig["market_sentiment"]
            break

    fear_greed = fear_greed_label = None
    btc_raw = signal_reader.read_signal("BTC")
    if btc_raw:
        summary = btc_raw.get("sentiment_summary", {})
        fear_greed = summary.get("fear_greed_index")
        fear_greed_label = summary.get("fear_greed_classification")

    return MacroData(
        sentiment=sentiment,
        vix=macro.get("VIX"),
        dxy=macro.get("DXY"),
        ten_year=macro.get("TNX"),
        btc_fear_greed=fear_greed,
        btc_fear_greed_label=fear_greed_label,
        last_scan_date=scan.get("scan_date"),
        sector_ranking=scan.get("sector_ranking", []),
        top_opportunities=scan.get("top_opportunities", []),
    )
