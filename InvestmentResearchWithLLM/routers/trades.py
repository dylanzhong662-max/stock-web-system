"""交易分析 API — 上传嘉信 CSV → 解析 → 分析 → LLM 诊断"""
from fastapi import APIRouter, UploadFile, File, Query
from fastapi.responses import StreamingResponse
from typing import Optional
import json

import trade_analytics

router = APIRouter()

_last_analytics: dict | None = None
_last_round_trips: list | None = None


@router.post("/upload")
async def upload_trades(file: UploadFile = File(...)):
    """上传嘉信 CSV 文件，返回解析结果 + 量化分析"""
    global _last_analytics, _last_round_trips

    content = await file.read()
    text = content.decode("utf-8-sig")

    trades = trade_analytics.parse_schwab_csv(text)
    if not trades:
        return {"error": "No valid Buy/Sell trades found in CSV"}

    round_trips, open_positions = trade_analytics.match_round_trips(trades)
    analytics = trade_analytics.compute_analytics(round_trips, open_positions)

    _last_analytics = analytics
    _last_round_trips = round_trips

    return {
        "parsed_trades": len(trades),
        "round_trips": len(round_trips),
        "open_positions": len(open_positions),
        "analytics": analytics,
    }


@router.post("/review")
async def trade_review(model: Optional[str] = Query(None)):
    """基于最近一次上传的分析结果，生成 LLM 诊断报告"""
    if not _last_analytics:
        return {"error": "请先调用 /api/trades/upload 上传 CSV"}

    review = await trade_analytics.generate_review(_last_analytics, model=model)
    return {"review": review}


@router.post("/review/stream")
async def trade_review_stream(model: Optional[str] = Query(None)):
    """流式返回 LLM 交易诊断"""
    if not _last_analytics:
        return {"error": "请先调用 /api/trades/upload 上传 CSV"}

    from llm_client import resolve_model, stream_chat
    model = resolve_model(model)
    prompt = trade_analytics._build_review_prompt(_last_analytics)

    async def event_stream():
        async for text in stream_chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=6000,
            temperature=0.3,
        ):
            yield f"data: {json.dumps({'content': text})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
