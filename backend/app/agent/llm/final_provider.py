"""Final LLM Provider Boundary (Phase 17).

FinalContextBuilder produces a provider-agnostic ``FinalPrompt``. This module is
the clean seam that turns a ``FinalPrompt`` into a ``FinalAnswer`` without
binding the runtime to any specific LLM vendor:

    FinalPrompt → FinalAnswerProvider.generate → FinalAnswer
      → attach_final_answer → RunContext.metadata["final_answer"]

Three pieces:
1. ``FinalAnswer`` — the provider-neutral result model.
2. ``FinalAnswerProvider`` — the async protocol a real vendor adapter will
   implement later (OpenAI/Anthropic/Gemini live *outside* this repo boundary).
3. ``render_final_prompt`` — flattens the typed FinalPrompt sections into an
   ordered list of neutral ``FinalPromptMessage`` objects, the single place a
   vendor adapter converts to its own wire format.

Plus a ``DeterministicFinalProvider`` fake for end-to-end tests. No real LLM
calls, no vendor SDKs, no config, no database.
"""

import json
from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from app.agent.models.final_prompt import FinalPrompt
from app.agent.runtime.context import RunContext


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    CONTEXT = "context"
    EVIDENCE = "evidence"
    TOOL = "tool"
    INSTRUCTION = "instruction"


class FinalPromptMessage(BaseModel):
    """One neutral message in the rendered prompt (not a vendor message shape)."""

    model_config = ConfigDict(frozen=True)

    role: MessageRole
    content: str
    metadata: dict = Field(default_factory=dict)


class FinalAnswer(BaseModel):
    """Provider-neutral result of a final generation."""

    model_config = ConfigDict(frozen=True)

    text: str
    used_citations: list[str] = Field(default_factory=list)
    usage_metadata: dict = Field(default_factory=dict)
    provider: str = ""
    model: str = ""
    finish_reason: str = "stop"
    metadata: dict = Field(default_factory=dict)


@runtime_checkable
class FinalAnswerProvider(Protocol):
    """The boundary a concrete LLM adapter implements. Async by design."""

    provider: str
    model: str

    async def generate(self, final_prompt: FinalPrompt) -> FinalAnswer:
        ...


# --------------------------------------------------------------------------- #
# Neutral renderer
# --------------------------------------------------------------------------- #

def _tool_content(section) -> str:
    label = section.capability_id or "tool"
    return f"{label} -> {json.dumps(section.output, sort_keys=True)}"


def render_final_prompt(final_prompt: FinalPrompt) -> list[FinalPromptMessage]:
    """Flatten a FinalPrompt into ordered, provider-neutral messages.

    Order: system → context → evidence → tool → user request → final
    instructions. Within each section list the original order is preserved, so a
    vendor adapter can map roles however it needs without losing sequence.
    """

    messages: list[FinalPromptMessage] = [
        FinalPromptMessage(role=MessageRole.SYSTEM, content=final_prompt.system_prompt)
    ]

    for section in final_prompt.context_sections:
        messages.append(
            FinalPromptMessage(
                role=MessageRole.CONTEXT,
                content=section.content,
                metadata={"source": section.source, "score": section.score},
            )
        )

    for section in final_prompt.evidence_sections:
        messages.append(
            FinalPromptMessage(
                role=MessageRole.EVIDENCE,
                content=f"[{section.id}] {section.content}",
                metadata={"id": section.id, "source": section.source, "score": section.score},
            )
        )

    for section in final_prompt.tool_output_sections:
        messages.append(
            FinalPromptMessage(
                role=MessageRole.TOOL,
                content=_tool_content(section),
                metadata={
                    "id": section.id,
                    "capability_id": section.capability_id,
                    "step_id": section.step_id,
                },
            )
        )

    messages.append(
        FinalPromptMessage(role=MessageRole.USER, content=final_prompt.user_request)
    )
    messages.append(
        FinalPromptMessage(
            role=MessageRole.INSTRUCTION, content=final_prompt.final_instructions
        )
    )
    return messages


# --------------------------------------------------------------------------- #
# Deterministic fake provider (tests / offline runs)
# --------------------------------------------------------------------------- #

class DeterministicFinalProvider:
    """A grounded, fully deterministic FinalAnswerProvider for tests.

    Composes an answer from the user request plus the top evidence/tool output,
    echoes the FinalPrompt's citations, and reports deterministic usage counts.
    No randomness, no clock, no network.
    """

    def __init__(self, *, provider: str = "deterministic", model: str = "fake-final-1") -> None:
        self.provider = provider
        self.model = model

    async def generate(self, final_prompt: FinalPrompt) -> FinalAnswer:
        parts = [
            f"Based on the available context, here is the answer to: "
            f"{final_prompt.user_request}"
        ]
        if final_prompt.evidence_sections:
            top = final_prompt.evidence_sections[0]
            parts.append(f"Supporting evidence [{top.id}]: {top.content}")
        elif final_prompt.tool_output_sections:
            top = final_prompt.tool_output_sections[0]
            parts.append(f"Tool result: {_tool_content(top)}")

        citation_ids = [c.id for c in final_prompt.citations]
        if citation_ids:
            parts.append("Citations: " + ", ".join(citation_ids))

        text = " ".join(parts)
        return FinalAnswer(
            text=text,
            used_citations=citation_ids,
            usage_metadata=self._usage(final_prompt, text),
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            metadata={
                "grounded": True,
                "evidence_used": len(final_prompt.evidence_sections),
                "tool_outputs_used": len(final_prompt.tool_output_sections),
            },
        )

    @staticmethod
    def _usage(final_prompt: FinalPrompt, text: str) -> dict:
        prompt_chars = (
            len(final_prompt.system_prompt)
            + len(final_prompt.user_request)
            + len(final_prompt.final_instructions)
            + sum(len(s.content) for s in final_prompt.context_sections)
            + sum(len(s.content) for s in final_prompt.evidence_sections)
        )
        completion_chars = len(text)
        return {
            "prompt_chars": prompt_chars,
            "completion_chars": completion_chars,
            "prompt_tokens": prompt_chars // 4,
            "completion_tokens": completion_chars // 4,
            "total_tokens": (prompt_chars + completion_chars) // 4,
        }


# --------------------------------------------------------------------------- #
# RunContext integration
# --------------------------------------------------------------------------- #

def attach_final_answer(run_context: RunContext, final_answer: FinalAnswer) -> RunContext:
    """Record the final answer on ``RunContext.metadata['final_answer']``.

    Append-only metadata write; the working context is never touched.
    """

    run_context.metadata["final_answer"] = {
        "text": final_answer.text,
        "used_citations": list(final_answer.used_citations),
        "provider": final_answer.provider,
        "model": final_answer.model,
        "finish_reason": final_answer.finish_reason,
        "usage_metadata": dict(final_answer.usage_metadata),
        "metadata": dict(final_answer.metadata),
    }
    return run_context
