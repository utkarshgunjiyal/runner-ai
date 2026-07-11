"""Data models for request interpretation (Phase 43). Pydantic only — config-free."""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Intent(str, Enum):
    CONVERSATION_FOLLOWUP = "conversation_followup"
    THREAD_MEMORY_QA = "thread_memory_qa"
    # Deterministic document-inventory listing (Phase 46.1): "what documents are
    # uploaded?" / "list my files". Answered by listing the thread's own document
    # records — it must NOT trigger document-content retrieval.
    DOCUMENT_INVENTORY = "document_inventory"
    DOCUMENT_QA = "document_qa"
    DOCUMENT_SUMMARY = "document_summary"
    DOCUMENT_COMPARISON = "document_comparison"
    PAGE_QA = "page_qa"
    EXTERNAL_LOOKUP = "external_lookup"
    EXTERNAL_ACTION = "external_action"
    MIXED_REQUEST = "mixed_request"


class DocumentScope(str, Enum):
    NONE = "none"
    ALL_THREAD_DOCUMENTS = "all_thread_documents"
    SINGLE_DOCUMENT = "single_document"
    SELECTED_DOCUMENTS = "selected_documents"
    SPECIFIC_PAGE = "specific_page"
    UNRESOLVED_DOCUMENT = "unresolved_document"


class ConnectorScope(str, Enum):
    NONE = "none"
    GITHUB = "github"
    GMAIL = "gmail"
    CALENDAR = "calendar"
    MULTIPLE_CONNECTORS = "multiple_connectors"
    UNRESOLVED_CONNECTOR = "unresolved_connector"


class ActionType(str, Enum):
    READ = "read"
    WRITE = "write"


# Document scopes that involve a document but whose exact target must be resolved.
DOCUMENT_INTENTS = frozenset(
    {Intent.DOCUMENT_QA, Intent.DOCUMENT_SUMMARY, Intent.DOCUMENT_COMPARISON, Intent.PAGE_QA}
)


class RequestInterpretation(BaseModel):
    """The deterministic interpretation of one request. Safe to log (no content)."""

    model_config = ConfigDict(frozen=True)

    intents: list[Intent] = Field(default_factory=list)
    document_scope: DocumentScope = DocumentScope.NONE
    connector_scope: ConnectorScope = ConnectorScope.NONE

    raw_document_references: list[str] = Field(default_factory=list)
    selected_document_ids: list[str] = Field(default_factory=list)
    resolved_document_ids: list[str] = Field(default_factory=list)
    page_numbers: list[int] = Field(default_factory=list)
    required_connectors: list[str] = Field(default_factory=list)

    action_type: ActionType = ActionType.READ
    clarification_required: bool = False
    confidence: float = 1.0
    resolution_source: str = "deterministic"
    # True only when the user EXPLICITLY asks to persist a durable preference
    # (Phase 44 — gates the save_user_preference capability so casual chat and
    # persistence-test messages never trigger a preference write).
    preference_write: bool = False
    # True only when the request explicitly references a page (Phase 44 — gates
    # page-summary tooling so broad summaries don't route to page tools).
    page_explicit: bool = False

    @property
    def primary_intent(self) -> Intent:
        return self.intents[0] if self.intents else Intent.CONVERSATION_FOLLOWUP

    @property
    def needs_documents(self) -> bool:
        """True when the request depends on document evidence."""
        return (
            self.document_scope != DocumentScope.NONE
            or any(i in DOCUMENT_INTENTS for i in self.intents)
        )

    def safe_summary(self) -> dict:
        """Observability-safe summary (no request content, no document content)."""
        return {
            "intents": [i.value for i in self.intents],
            "document_scope": self.document_scope.value,
            "connector_scope": self.connector_scope.value,
            "raw_document_reference_count": len(self.raw_document_references),
            "selected_document_count": len(self.selected_document_ids),
            "page_numbers": list(self.page_numbers),
            "required_connectors": list(self.required_connectors),
            "action_type": self.action_type.value,
            "clarification_required": self.clarification_required,
            "confidence": self.confidence,
        }
