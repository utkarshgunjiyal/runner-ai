from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1)
    thread_id: str | None = None
    # Optional: scope document retrieval to a specific uploaded document.
    document_id: str | None = None


class ChatResponse(BaseModel):
    thread_id: str
    answer: str