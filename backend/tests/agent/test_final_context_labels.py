"""Phase 44 — source-labelled evidence + comparison instructions (defects 3, 4)."""

from app.agent.context.final_builder import FinalContextBuilder
from app.agent.llm.final_provider import render_final_prompt
from app.agent.runtime.context import EvidenceItem, RunContext


def _ctx_with_evidence(evidence, metadata=None):
    rc = RunContext.create("compare the two documents", user_id="u", metadata=metadata or {})
    for e in evidence:
        rc.append_evidence(e)
    return rc


def test_evidence_renders_document_and_page_labels():
    rc = _ctx_with_evidence([
        EvidenceItem(source="document:resume.pdf", content="Python and FastAPI.",
                     score=0.9, metadata={"filename": "resume.pdf", "page": 1, "document_id": "d1"}),
    ])
    prompt = FinalContextBuilder().build(rc)
    messages = render_final_prompt(prompt)
    evidence_text = " ".join(m.content for m in messages if m.role.value == "evidence")
    assert "[DOCUMENT: resume.pdf]" in evidence_text
    assert "[PAGE: 1]" in evidence_text


def test_comparison_instructions_added_for_multiple_documents():
    rc = _ctx_with_evidence([
        EvidenceItem(source="document:a.pdf", content="A skills", score=0.9,
                     metadata={"filename": "a.pdf", "page": 1, "document_id": "A"}),
        EvidenceItem(source="document:b.pdf", content="B skills", score=0.8,
                     metadata={"filename": "b.pdf", "page": 2, "document_id": "B"}),
    ])
    prompt = FinalContextBuilder().build(rc)
    instr = prompt.final_instructions.lower()
    assert "similarities" in instr and "differences" in instr
    assert "do not merge" in instr


def test_single_document_has_no_comparison_boilerplate():
    rc = _ctx_with_evidence([
        EvidenceItem(source="document:a.pdf", content="only A", score=0.9,
                     metadata={"filename": "a.pdf", "page": 1, "document_id": "A"}),
    ])
    prompt = FinalContextBuilder().build(rc)
    assert "similarities" not in prompt.final_instructions.lower()
