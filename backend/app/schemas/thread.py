from datetime import datetime

from pydantic import BaseModel, Field


class ThreadCreate(BaseModel):
    title: str = Field(..., min_length=1)


class ThreadPublic(BaseModel):
    id: str
    user_id: str
    title: str
    created_at: datetime
    updated_at: datetime
