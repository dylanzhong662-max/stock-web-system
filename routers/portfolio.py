import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, date
from typing import List, Optional

from database import get_db
from models import Position, Trade
from schemas import PositionCreate, PositionUpdate, PositionClose, PositionResponse, TradeResponse
import price_fetcher
import signal_reader
import sync

router = APIRouter()


def _compute(pos: Position, current_price: Optional[float] = None) -> dict:
    if current_price is None:
        current_price = price_fetcher.get_current_price(pos.asset, pos.ticker or "")
    # 先用 ticker 查信号（去掉交易所后缀），再 fallback 到 asset
    sig = signal_reader.extract_signal_summary(pos.ticker or pos.asset) or \
          signal_reader.extract_signal_summary(pos.asset)

    pnl_usd = pnl_pct = dist_stop = dist_target = None
    position_status = "HOLD"

    if current_price:
        if pos.direction == "long":
            pnl_usd = (current_price - pos.entry_price) * pos.quantity
            pnl_pct = (current_price - pos.entry_price) / pos.entry_price * 100
            if pos.stop_loss:
                dist_stop = (current_price - pos.stop_loss) / pos.entry_price * 100
                if current_price <= pos.stop_loss:
                    position_status = "STOP_TRIGGERED"
            if pos.profit_target and position_status == "HOLD":
                dist_target = (pos.profit_target - current_price) / pos.entry_price * 100
                if current_price >= pos.profit_target:
                    position_status = "TARGET_REACHED"
        else:
            pnl_usd = (pos.entry_price - current_price) * pos.quantity
            pnl_pct = (pos.entry_price - current_price) / pos.entry_price * 100
            if pos.stop_loss:
                dist_stop = (pos.stop_loss - current_price) / pos.entry_price * 100
                if current_price >= pos.stop_loss:
                    position_status = "STOP_TRIGGERED"
            if pos.profit_target and position_status == "HOLD":
                dist_target = (current_price - pos.profit_target) / pos.entry_price * 100
                if current_price <= pos.profit_target:
                    position_status = "TARGET_REACHED"

    sig_action = sig_bias = None
    if sig:
        sig_action = sig.get("action")
        sig_bias = sig.get("bias_score")
        if position_status == "HOLD" and sig_action:
            if (pos.direction == "long" and sig_action == "short") or \
               (pos.direction == "short" and sig_action == "long"):
                position_status = "SIGNAL_REVERSED"

    return {
        "current_price": current_price,
        "unrealized_pnl_usd": round(pnl_usd, 2) if pnl_usd is not None else None,
        "unrealized_pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
        "distance_to_stop_pct": round(dist_stop, 2) if dist_stop is not None else None,
        "distance_to_target_pct": round(dist_target, 2) if dist_target is not None else None,
        "position_status": position_status,
        "latest_signal_action": sig_action,
        "latest_signal_bias": sig_bias,
    }


def _to_response(pos: Position, current_price: Optional[float] = None) -> PositionResponse:
    data = {c.name: getattr(pos, c.name) for c in pos.__table__.columns}
    data.update(_compute(pos, current_price=current_price))
    return PositionResponse(**data)


@router.get("/positions", response_model=List[PositionResponse])
def list_positions(db: Session = Depends(get_db)):
    positions = db.query(Position).filter(Position.status == "open").all()
    # 用 (asset, ticker) 对批量拉价格，key 是 asset
    pos_pairs = list({(p.asset, p.ticker or p.asset) for p in positions})
    prices = price_fetcher.get_prices_batch(pos_pairs)
    return [_to_response(p, current_price=prices.get(p.asset)) for p in positions]


@router.post("/positions/bulk-import")
def bulk_import_positions(body: List[PositionCreate], db: Session = Depends(get_db)):
    """批量导入持仓：同 ticker 已有开仓则跳过，否则新建。返回每条结果。"""
    results = []
    for item in body:
        existing = db.query(Position).filter(
            Position.ticker == item.ticker,
            Position.status == "open"
        ).first()
        if existing:
            results.append({"ticker": item.ticker, "asset": item.asset, "action": "skipped", "reason": "已有开仓"})
            continue
        pos = Position(**item.model_dump())
        pos.created_at = datetime.now().isoformat()
        pos.updated_at = datetime.now().isoformat()
        db.add(pos)
        results.append({"ticker": item.ticker, "asset": item.asset, "action": "created"})
    db.commit()
    sync.sync_to_json(db)
    created = sum(1 for r in results if r["action"] == "created")
    skipped = sum(1 for r in results if r["action"] == "skipped")
    return {"created": created, "skipped": skipped, "details": results}


@router.post("/positions", response_model=PositionResponse)
def create_position(body: PositionCreate, db: Session = Depends(get_db)):
    pos = Position(**body.model_dump())
    pos.created_at = datetime.now().isoformat()
    pos.updated_at = datetime.now().isoformat()
    db.add(pos)
    db.commit()
    db.refresh(pos)
    sync.sync_to_json(db)
    return _to_response(pos)


@router.put("/positions/{position_id}", response_model=PositionResponse)
def update_position(position_id: int, body: PositionUpdate, db: Session = Depends(get_db)):
    pos = db.query(Position).filter(Position.id == position_id).first()
    if not pos:
        raise HTTPException(status_code=404, detail="Position not found")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(pos, field, value)
    pos.updated_at = datetime.now().isoformat()
    db.commit()
    db.refresh(pos)
    sync.sync_to_json(db)
    return _to_response(pos)


@router.delete("/positions/{position_id}")
def delete_position(position_id: int, db: Session = Depends(get_db)):
    pos = db.query(Position).filter(Position.id == position_id).first()
    if not pos:
        raise HTTPException(status_code=404, detail="Position not found")
    db.delete(pos)
    db.commit()
    sync.sync_to_json(db)
    return {"success": True}


@router.post("/positions/{position_id}/close", response_model=TradeResponse)
def close_position(position_id: int, body: PositionClose, db: Session = Depends(get_db)):
    pos = db.query(Position).filter(Position.id == position_id).first()
    if not pos:
        raise HTTPException(status_code=404, detail="Position not found")

    if pos.direction == "long":
        pnl_usd = (body.exit_price - pos.entry_price) * pos.quantity
        pnl_pct = (body.exit_price - pos.entry_price) / pos.entry_price * 100
    else:
        pnl_usd = (pos.entry_price - body.exit_price) * pos.quantity
        pnl_pct = (pos.entry_price - body.exit_price) / pos.entry_price * 100

    try:
        d1 = date.fromisoformat(pos.entry_date[:10])
        d2 = date.fromisoformat(body.exit_date[:10])
        holding_days = (d2 - d1).days
    except Exception:
        holding_days = None

    trade = Trade(
        position_id=pos.id,
        asset=pos.asset,
        ticker=pos.ticker,
        direction=pos.direction,
        entry_price=pos.entry_price,
        entry_date=pos.entry_date,
        exit_price=body.exit_price,
        exit_date=body.exit_date,
        quantity=pos.quantity,
        cost_basis_usd=pos.cost_basis_usd,
        realized_pnl_usd=round(pnl_usd, 2),
        realized_pnl_pct=round(pnl_pct, 2),
        exit_reason=body.exit_reason,
        holding_days=holding_days,
        notes=body.notes,
        created_at=datetime.now().isoformat(),
    )
    pos.status = "closed"
    pos.updated_at = datetime.now().isoformat()
    db.add(trade)
    db.commit()
    db.refresh(trade)
    sync.sync_to_json(db)
    return TradeResponse.model_validate(trade)
