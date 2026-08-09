"""Lightweight entity graph construction and permission-scoped graph recall."""

import itertools
import re

import jieba.analyse
from sqlalchemy import delete, or_, select

from app.models import GraphEntity, GraphRelation


def _normalize_entity(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()[:255]


def rebuild_document_graph(db, *, document, chunks: list, workspace_id: str) -> None:
    """Build entity/co-occurrence edges from persisted chunks in the same transaction."""
    db.execute(delete(GraphEntity).where(GraphEntity.document_id == document.id))
    db.flush()
    for chunk in chunks:
        tags = []
        for value in jieba.analyse.extract_tags(chunk.content, topK=8, withWeight=False):
            normalized = _normalize_entity(value)
            if len(normalized) >= 2 and normalized not in {item[1] for item in tags}:
                tags.append((value[:255], normalized))
        entities = [
            GraphEntity(
                workspace_id=workspace_id,
                kb_id=document.kb_id,
                document_id=document.id,
                name=name,
                normalized_name=normalized,
                entity_type="keyword",
                chunk_id=chunk.id,
            )
            for name, normalized in tags
        ]
        db.add_all(entities)
        db.flush()
        db.add_all(
            GraphRelation(
                workspace_id=workspace_id,
                kb_id=document.kb_id,
                source_entity_id=left.id,
                target_entity_id=right.id,
                relation_type="co_occurs",
                evidence_chunk_id=chunk.id,
            )
            for left, right in itertools.combinations(entities, 2)
        )


async def graph_recall_chunk_ids(
    db,
    *,
    keywords: list[str],
    kb_id: str | None,
    limit: int,
) -> list[str]:
    """Return evidence chunks reached from matched entities through up to two hops."""
    normalized = [_normalize_entity(value) for value in keywords if len(_normalize_entity(value)) >= 2]
    if not normalized:
        return []
    conditions = [GraphEntity.normalized_name == value for value in normalized]
    conditions.extend(GraphEntity.normalized_name.contains(value) for value in normalized[:4])
    seed_stmt = select(GraphEntity.id, GraphEntity.chunk_id).where(or_(*conditions))
    if kb_id:
        seed_stmt = seed_stmt.where(GraphEntity.kb_id == kb_id)
    seeds = (await db.execute(seed_stmt.limit(limit))).all()
    if not seeds:
        return []
    entity_ids = {row.id for row in seeds}
    chunk_ids = [row.chunk_id for row in seeds if row.chunk_id]
    for _ in range(2):
        relation_rows = (
            await db.execute(
                select(
                    GraphRelation.source_entity_id,
                    GraphRelation.target_entity_id,
                    GraphRelation.evidence_chunk_id,
                )
                .where(
                    or_(
                        GraphRelation.source_entity_id.in_(entity_ids),
                        GraphRelation.target_entity_id.in_(entity_ids),
                    )
                )
                .limit(limit * 4)
            )
        ).all()
        next_ids = set()
        for row in relation_rows:
            next_ids.update((row.source_entity_id, row.target_entity_id))
            if row.evidence_chunk_id:
                chunk_ids.append(row.evidence_chunk_id)
        if next_ids.issubset(entity_ids):
            break
        entity_ids.update(next_ids)
        related = (
            await db.execute(
                select(GraphEntity.chunk_id).where(GraphEntity.id.in_(next_ids)).limit(limit * 2)
            )
        ).scalars()
        chunk_ids.extend(value for value in related if value)
    return list(dict.fromkeys(chunk_ids))[:limit]
