"""Verify the audit_logs hash chain (item 17).

Recomputes entry_hash from each row's fields and prev_hash, then reports the
first break. Works against a live database (default) or a JSONL export file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def _entry_hash(prev_hash: str, row: dict) -> str:
    payload = "|".join(
        [
            prev_hash,
            str(row["id"]),
            str(row["action"]),
            str(row.get("resource_type") or ""),
            str(row.get("resource_id") or ""),
            str(row.get("actor_user_id") or ""),
            str(row.get("workspace_id") or ""),
            str(row.get("outcome") or ""),
            _json_text(row.get("before")),
            _json_text(row.get("after")),
            str(row["created_at"]),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_text(value) -> str:
    if value is None:
        return ""
    # Match PostgreSQL `#>> '{}'` which flattens JSON to text without spaces.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def verify_rows(rows: list[dict]) -> list[str]:
    failures = []
    previous = "0" * 64
    for row in rows:
        expected_prev = previous
        actual_prev = row.get("prev_hash") or ""
        expected_hash = _entry_hash(expected_prev, row)
        actual_hash = row.get("entry_hash") or ""
        if actual_prev != expected_prev:
            failures.append(
                f"seq={row.get('chain_seq')} id={row['id']}: prev_hash mismatch"
            )
        if actual_hash != expected_hash:
            failures.append(
                f"seq={row.get('chain_seq')} id={row['id']}: entry_hash mismatch"
            )
        previous = actual_hash or expected_hash
    return failures


def _load_from_db() -> list[dict]:
    from sqlalchemy import select

    from app.db import SyncSessionLocal
    from app.models import AuditLog

    with SyncSessionLocal() as db:
        rows = list(
            db.execute(
                select(AuditLog).order_by(
                    AuditLog.chain_seq.nulls_last(), AuditLog.created_at
                )
            ).scalars()
        )
    return [
        {
            "id": r.id,
            "chain_seq": r.chain_seq,
            "prev_hash": r.prev_hash,
            "entry_hash": r.entry_hash,
            "action": r.action,
            "resource_type": r.resource_type,
            "resource_id": r.resource_id,
            "actor_user_id": r.actor_user_id,
            "workspace_id": r.workspace_id,
            "outcome": r.outcome,
            "before": r.before,
            "after": r.after,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
        if r.entry_hash  # skip rows created before hash-chain migration / auto_create
    ]


def _load_from_file(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", help="JSONL export; default = live database")
    args = parser.parse_args()
    rows = _load_from_file(Path(args.file)) if args.file else _load_from_db()
    if not rows:
        print("[PASS] no hash-chained audit rows to verify")
        return 0
    failures = verify_rows(rows)
    if failures:
        print("\n".join(f"[FAIL] {f}" for f in failures[:20]))
        return 1
    print(f"[PASS] audit hash chain intact ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
