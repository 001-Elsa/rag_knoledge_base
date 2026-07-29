"""Seed an eval tenant with fixtures and a known login for HTTP eval scripts."""

from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func

from app.config import settings
from app.db import AsyncSessionLocal
from app.models import (
    Chunk,
    DocStatus,
    Document,
    KnowledgeBase,
    Organization,
    User,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
)
from app.security import hash_password
from app.services.chunker import split_text
from app.services.embedder import embed_documents
from app.services.parser import parse_file


def _tokenize(text: str) -> str:
    import jieba

    return " ".join(t for t in jieba.cut_for_search(text) if t.strip())


async def main() -> int:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    username = f"eval_{uuid.uuid4().hex[:8]}"
    password = "EvalPass123!"
    fixtures = Path("eval/fixtures")

    async with AsyncSessionLocal() as db:
        owner = User(username=username, password_hash=hash_password(password))
        db.add(owner)
        await db.flush()
        org = Organization(name=f"{username}_org", created_by=owner.id)
        db.add(org)
        await db.flush()
        workspace = Workspace(organization_id=org.id, name="Eval")
        db.add(workspace)
        await db.flush()
        db.add(
            WorkspaceMembership(
                workspace_id=workspace.id, user_id=owner.id, role=WorkspaceRole.owner
            )
        )
        kb = KnowledgeBase(
            owner_id=owner.id, workspace_id=workspace.id, name="eval_kb"
        )
        db.add(kb)
        await db.flush()

        for path in sorted(fixtures.glob("*.md")):
            segments = parse_file(str(path))
            pieces: list[tuple[str, int | None, int, str]] = []
            for parent_seq, (text, page) in enumerate(segments):
                for piece in split_text(
                    text, settings.chunk_size, settings.chunk_overlap
                ):
                    pieces.append((piece, page, parent_seq, text))
            vectors = await asyncio.to_thread(
                embed_documents, [p for p, _, _, _ in pieces]
            )
            document = Document(
                owner_id=owner.id,
                kb_id=kb.id,
                filename=path.name,
                filepath=str(path),
                object_key=str(path),
                content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
                status=DocStatus.ready,
                stage=DocStatus.ready.value,
                active_index_version=1,
                chunk_count=len(pieces),
                embedding_model=settings.embedding_model,
            )
            db.add(document)
            await db.flush()
            db.add_all(
                Chunk(
                    document_id=document.id,
                    owner_id=owner.id,
                    kb_id=kb.id,
                    index_version=1,
                    seq=index,
                    page=page,
                    parent_seq=parent_seq,
                    content=piece,
                    parent_content=parent_content,
                    content_tokens=func.to_tsvector("simple", _tokenize(piece)),
                    embedding=vector,
                )
                for index, (
                    (piece, page, parent_seq, parent_content),
                    vector,
                ) in enumerate(zip(pieces, vectors))
            )
        await db.commit()
        print(f"EVAL_USER={username}")
        print(f"EVAL_PASSWORD={password}")
        print(f"EVAL_KB_ID={kb.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
