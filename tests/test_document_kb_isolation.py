"""Regression coverage for immediate upload display and knowledge-base isolation."""

from pathlib import Path

from app.models import Document
from app.services.notify import document_event

ROOT = Path(__file__).resolve().parents[1]


def test_document_identity_is_unique_inside_each_knowledge_base():
    constraints = {constraint.name for constraint in Document.__table__.constraints}
    assert "uq_documents_owner_kb_content_hash" in constraints


def test_document_notifications_always_identify_the_knowledge_base():
    event = document_event("doc-1", "ready", kb_id="kb-1")
    assert event["document_id"] == "doc-1"
    assert event["kb_id"] == "kb-1"


def test_frontend_shows_upload_immediately_and_guards_async_kb_switches():
    page = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert "const pendingDocument" in page
    assert "this.docs.unshift(pendingDocument)" in page
    assert "const uploadKbId = this.currentKb" in page
    assert "this.currentKb === uploadKbId" in page
    assert "this.docsLoadVersion === loadVersion" in page
    assert "rows.filter(document => document.kb_id === id)" in page
    assert "msg.kb_id !== this.currentKb" in page


def test_retrieval_requires_chunk_and_parent_document_to_share_a_kb():
    source = (ROOT / "app" / "services" / "retriever.py").read_text(encoding="utf-8")
    assert "Chunk.kb_id == Document.kb_id" in source
