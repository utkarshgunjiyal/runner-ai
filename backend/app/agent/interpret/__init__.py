"""Request interpretation (Phase 43 — thread/document/connector integration).

Deterministic, config-free classification of a user request into *intent* and
*scope* (document + connector), plus extracted references (raw document mentions,
page numbers, required connectors) and a read/write action classification.

Intent and scope are SEPARATE axes. Interpretation makes NO ownership decisions
and performs NO retrieval — it only classifies. Ownership/resolution belongs to
the DocumentResolver; eligibility belongs to the connector layer. An LLM is never
used for interpretation here (deterministic evidence first).
"""

from app.agent.interpret.interpreter import (
    interpret_request,
    is_document_inventory_request,
)
from app.agent.interpret.models import (
    ActionType,
    ConnectorScope,
    DocumentScope,
    Intent,
    RequestInterpretation,
)

__all__ = [
    "interpret_request",
    "is_document_inventory_request",
    "ActionType",
    "ConnectorScope",
    "DocumentScope",
    "Intent",
    "RequestInterpretation",
]
