"""PDF text extraction (synchronous — call via a thread from async code)."""

import io

from pypdf import PdfReader


def extract_pages(data: bytes) -> list[str]:
    """Return per-page extracted text. Index 0 corresponds to page 1."""
    reader = PdfReader(io.BytesIO(data))
    return [(page.extract_text() or "") for page in reader.pages]
