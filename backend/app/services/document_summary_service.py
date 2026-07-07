"""Document summary generation.

Phase 1 ships a deterministic extractive stub. Phase 3 replaces the body of
``generate_document_summary`` with a real LLM call behind the same signature.
"""

from app.config import settings


async def generate_document_summary(pages: list[str]) -> str:
    full = "\n".join(page.strip() for page in pages if page and page.strip())
    snippet = full[: settings.summary_max_chars].strip()
    if not snippet:
        return ""
    ellipsis = "…" if len(full) > len(snippet) else ""
    return f"[Auto-extractive summary]\n{snippet}{ellipsis}"
