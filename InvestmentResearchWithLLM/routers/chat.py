import json
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from orchestrator import Orchestrator

router = APIRouter()
_orchestrator = Orchestrator()


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


@router.post("/stream")
async def chat_stream(req: ChatRequest):
    async def event_stream():
        async for chunk in _orchestrator.stream(req.message, req.session_id):
            data = json.dumps({"text": chunk}, ensure_ascii=False)
            yield f"data: {data}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
