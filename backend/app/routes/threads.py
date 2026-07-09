from fastapi import APIRouter

from app.schemas.thread import ThreadCreate, ThreadPublic
from app.services import thread_service
from app.services.thread_summary_service import create_empty_thread_summary

router = APIRouter(prefix="/threads", tags=["threads"])

# Single-user placeholder until auth lands; matches chat_service.
DEV_USER_ID = "dev_user"


def _thread_public(thread: dict) -> ThreadPublic:
    return ThreadPublic(
        id=str(thread["_id"]),
        user_id=thread["user_id"],
        title=thread["title"],
        created_at=thread["created_at"],
        updated_at=thread["updated_at"],
    )


@router.post("", response_model=ThreadPublic, status_code=201)
async def create_thread(payload: ThreadCreate) -> ThreadPublic:
    thread = await thread_service.create_thread(DEV_USER_ID, payload.title)
    # Match the chat flow: every thread gets an (empty) rolling summary doc.
    await create_empty_thread_summary(DEV_USER_ID, str(thread["_id"]))
    return _thread_public(thread)


@router.get("", response_model=list[ThreadPublic])
async def list_threads(limit: int = 100) -> list[ThreadPublic]:
    threads = await thread_service.list_threads(DEV_USER_ID, limit=limit)
    return [_thread_public(t) for t in threads]


@router.get("/{thread_id}", response_model=ThreadPublic)
async def get_thread(thread_id: str) -> ThreadPublic:
    thread = await thread_service.get_thread(DEV_USER_ID, thread_id)
    return _thread_public(thread)


@router.delete("/{thread_id}")
async def delete_thread(thread_id: str) -> dict:
    await thread_service.delete_thread(DEV_USER_ID, thread_id)
    return {"deleted": True, "thread_id": thread_id}
