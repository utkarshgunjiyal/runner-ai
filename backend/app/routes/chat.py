import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat_service import handle_chat, stream_chat

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/ask", response_model=ChatResponse)
async def ask(request: ChatRequest):
    return await handle_chat(
        question=request.question,
        thread_id=request.thread_id,
        document_id=request.document_id,
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("/stream")
async def stream(request: ChatRequest):
    async def event_source():
        async for evt in stream_chat(
            question=request.question,
            thread_id=request.thread_id,
            document_id=request.document_id,
        ):
            yield _sse(evt["event"], evt["data"])

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
