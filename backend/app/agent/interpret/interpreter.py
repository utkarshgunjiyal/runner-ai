"""Deterministic request interpreter (Phase 43).

Classifies a request into intent + document/connector scope using keyword/heuristic
evidence only. No LLM, no database, no settings, no ownership decisions.

Design: explicit UI signals win over text heuristics. `selected_document_ids`
(revalidated later by the resolver) and `explicit_context_mode` from the UI take
precedence; otherwise text is scanned for page references, document-reference
phrases, summary/comparison cues, and connector cues.
"""

import re

from app.agent.interpret.models import (
    ActionType,
    ConnectorScope,
    DocumentScope,
    Intent,
    RequestInterpretation,
)

# Document reference cues — vague ("this document") or typed ("the pdf").
_DOC_REFERENCE_PHRASES = (
    "this document", "that document", "the document", "this file", "that file",
    "the file", "this pdf", "the pdf", "this report", "the report", "the doc",
    "this doc", "attached", "the attachment", "uploaded",
)
_SUMMARY_CUES = ("summarize", "summary", "summarise", "overview", "tl;dr", "recap of the")
_COMPARISON_CUES = ("compare", "comparison", "versus", " vs ", "difference between", "differences between")
_DOC_CONTENT_CUES = (
    "document", "documents", "pdf", "report", "file", "chapter", "section",
    "clause", "paragraph", "policy", "contract", "invoice",
)
_MEMORY_CUES = (
    "you said", "we discussed", "earlier", "before", "previously", "what did i ask",
    "what did we", "recap", "so far", "last time", "my last question",
)
_FOLLOWUP_CUES = ("thanks", "thank you", "ok", "okay", "got it", "hello", "hi ", "hey")

_CONNECTOR_CUES = {
    ConnectorScope.GMAIL: ("email", "gmail", "inbox", "e-mail", "mail "),
    ConnectorScope.GITHUB: ("github", "repository", "repo", "pull request", " pr ", "issue", "commit", "branch"),
    ConnectorScope.CALENDAR: ("calendar", "schedule", "meeting", "invite", "availability", "appointment"),
}
_WRITE_CUES = (
    "send", "create", "delete", "remove", "merge", "close", "open a", "add ",
    "schedule", "book", "draft", "reply", "post", "update", "archive", "cancel",
)

_PAGE_RE = re.compile(r"\bpage[s]?\s+(\d{1,4})(?:\s*(?:-|to|and|,)\s*(\d{1,4}))?", re.IGNORECASE)

# --- Document inventory intent (Phase 46.1) --------------------------------
# Deterministic detection of "what documents are uploaded?" style questions —
# a LISTING of the thread's own documents, NOT a content query. These must route
# to the deterministic inventory handler and never trigger document retrieval.
_INVENTORY_SUBJECT = r"(?:documents?|files?|pdfs?|docs?)"
_INVENTORY_PATTERNS = (
    # "how many documents/files ..."
    re.compile(r"\bhow many\s+" + _INVENTORY_SUBJECT + r"\b", re.IGNORECASE),
    # "list / show ... documents/files"
    re.compile(r"\b(?:list|show)\b[^?.!]*\b" + _INVENTORY_SUBJECT + r"\b", re.IGNORECASE),
    # "what/which documents ... uploaded/attached/available/(do) i have/in this thread"
    re.compile(
        r"\b(?:what|which)\b[^?.!]*\b" + _INVENTORY_SUBJECT + r"\b[^?.!]*\b"
        r"(?:uploaded|attached|available|do i have|i have|are there|"
        r"in (?:this|the) (?:thread|conversation|chat))",
        re.IGNORECASE,
    ),
    # "do I have any documents ..."
    re.compile(r"\bdo i have\b[^?.!]*\b" + _INVENTORY_SUBJECT + r"\b", re.IGNORECASE),
    # "... documents (are) uploaded/attached/available"
    re.compile(
        r"\b" + _INVENTORY_SUBJECT + r"\b[^?.!]*\b(?:uploaded|attached|available)\b",
        re.IGNORECASE,
    ),
)
# If any of these appear, the request is a content/action request about documents,
# NOT an inventory listing — so it must not be classified as inventory.
_INVENTORY_NEGATIVES = (
    "summarize", "summarise", "summary", "compare", "comparison", "versus", " vs ",
    "delete", "remove", "upload ", "select", "open ", "say", "says", "about",
    "search", "find", "inside", "content of", "tell me about", "explain",
)


def is_document_inventory_request(user_request: str) -> bool:
    """Deterministically decide whether a request is a document-INVENTORY listing
    (list/count the thread's documents), as opposed to a document-content request.

    Language-robust over ordinary English phrasings; no LLM, no model call. A
    content/action cue (summarize/compare/search/delete/upload/select/"say"/…)
    disqualifies it, so content and management requests never misroute here."""
    text = f" {(user_request or '').lower().strip()} "
    if any(neg in text for neg in _INVENTORY_NEGATIVES):
        return False
    return any(pattern.search(text) for pattern in _INVENTORY_PATTERNS)

# Explicit, durable preference-save intent only (Phase 44). Casual statements,
# persistence-test messages, and one-off facts must NOT match.
_PREFERENCE_CUES = (
    "remember that", "remember to", "remember i", "remember my", "please remember",
    "save this preference", "save my preference", "save this as a preference",
    "from now on", "going forward always", "always use", "always answer",
    "note that i prefer", "set my preference",
)


def _is_preference_write(text: str) -> bool:
    return any(cue in text for cue in _PREFERENCE_CUES)


def _contains_any(text: str, needles) -> bool:
    return any(n in text for n in needles)


def _extract_pages(text: str) -> list[int]:
    pages: list[int] = []
    for match in _PAGE_RE.finditer(text):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        lo, hi = (start, end) if start <= end else (end, start)
        for p in range(lo, min(hi, lo + 50) + 1):  # bound the range defensively
            if p not in pages:
                pages.append(p)
    return pages


def _extract_doc_references(text: str) -> list[str]:
    refs = [phrase for phrase in _DOC_REFERENCE_PHRASES if phrase in text]
    # Quoted filename-like tokens (e.g. "report.pdf", 'q3 report').
    for quoted in re.findall(r"[\"']([^\"']{2,80})[\"']", text):
        refs.append(quoted.strip())
    # Bare filename tokens (word.ext).
    for token in re.findall(r"\b[\w.-]+\.(?:pdf|docx?|txt|md|csv|xlsx?)\b", text, re.IGNORECASE):
        refs.append(token)
    # De-dup preserving order.
    seen, out = set(), []
    for r in refs:
        key = r.lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _connector_scope(text: str) -> tuple[ConnectorScope, list[str]]:
    matched = [scope for scope, cues in _CONNECTOR_CUES.items() if _contains_any(text, cues)]
    if not matched:
        return ConnectorScope.NONE, []
    providers = [s.value for s in matched]
    if len(matched) > 1:
        return ConnectorScope.MULTIPLE_CONNECTORS, providers
    return matched[0], providers


def interpret_request(
    user_request: str,
    *,
    selected_document_ids: list[str] | None = None,
    page_numbers: list[int] | None = None,
    has_thread_documents: bool = False,
    explicit_context_mode: str | None = None,
) -> RequestInterpretation:
    """Interpret a request deterministically. `selected_document_ids` are UI hints
    (revalidated by the resolver, never trusted as authorization here)."""
    text = f" {(user_request or '').lower().strip()} "
    selected = list(selected_document_ids or [])

    # Document-inventory listing (Phase 46.1): a deterministic LISTING request,
    # not a content query. Classify it with NO document scope so downstream never
    # resolves or retrieves document chunks. An explicit UI document selection is a
    # deliberate content scope and takes precedence over the text heuristic.
    if not selected and is_document_inventory_request(user_request):
        return RequestInterpretation(
            intents=[Intent.DOCUMENT_INVENTORY],
            document_scope=DocumentScope.NONE,
            confidence=1.0,
            resolution_source="deterministic",
        )

    text_pages = _extract_pages(text)
    page_explicit = bool(text_pages)
    pages = list(page_numbers or []) or text_pages
    doc_refs = _extract_doc_references(text)
    preference_write = _is_preference_write(text)

    connector_scope, required_connectors = _connector_scope(text)
    is_write = _contains_any(text, _WRITE_CUES) and connector_scope != ConnectorScope.NONE
    action_type = ActionType.WRITE if is_write else ActionType.READ

    intents: list[Intent] = []
    document_scope = DocumentScope.NONE
    clarification_required = False
    resolution_source = "deterministic"

    # --- Document scope (explicit UI signal wins) ------------------------------
    mode = (explicit_context_mode or "").strip().lower()
    if mode == "none":
        document_scope = DocumentScope.NONE
    elif selected:
        document_scope = DocumentScope.SINGLE_DOCUMENT if len(selected) == 1 else DocumentScope.SELECTED_DOCUMENTS
        resolution_source = "ui_selection"
    elif mode in {"all", "all_thread_documents"} and has_thread_documents:
        document_scope = DocumentScope.ALL_THREAD_DOCUMENTS
    elif pages and (doc_refs or has_thread_documents):
        document_scope = DocumentScope.SPECIFIC_PAGE
    elif doc_refs:
        # A document is referenced but not resolved yet → the resolver decides.
        document_scope = DocumentScope.UNRESOLVED_DOCUMENT
        clarification_required = True
    elif has_thread_documents and _contains_any(text, _DOC_CONTENT_CUES):
        document_scope = DocumentScope.ALL_THREAD_DOCUMENTS

    # --- Intent ----------------------------------------------------------------
    if _contains_any(text, _COMPARISON_CUES) and (len(selected) > 1 or has_thread_documents):
        intents.append(Intent.DOCUMENT_COMPARISON)
    if _contains_any(text, _SUMMARY_CUES) and document_scope != DocumentScope.NONE:
        intents.append(Intent.DOCUMENT_SUMMARY)
    if document_scope == DocumentScope.SPECIFIC_PAGE:
        intents.append(Intent.PAGE_QA)
    if document_scope in (
        DocumentScope.SINGLE_DOCUMENT, DocumentScope.SELECTED_DOCUMENTS,
        DocumentScope.ALL_THREAD_DOCUMENTS, DocumentScope.UNRESOLVED_DOCUMENT,
    ) and not intents:
        intents.append(Intent.DOCUMENT_QA)

    if connector_scope != ConnectorScope.NONE:
        intents.append(Intent.EXTERNAL_ACTION if is_write else Intent.EXTERNAL_LOOKUP)

    if not intents:
        if _contains_any(text, _MEMORY_CUES):
            intents.append(Intent.THREAD_MEMORY_QA)
        else:
            intents.append(Intent.CONVERSATION_FOLLOWUP)

    if len(intents) > 1:
        intents = [Intent.MIXED_REQUEST, *intents]

    confidence = 0.6 if document_scope == DocumentScope.UNRESOLVED_DOCUMENT else 0.9
    if selected or mode:
        confidence = 1.0

    return RequestInterpretation(
        intents=intents,
        document_scope=document_scope,
        connector_scope=connector_scope,
        raw_document_references=doc_refs,
        selected_document_ids=selected,
        resolved_document_ids=list(selected),  # UI selection is provisionally resolved
        page_numbers=pages,
        required_connectors=required_connectors,
        action_type=action_type,
        clarification_required=clarification_required,
        confidence=confidence,
        resolution_source=resolution_source,
        preference_write=preference_write,
        page_explicit=page_explicit or bool(page_numbers),
    )
