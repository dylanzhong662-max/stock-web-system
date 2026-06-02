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
from pnl import calc_pnl

router = APIRouter()


def _compute(pos: Position, current_price: Optional[float] = None) -> dict:
    sig = signal_reader.extract_signal_summary(pos.ticker or pos.asset) or \
          signal_reader.extract_signal_summary(pos.asset)

    pnl_usd = pnl_pct = dist_stop = dist_target = None
    position_status = "HOLD"

    if current_price:
        pnl_usd, pnl_pct = calc_pnl(pos.direction, pos.entry_price, current_price, pos.quantity)
        if pos.direction == "long":
            if pos.stop_loss:
                dist_stop = (current_price - pos.stop_loss) / pos.entry_price * 100
                if current_price <= pos.stop_loss:
                    position_status = "STOP_TRIGGERED"
            if pos.profit_target and position_status == "HOLD":
                dist_target = (pos.profit_target - current_price) / pos.entry_price * 100
                if current_price >= pos.profit_target:
                    position_status = "TARGET_1_REACHED"
            target_2 = getattr(pos, "profit_target_2", None)
            if target_2 and position_status == "HOLD" and current_price >= target_2:
                position_status = "TARGET_2_REACHED"
        else:
            if pos.stop_loss:
                dist_stop = (pos.stop_loss - current_price) / pos.entry_price * 100
                if current_price >= pos.stop_loss:
                    position_status = "STOP_TRIGGERED"
            if pos.profit_target and position_status == "HOLD":
                dist_target = (current_price - pos.profit_target) / pos.entry_price * 100
                if current_price <= pos.profit_target:
                    position_status = "TARGET_1_REACHED"
            target_2 = getattr(pos, "profit_target_2", None)
            if target_2 and position_status == "HOLD" and current_price <= target_2:
                position_status = "TARGET_2_REACHED"

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
    return [_to_response(p, current_price=p.current_price) for p in positions]


@router.get("/positions/tickers")
def list_tickers(db: Session = Depends(get_db)):
    """轻量接口：只返回当前持仓 ticker 列表，不拉价格。供 RAG 系统同步用。"""
    positions = db.query(Position).filter(Position.status == "open").all()
    return [{"asset": p.asset, "ticker": p.ticker} for p in positions]


@router.post("/positions/bulk-import")
def bulk_import_positions(body: List[PositionCreate], db: Session = Depends(get_db)):
    """批量导入持仓：同 ticker 已有开仓则更新 current_price，否则新建。返回每条结果。"""
    results = []
    for item in body:
        existing = db.query(Position).filter(
            Position.ticker == item.ticker,
            Position.status == "open"
        ).first()
        if existing:
            if item.current_price:
                existing.current_price = item.current_price
                existing.updated_at = datetime.now().isoformat()
                results.append({"ticker": item.ticker, "asset": item.asset, "action": "price_updated"})
            else:
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
    price_updated = sum(1 for r in results if r["action"] == "price_updated")
    return {"created": created, "skipped": skipped, "price_updated": price_updated, "details": results}


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

    pnl_usd, pnl_pct = calc_pnl(pos.direction, pos.entry_price, body.exit_price, pos.quantity)

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
        realized_pnl_usd=pnl_usd,
        realized_pnl_pct=pnl_pct,
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
