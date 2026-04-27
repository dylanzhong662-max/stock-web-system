import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from sqlalchemy.orm import Session
from typing import List
import asyncio

from database import get_db
from models import Position
from schemas import ScreenshotParseResponse, AdviceRequest, AdviceResponse, ParsedPosition
import image_parser
import advisor as advisor_module
import signal_reader
import price_fetcher
from routers.portfolio import _compute

router = APIRouter()

ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_FILES = 10


@router.post("/parse-screenshot", response_model=ScreenshotParseResponse)
async def parse_screenshot(files: List[UploadFile] = File(...)):
    """上传一张或多张持仓截图（最多10张），使用 Qwen-VL 并发解析，合并去重后返回"""
    if len(files) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"最多上传 {MAX_FILES} 张图片")

    # 读取所有文件内容
    file_data = []
    for f in files:
        content_type = f.content_type or "image/jpeg"
        if content_type not in ALLOWED_MIME:
            raise HTTPException(status_code=400, detail=f"不支持的图片格式: {content_type}（文件: {f.filename}）")
        image_bytes = await f.read()
        if len(image_bytes) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"图片过大（限 10MB）：{f.filename}")
        file_data.append((image_bytes, content_type))

    # 并发调用 Qwen-VL
    loop = asyncio.get_event_loop()
    tasks = [
        loop.run_in_executor(None, image_parser.parse_image_to_positions, img, mime)
        for img, mime in file_data
    ]
    results = await asyncio.gather(*tasks)

    # 合并所有图片的解析结果，按 ticker 去重（后出现的覆盖前面的）
    merged: dict[str, dict] = {}
    all_errors = []
    raw_texts = []
    any_success = False

    for r in results:
        if r["success"]:
            any_success = True
        if r.get("error"):
            all_errors.append(r["error"])
        if r.get("raw_ocr"):
            raw_texts.append(r["raw_ocr"])
        for p in r.get("positions", []):
            key = (p.get("ticker") or p.get("asset") or "").upper()
            if key:
                merged[key] = p  # 同一 ticker 后张图覆盖前张图（通常后张更新）

    positions = [ParsedPosition(**p) for p in merged.values()]
    # 把截图里的现价持久化为兜底价格（Yahoo Finance 在国内不可用时使用）
    for pos in positions:
        if pos.asset and pos.current_price:
            price_fetcher.set_fallback_price(pos.asset, pos.current_price)
        if pos.ticker and pos.current_price:
            price_fetcher.set_fallback_price(pos.ticker, pos.current_price)
    return ScreenshotParseResponse(
        success=any_success,
        positions=positions,
        raw_ocr="\n---\n".join(raw_texts) if raw_texts else None,
        error="; ".join(all_errors) if all_errors and not any_success else None,
    )


@router.post("/advice", response_model=AdviceResponse)
def get_advice(body: AdviceRequest):
    """基于持仓列表 + LLM 信号，生成调仓建议（DeepSeek R1）"""
    positions_data = [p.model_dump(exclude_none=True) for p in body.positions]
    if not positions_data:
        raise HTTPException(status_code=400, detail="持仓列表不能为空")

    signals = {}
    if body.include_signals:
        signals = signal_reader.read_all_signals()

    result = advisor_module.generate_advice(positions_data, signals, model_override=body.model)
    return AdviceResponse(
        summary=result.get("summary", ""),
        recommendations=result.get("recommendations", []),
        new_opportunities=result.get("new_opportunities", []),
        risk_notes=result.get("risk_notes", []),
        raw_thinking=result.get("raw_thinking"),
    )


@router.post("/advice-from-db", response_model=AdviceResponse)
def get_advice_from_db(db: Session = Depends(get_db)):
    """直接读取数据库中的开仓持仓，生成调仓建议（无需上传截图）"""
    positions = db.query(Position).filter(Position.status == "open").all()
    if not positions:
        raise HTTPException(status_code=404, detail="当前无开仓持仓")

    # 批量拉实时价格
    pos_pairs = list({(p.asset, p.ticker or p.asset) for p in positions})
    prices = price_fetcher.get_prices_batch(pos_pairs)

    positions_data = []
    for p in positions:
        current_price = prices.get(p.asset)
        computed = _compute(p, current_price=current_price)
        positions_data.append({
            "asset": p.asset,
            "ticker": p.ticker,
            "direction": p.direction,
            "quantity": p.quantity,
            "entry_price": p.entry_price,
            "current_price": computed.get("current_price"),
            "unrealized_pnl_pct": computed.get("unrealized_pnl_pct"),
            "unrealized_pnl_usd": computed.get("unrealized_pnl_usd"),
            "cost_basis": p.cost_basis_usd,
            "stop_loss": p.stop_loss,
            "profit_target": p.profit_target,
            "notes": p.notes,
        })

    signals = signal_reader.read_all_signals()
    result = advisor_module.generate_advice(positions_data, signals)
    return AdviceResponse(
        summary=result.get("summary", ""),
        recommendations=result.get("recommendations", []),
        new_opportunities=result.get("new_opportunities", []),
        risk_notes=result.get("risk_notes", []),
        raw_thinking=result.get("raw_thinking"),
    )
