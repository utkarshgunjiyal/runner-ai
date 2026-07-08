"""Answer generation.

Preserves the ``generate_answer(context) -> str`` interface. Builds a grounded
prompt from the composed context (system prompt + priority-ordered evidence +
question) and delegates to the provider-agnostic llm_client.
"""

from app.services import llm_client

_DEFAULT_SYSTEM = (
    "You are Runner.ai, a context-aware AI assistant. Answer using the provided "
    "evidence in priority order. The current user message has highest priority. "
    "If the evidence is insufficient, say so plainly rather than inventing facts."
)


async def generate_answer(context: dict) -> str:
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

    return await llm_client.complete(system, prompt)
