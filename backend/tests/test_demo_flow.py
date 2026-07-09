"""V1.5 demo-completion tests: thread APIs + document Q&A.

Requires dev deps: pip install mongomock-motor httpx  (plus requirements.txt).
Mongo -> mongomock; Qdrant -> in-memory; LLM -> stub. No live infra.
"""
import asyncio
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ["LOG_LEVEL"] = "WARNING"

from mongomock_motor import AsyncMongoMockClient
import app.database as dbm

_mock_db = AsyncMongoMockClient()["runner_ai_v1"]
for name in ("threads", "messages", "thread_summaries", "documents", "jobs",
             "user_preferences", "knowledge"):
    setattr(dbm, f"{name}_collection", _mock_db[name])
dbm.db = _mock_db

from fastapi.testclient import TestClient
from qdrant_client import AsyncQdrantClient

import app.main as m
from app.config import settings
from app.services import (
    chat_service,
    embedding_service,
    thread_service,
    vector_store_service,
)
from app.services.thread_summary_service import create_empty_thread_summary


# --------------------------------------------------------------------------- #
# Thread APIs
# --------------------------------------------------------------------------- #

def test_thread_crud():
    with TestClient(m.app) as c:
        # create
        r = c.post("/threads", json={"title": "Resume Demo"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["user_id"] == "dev_user"
        assert body["title"] == "Resume Demo"
        assert body["id"] and body["created_at"] and body["updated_at"]
        thread_id = body["id"]

        # list
        r = c.get("/threads")
        assert r.status_code == 200
        assert any(t["id"] == thread_id for t in r.json())

        # get
        r = c.get(f"/threads/{thread_id}")
        assert r.status_code == 200 and r.json()["id"] == thread_id

        # get unknown -> 404
        assert c.get("/threads/000000000000000000000000").status_code == 404
        # get invalid id -> 400
        assert c.get("/threads/not-an-objectid").status_code == 400

        # delete
        r = c.delete(f"/threads/{thread_id}")
        assert r.status_code == 200 and r.json()["deleted"] is True
        assert c.get(f"/threads/{thread_id}").status_code == 404


def test_chat_ask_requires_valid_thread():
    with TestClient(m.app) as c:
        r = c.post("/chat/ask", json={"question": "hi", "thread_id": "000000000000000000000000"})
        assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Document Q&A (chat_service + in-memory Qdrant + stub LLM)
# --------------------------------------------------------------------------- #

def test_document_qa_returns_answer_and_evidence():
    settings.llm_provider = "stub"
    vector_store_service._client = AsyncQdrantClient(":memory:")

    async def run():
        provider = embedding_service.get_embedding_provider()
        chunks = [
            {"text": "Built a resume parser project using Python and FastAPI.", "page": 1, "chunk_index": 0},
            {"text": "Led the deployment demo project on Render and Railway.", "page": 1, "chunk_index": 1},
        ]
        vectors = await provider.embed([ch["text"] for ch in chunks])
        await vector_store_service.upsert_chunks("dev_user", "doc1", chunks, vectors)

        thread = await thread_service.create_thread("dev_user", "demo")
        await create_empty_thread_summary("dev_user", str(thread["_id"]))

        result = await chat_service.handle_chat(
            question="What projects are mentioned in this resume?",
            thread_id=str(thread["_id"]),
            document_id="doc1",
        )
        return result

    result = asyncio.get_event_loop().run_until_complete(run())
    assert result["answer"]
    assert result["evidence"], "expected document chunks as evidence"
    assert all(e["document_id"] == "doc1" for e in result["evidence"])


if __name__ == "__main__":
    test_thread_crud()
    test_chat_ask_requires_valid_thread()
    test_document_qa_returns_answer_and_evidence()
    print("ALL PASSED")
