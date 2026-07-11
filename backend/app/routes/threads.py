"""Thread / message / document APIs for the V2 product (Phase 43).

Every operation is scoped to the authenticated user (the ``get_current_user``
seam shared with the agent router) and verifies thread ownership before reading
messages or documents. Responses carry only SAFE fields — never storage keys,
raw object metadata, or document content.
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.routes.agent import get_current_user, resolve_user_id
from app.services import document_service, message_service, thread_service

router = APIRouter(prefix="/threads", tags=["threads"])


class CreateThreadRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class UpdateThreadRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


def _thread_view(thread: dict) -> dict:
    return {
        "thread_id": str(thread["_id"]),
        "title": thread.get("title") or "New conversation",
        "created_at": thread.get("created_at"),
        "updated_at": thread.get("updated_at"),
        "message_count": thread.get("message_count", 0),
    }


def _message_view(message: dict) -> dict:
    return {
        "seq": message.get("seq"),
        "role": message.get("role"),
        "content": message.get("content", ""),
        "created_at": message.get("created_at"),
    }


def _document_view(document: dict) -> dict:
    return {
        "document_id": str(document["_id"]),
        "filename": document.get("filename"),
        "status": document.get("status"),
        "page_count": document.get("page_count"),
        "created_at": document.get("created_at"),
    }


@router.post("")
async def create_thread(request: CreateThreadRequest, user=Depends(get_current_user)):
    user_id = resolve_user_id(user)
    title = (request.title or "New conversation").strip() or "New conversation"
    thread = await thread_service.create_thread(user_id, title)
    return _thread_view(thread)


@router.get("")
async def list_threads(user=Depends(get_current_user), limit: int = Query(50, ge=1, le=200)):
    user_id = resolve_user_id(user)
    threads = await thread_service.list_threads(user_id, limit=limit)
    return {"threads": [_thread_view(t) for t in threads]}


@router.get("/{thread_id}")
async def get_thread(thread_id: str, user=Depends(get_current_user)):
    user_id = resolve_user_id(user)
    thread = await thread_service.get_thread(user_id, thread_id)  # 404 if not owned
    return _thread_view(thread)


@router.patch("/{thread_id}")
async def rename_thread(thread_id: str, request: UpdateThreadRequest, user=Depends(get_current_user)):
    user_id = resolve_user_id(user)
    thread = await thread_service.update_thread_title(user_id, thread_id, request.title.strip())
    return _thread_view(thread)


@router.get("/{thread_id}/messages")
async def list_messages(
    thread_id: str, user=Depends(get_current_user), limit: int = Query(50, ge=1, le=200)
):
    user_id = resolve_user_id(user)
    await thread_service.get_thread(user_id, thread_id)  # ownership check (404 otherwise)
    messages = await message_service.get_recent_messages(
        user_id=user_id, thread_id=thread_id, limit=limit
    )
    return {"messages": [_message_view(m) for m in messages]}


@router.get("/{thread_id}/documents")
async def list_thread_documents(thread_id: str, user=Depends(get_current_user)):
    user_id = resolve_user_id(user)
    await thread_service.get_thread(user_id, thread_id)  # ownership check
    documents = await document_service.list_thread_documents(user_id, thread_id)
    return {"documents": [_document_view(d) for d in documents]}
