"""Answer generation.

Preserves the ``generate_answer(context) -> str`` interface and adds a
streaming variant. Both build the same grounded prompt (system prompt +
priority-ordered evidence + question) via ``build_prompt``.
"""

from typing import AsyncIterator

from app.services import llm_client

_DEFAULT_SYSTEM = (
    "You are Runner.ai, a context-aware AI assistant. Answer using the provided "
    "evidence in priority order. The current user message has highest priority. "
    "If the evidence is insufficient, say so plainly rather than inventing facts."
)


def build_prompt(context: dict) -> tuple[str, str]:
    system = context.get("system_prompt") or _DEFAULT_SYSTEM
    question = context["question"]
    evidence = context.get("evidence", [])

    if evidence:
        evidence_block = "\n\n".join(evidence)
        prompt = (
            "Use the following context to answer the user's question. "
            "Cite pages/sources where relevant; if the context does not contain "
            "the answer, say so.\n\n"
            f"=== CONTEXT ===\n{evidence_block}\n\n"
            f"=== QUESTION ===\n{question}"
        )
    else:
        prompt = question

    return system, prompt


async def generate_answer(context: dict) -> str:
    system, prompt = build_prompt(context)
    return await llm_client.complete(system, prompt)


async def stream_answer(context: dict) -> AsyncIterator[str]:
    system, prompt = build_prompt(context)
    async for chunk in llm_client.stream(system, prompt):
        yield chunk
