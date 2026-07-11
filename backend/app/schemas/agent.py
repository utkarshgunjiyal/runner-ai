"""Agent API schemas (Phase 30).

Request/response models for POST /agent/run. Deliberately API-safe: the internal
RunContext and the full FinalPrompt are never exposed — only the final answer
text, the terminal runtime outcome, and (for waiting outcomes) the checkpoint id
and pending fields.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent.checkpoint.resume import ResumeKind


class AgentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_request: str = Field(min_length=1)
    thread_id: str | None = None
    # Phase 43: optional document-scope hints from the UI. These are HINTS only —
    # the backend revalidates every id against the thread's owned document set
    # (the client never asserts ownership). explicit_context_mode ∈
    # {"none","all","selected"} lets the UI force a scope.
    selected_document_ids: list[str] | None = None
    selected_page_numbers: list[int] | None = None
    explicit_context_mode: str | None = None
    metadata: dict | None = None

    @field_validator("user_request")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("user_request must be a non-empty string")
        return value

    @field_validator("explicit_context_mode")
    @classmethod
    def _valid_mode(cls, value):
        if value is None:
            return None
        normalized = str(value).strip().lower()
        if normalized not in {"none", "all", "all_thread_documents", "selected"}:
            raise ValueError("explicit_context_mode must be 'none', 'all', or 'selected'")
        return normalized

    def scope_metadata(self) -> dict:
        """Fold the safe scope hints into run metadata for the scope gate."""
        merged = dict(self.metadata or {})
        if self.selected_document_ids is not None:
            merged["selected_document_ids"] = list(self.selected_document_ids)
        if self.selected_page_numbers is not None:
            merged["selected_page_numbers"] = list(self.selected_page_numbers)
        if self.explicit_context_mode is not None:
            merged["explicit_context_mode"] = self.explicit_context_mode
        return merged


class ResolutionPayload(BaseModel):
    """The caller's resolution for a waiting run (maps to ResumeResolution)."""

    model_config = ConfigDict(extra="forbid")

    kind: ResumeKind
    value: Any = None
    reason: str = ""
    metadata: dict = Field(default_factory=dict)


class AgentResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checkpoint_id: str = Field(min_length=1)
    resolution: ResolutionPayload

    @field_validator("checkpoint_id")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("checkpoint_id must be a non-empty string")
        return value


class AgentRunResponse(BaseModel):
    run_id: str
    thread_id: str | None = None
    runtime_outcome: str
    answer: str | None = None
    checkpoint_id: str | None = None
    pending_action: str | None = None
    pending_reason: str | None = None
    metadata: dict = Field(default_factory=dict)
