from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings

client = AsyncIOMotorClient(settings.mongo_url)
db = client[settings.db_name]

threads_collection = db["threads"]
messages_collection = db["messages"]
thread_summaries_collection = db["thread_summaries"]


async def ensure_indexes() -> None:
    """Create the indexes backing the app's hot query paths.

    Idempotent — MongoDB ignores ``create_index`` calls for indexes that
    already exist, so this is safe to run on every startup.
    """
    # Thread listing per user, newest first.
    await threads_collection.create_index([("user_id", 1), ("updated_at", -1)])

    # Recent-message and seq-range lookups; unique guards against duplicate
    # sequence numbers within a thread.
    await messages_collection.create_index(
        [("user_id", 1), ("thread_id", 1), ("seq", 1)],
        unique=True,
        name="uniq_user_thread_seq",
    )

    # One summary document per (user, thread).
    await thread_summaries_collection.create_index(
        [("user_id", 1), ("thread_id", 1)],
        unique=True,
        name="uniq_user_thread_summary",
    )
