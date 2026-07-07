"""Character-based chunking with overlap, preserving page provenance."""

from app.config import settings


def chunk_pages(
    pages: list[str],
    chunk_size: int | None = None,
    overlap: int | None = None,
) -> list[dict]:
    """Split per-page text into overlapping chunks.

    Args:
        pages: per-page text (index 0 == page 1).
    Returns:
        list of {"text", "page", "chunk_index"} with a document-global,
        monotonically increasing chunk_index.
    """
    chunk_size = chunk_size or settings.chunk_size
    overlap = settings.chunk_overlap if overlap is None else overlap

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be >= 0 and < chunk_size")

    step = chunk_size - overlap
    chunks: list[dict] = []
    index = 0

    for page_number, raw in enumerate(pages, start=1):
        text = (raw or "").strip()
        if not text:
            continue

        start = 0
        while start < len(text):
            piece = text[start : start + chunk_size].strip()
            if piece:
                chunks.append(
                    {"text": piece, "page": page_number, "chunk_index": index}
                )
                index += 1
            start += step

    return chunks
