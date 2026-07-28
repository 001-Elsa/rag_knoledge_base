"""Backfill SHA-256 hashes before migration 0004 makes content_hash NOT NULL.

Run this while API/worker writes are stopped:
    python scripts/backfill_document_hashes.py
"""

import hashlib
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SyncSessionLocal


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    with SyncSessionLocal() as db:
        documents = list(
            db.execute(
                text(
                    "SELECT id, filepath FROM documents "
                    "WHERE content_hash IS NULL FOR UPDATE"
                )
            ).mappings()
        )
        for document in documents:
            path = Path(document["filepath"])
            if not path.is_file():
                raise FileNotFoundError(
                    f"cannot hash document {document['id']}: legacy file is missing at {path}"
                )
            db.execute(
                text(
                    "UPDATE documents SET content_hash = :content_hash WHERE id = :id"
                ),
                {"content_hash": sha256_file(path), "id": document["id"]},
            )
        db.commit()
    print(f"backfilled {len(documents)} document hashes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
