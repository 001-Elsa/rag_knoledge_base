"""Shared fixtures for integration tests.

Celery/sync workers open separate DB connections, so per-test transaction
rollback is unreliable. Explicit table cleanup keeps outbox/dispatch tests
isolated from leftover pending events.
"""

import pytest
from sqlalchemy import delete

from app.db import SyncSessionLocal
from app.models import (
    Chunk,
    Conversation,
    DeadLetterTask,
    Document,
    KnowledgeBase,
    Message,
    Organization,
    OutboxEvent,
    UsageRecord,
    User,
    Workspace,
    WorkspaceMembership,
)


def _purge_business_tables(db) -> None:
    # Child → parent order to satisfy FK constraints without relying on CASCADE.
    db.execute(delete(DeadLetterTask))
    db.execute(delete(OutboxEvent))
    db.execute(delete(Chunk))
    db.execute(delete(Document))
    db.execute(delete(Message))
    db.execute(delete(Conversation))
    db.execute(delete(UsageRecord))
    db.execute(delete(KnowledgeBase))
    db.execute(delete(WorkspaceMembership))
    db.execute(delete(Workspace))
    db.execute(delete(Organization))
    db.execute(delete(User))
    db.commit()


@pytest.fixture(autouse=True)
def clean_integration_database():
    with SyncSessionLocal() as db:
        _purge_business_tables(db)

    yield

    with SyncSessionLocal() as db:
        db.execute(delete(DeadLetterTask))
        db.execute(delete(OutboxEvent))
        db.commit()
