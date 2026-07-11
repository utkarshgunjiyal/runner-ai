"""Phase 43 — deterministic request interpreter. Config-free (pydantic only)."""

from app.agent.interpret import (
    ActionType,
    ConnectorScope,
    DocumentScope,
    Intent,
    interpret_request,
)


def test_conversation_followup_no_documents():
    r = interpret_request("thanks, that helps")
    assert r.primary_intent == Intent.CONVERSATION_FOLLOWUP
    assert r.document_scope == DocumentScope.NONE
    assert not r.needs_documents


def test_thread_memory_question():
    r = interpret_request("what did we discuss earlier?")
    assert Intent.THREAD_MEMORY_QA in r.intents
    assert r.document_scope == DocumentScope.NONE


def test_ui_selected_single_document_wins():
    r = interpret_request("what does it say about pricing?", selected_document_ids=["d1"])
    assert r.document_scope == DocumentScope.SINGLE_DOCUMENT
    assert r.resolved_document_ids == ["d1"]
    assert r.resolution_source == "ui_selection"
    assert r.confidence == 1.0


def test_ui_selected_multiple_documents():
    r = interpret_request("compare them", selected_document_ids=["d1", "d2"])
    assert r.document_scope == DocumentScope.SELECTED_DOCUMENTS
    assert Intent.DOCUMENT_COMPARISON in r.intents


def test_vague_reference_is_unresolved_and_needs_clarification():
    r = interpret_request("summarize the report", has_thread_documents=True)
    assert r.document_scope == DocumentScope.UNRESOLVED_DOCUMENT
    assert r.clarification_required is True
    assert "the report" in r.raw_document_references


def test_page_reference_specific_page():
    r = interpret_request("what is on page 3 of the document?", has_thread_documents=True)
    assert 3 in r.page_numbers
    assert r.document_scope == DocumentScope.SPECIFIC_PAGE
    assert Intent.PAGE_QA in r.intents


def test_all_thread_documents_when_content_question_and_docs_present():
    r = interpret_request("what does the contract require for termination?", has_thread_documents=True)
    assert r.document_scope == DocumentScope.ALL_THREAD_DOCUMENTS
    assert r.needs_documents


def test_explicit_context_mode_none_disables_documents():
    r = interpret_request("what about the document?", selected_document_ids=[], explicit_context_mode="none")
    assert r.document_scope == DocumentScope.NONE


def test_connector_read_lookup():
    r = interpret_request("search my email for the invoice")
    assert r.connector_scope == ConnectorScope.GMAIL
    assert Intent.EXTERNAL_LOOKUP in r.intents
    assert r.action_type == ActionType.READ


def test_connector_write_action():
    r = interpret_request("send an email to the team")
    assert r.connector_scope == ConnectorScope.GMAIL
    assert r.action_type == ActionType.WRITE
    assert Intent.EXTERNAL_ACTION in r.intents


def test_multiple_connectors():
    r = interpret_request("check my calendar and email")
    assert r.connector_scope == ConnectorScope.MULTIPLE_CONNECTORS
    assert set(r.required_connectors) == {"calendar", "gmail"}


def test_filename_reference_extracted():
    r = interpret_request("what does report.pdf say?", has_thread_documents=True)
    assert any("report.pdf" in ref.lower() for ref in r.raw_document_references)


def test_preference_write_only_on_explicit_save_language():
    assert interpret_request("Remember that I prefer concise answers").preference_write is True
    assert interpret_request("From now on, use bullet points").preference_write is True
    assert interpret_request("Save this preference").preference_write is True
    # Casual / persistence-test / one-off statements must NOT be a preference write.
    assert interpret_request("This is my persistence test message.").preference_write is False
    assert interpret_request("my favorite color is blue").preference_write is False


def test_page_explicit_flag():
    assert interpret_request("what is on page 4?", has_thread_documents=True).page_explicit is True
    assert interpret_request("summarize this document", has_thread_documents=True).page_explicit is False


def test_safe_summary_has_no_request_content():
    r = interpret_request("secret question about the acquisition price", has_thread_documents=True)
    s = r.safe_summary()
    assert "acquisition" not in str(s)
    assert set(s).issuperset({"intents", "document_scope", "action_type"})
