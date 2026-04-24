import json
import os
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PORTFOLIO_JSON = os.path.join(DATA_DIR, "portfolio.json")


def sync_to_json(db):
    from models import Position
    positions = db.query(Position).filter(Position.status == "open").all()
    data = {
        "_comment": "Auto-synced from trading.db by holderAndAction backend",
        "_last_sync": datetime.now().isoformat(),
        "positions": [
            {
                "asset": p.asset,
                "type": p.direction,
                "entry_price": p.entry_price,
                "entry_date": p.entry_date,
                "quantity": p.quantity,
                "cost_basis_usd": p.cost_basis_usd,
                "stop_loss": p.stop_loss,
                "profit_target": p.profit_target,
                "trailing_stop": p.trailing_stop,
                "source_signal": p.source_signal,
                "exchange": p.exchange,
                "symbol": p.symbol,
                "notes": p.notes,
            }
            for p in positions
        ],
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PORTFOLIO_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
