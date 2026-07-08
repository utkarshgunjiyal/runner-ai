from fastapi import APIRouter
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat_service import handle_chat

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/ask", response_model=ChatResponse)
async def ask(request: ChatRequest):
    return await handle_chat(
        question=request.question,
        thread_id=request.thread_id,
        document_id=request.document_id,
    )