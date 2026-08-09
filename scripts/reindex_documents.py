"""Queue a versioned reindex for existing documents after a RAG pipeline upgrade."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SyncSessionLocal  # noqa: E402
from app.models import DocStatus, Document, KnowledgeBase, OutboxEvent  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", help="Only reindex one workspace")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--include-failed", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be positive")

    statuses = [DocStatus.ready, DocStatus.failed] if args.include_failed else [DocStatus.ready]
    queued = 0
    last_id: str | None = None

    while True:
        with SyncSessionLocal() as db:
            stmt = (
                select(Document)
                .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
                .where(Document.status.in_(statuses))
                .order_by(Document.id)
                .limit(args.batch_size)
            )
            if args.workspace_id:
                stmt = stmt.where(KnowledgeBase.workspace_id == args.workspace_id)
            if last_id:
                stmt = stmt.where(Document.id > last_id)

            documents = list(db.scalars(stmt))
            if not documents:
                break

            for document in documents:
                target_version = (document.active_index_version or 0) + 1
                document.target_index_version = target_version
                document.status = DocStatus.queued
                document.stage = DocStatus.queued.value
                document.error = None
                db.add(
                    OutboxEvent(
                        aggregate_type="document",
                        aggregate_id=document.id,
                        event_type="document.ingest.requested",
                        payload={
                            "document_id": str(document.id),
                            "version": target_version,
                            "reason": "rag_pipeline_upgrade",
                        },
                        dedup_key=f"document.ingest.upgrade:{document.id}:{target_version}",
                    )
                )

            last_id = documents[-1].id
            queued += len(documents)
            db.commit()
            print(f"queued {queued} document(s)")

    print(f"done: queued {queued} document(s) for versioned reindex")


if __name__ == "__main__":
    main()
