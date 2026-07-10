"""Provider-agnostic final-answer prompt model (Phase 16).

The Final Context Builder assembles a ``FinalPrompt`` from a RunContext — a
structured, curated view of everything the final LLM will need. It is
deliberately *not* rendered into any provider's wire format (no OpenAI /
Anthropic / Gemini message shape); a later provider adapter turns these typed
sections into whatever a specific model expects.

Structured sections, never one concatenated blob: each piece of context,
evidence, and tool output keeps its own provenance. See ARCHITECTURE.md §21.
"""

from pydantic import BaseModel, ConfigDict, Field


class ContextSection(BaseModel):
    """One prioritized working-context item retained for the final answer."""

    model_config = ConfigDict(frozen=True)

    source: str
    content: str
    score: float | None = None
    truncated: bool = False
    metadata: dict = Field(default_factory=dict)


class EvidenceSection(BaseModel):
    """One piece of grounding evidence, carrying a citation id + provenance."""

    model_config = ConfigDict(frozen=True)

    id: str  # citation marker, e.g. "E1"
    source: str
    content: str
    score: float | None = None
    truncated: bool = False
    metadata: dict = Field(default_factory=dict)


class ToolOutputSection(BaseModel):
    """One structured tool output, with the capability/step that produced it."""

    model_config = ConfigDict(frozen=True)

    id: str  # e.g. "T1"
    capability_id: str | None = None
    step_id: str | None = None
    output: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)


class Citation(BaseModel):
    """Provenance record for an evidence section, referenced by its id."""

    model_config = ConfigDict(frozen=True)

    id: str
    source: str
    score: float | None = None
    metadata: dict = Field(default_factory=dict)


class ExecutionSummary(BaseModel):
    """What the runtime did — path, status, tasks, and raw stage metadata."""

    model_config = ConfigDict(frozen=True)

    path: str | None = None
    status: str | None = None
    selected_capabilities: list[str] = Field(default_factory=list)
    completed_tasks: list[str] = Field(default_factory=list)
    failed_tasks: list[str] = Field(default_factory=list)
    partial_tasks: list[str] = Field(default_factory=list)
    execution_order: list[str] = Field(default_factory=list)
    tool_output_count: int = 0
    evidence_count: int = 0
    recovery_event_count: int = 0
    # Raw planner_runtime / direct_runtime metadata, preserved verbatim.
    details: dict = Field(default_factory=dict)


class FinalPrompt(BaseModel):
    """The complete, provider-agnostic final-answer context."""

    model_config = ConfigDict(frozen=True)

    system_prompt: str
    user_request: str
    context_sections: list[ContextSection] = Field(default_factory=list)
    evidence_sections: list[EvidenceSection] = Field(default_factory=list)
    tool_output_sections: list[ToolOutputSection] = Field(default_factory=list)
    execution_summary: ExecutionSummary
    final_instructions: str
    citations: list[Citation] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
