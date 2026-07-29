"""Offline ablation with real local embeddings (no LLM required for most profiles).

Loads eval fixtures into PostgreSQL, embeds with BAAI/bge-small-zh-v1.5, runs each
retrieval profile through app.services.retriever.retrieve, and writes measured
reports under eval/reports/. multi_query is skipped unless --with-llm is passed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import statistics
import sys
import time
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
from app.services.embedder import embed_documents, embed_query
from app.services.evidence import assess_evidence
from app.services.retriever import retrieve

PROFILES = ["vector", "hybrid", "hybrid_rerank", "parent_child", "multi_query"]


def _relevant(content: str, filename: str, case: dict) -> bool:
    expected_documents = set(case.get("expected_documents", []))
    document_match = not expected_documents or filename in expected_documents
    keywords = case.get("expected_keywords", [])
    keyword_match = all(keyword in content for keyword in keywords)
    return document_match and keyword_match


def _tokenize_for_fts(text: str) -> str:
    import jieba

    return " ".join(t for t in jieba.cut_for_search(text) if t.strip())


async def _seed(owner_id: str, kb_id: str, fixtures_dir: Path) -> dict[str, str]:
    """Parse fixture markdown into chunks, embed, and persist. Returns filename->id."""
    from app.services.chunker import split_text
    from app.services.parser import parse_file

    files = sorted(fixtures_dir.glob("*.md"))
    mapping: dict[str, str] = {}
    async with AsyncSessionLocal() as db:
        for path in files:
            segments = parse_file(str(path))
            pieces: list[tuple[str, int | None, int, str]] = []
            for parent_seq, (text, page) in enumerate(segments):
                for piece in split_text(text, settings.chunk_size, settings.chunk_overlap):
                    pieces.append((piece, page, parent_seq, text))
            if not pieces:
                continue
            vectors = await asyncio.to_thread(
                embed_documents, [p for p, _, _, _ in pieces]
            )
            content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            document = Document(
                owner_id=owner_id,
                kb_id=kb_id,
                filename=path.name,
                filepath=str(path),
                object_key=str(path),
                content_hash=content_hash,
                status=DocStatus.ready,
                stage=DocStatus.ready.value,
                active_index_version=1,
                chunk_count=len(pieces),
                embedding_model=settings.embedding_model,
            )
            db.add(document)
            await db.flush()
            mapping[path.name] = document.id
            db.add_all(
                Chunk(
                    document_id=document.id,
                    owner_id=owner_id,
                    kb_id=kb_id,
                    index_version=1,
                    seq=index,
                    page=page,
                    parent_seq=parent_seq,
                    content=piece,
                    parent_content=parent_content,
                    content_tokens=func.to_tsvector(
                        "simple", _tokenize_for_fts(piece)
                    ),
                    embedding=vector,
                )
                for index, ((piece, page, parent_seq, parent_content), vector) in enumerate(
                    zip(pieces, vectors)
                )
            )
        await db.commit()
    return mapping


async def _evaluate_profile(
    owner_id: str, kb_id: str, cases: list[dict], profile: str
) -> dict:
    hits = 0
    reciprocal_ranks = []
    ndcg_values = []
    false_positive_no_answers = 0
    no_answer_cases = 0
    latencies = []
    failures = []

    async with AsyncSessionLocal() as db:
        for case in cases:
            extra = []
            if profile == "multi_query":
                from app.services.llm import expand_queries

                extra = await expand_queries(case["question"])
            t0 = time.perf_counter()
            query_vec = await asyncio.to_thread(embed_query, case["question"])
            results = await retrieve(
                db,
                owner_id,
                case["question"],
                kb_id=kb_id,
                query_vec=query_vec,
                extra_queries=extra,
                keyword_enabled=profile != "vector",
                rerank_enabled=profile == "hybrid_rerank",
                parent_child_enabled=profile == "parent_child",
            )
            latencies.append((time.perf_counter() - t0) * 1000)
            evidence = assess_evidence(results)
            if case["answerable"]:
                rank = None
                gains = []
                for index, chunk in enumerate(results[:5], start=1):
                    ok = _relevant(chunk.content, chunk.filename, case)
                    gains.append(1.0 if ok else 0.0)
                    if ok and rank is None:
                        rank = index
                hits += int(rank is not None)
                reciprocal_ranks.append(1.0 / rank if rank else 0.0)
                dcg = sum(g / math.log2(i + 1) for i, g in enumerate(gains, 1))
                ndcg_values.append(dcg)
                if rank is None:
                    failures.append({"id": case["id"], "reason": "retrieval_miss"})
            else:
                no_answer_cases += 1
                false_positive = evidence.answerable
                false_positive_no_answers += int(false_positive)
                if false_positive:
                    failures.append(
                        {"id": case["id"], "reason": "false_positive_answer"}
                    )

    answerable_count = max(1, sum(1 for c in cases if c["answerable"]))
    sorted_latency = sorted(latencies)
    p95_index = min(len(sorted_latency) - 1, math.ceil(len(sorted_latency) * 0.95) - 1)
    return {
        "case_count": len(cases),
        "retrieval_profile": profile,
        "hit_rate_at_5": hits / answerable_count,
        "mrr": statistics.fmean(reciprocal_ranks) if reciprocal_ranks else 0.0,
        "ndcg_at_5": statistics.fmean(ndcg_values) if ndcg_values else 0.0,
        "no_answer_false_positive_rate": (
            false_positive_no_answers / no_answer_cases if no_answer_cases else 0.0
        ),
        "p95_retrieval_ms": sorted_latency[p95_index] if sorted_latency else 0.0,
        "failures": failures,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "embedding_model": settings.embedding_model,
    }


async def _main_async(args: argparse.Namespace) -> int:
    cases = [
        json.loads(line)
        for line in Path(args.file).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    suffix = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as db:
        owner = User(username=f"ablation_{suffix}", password_hash="x")
        db.add(owner)
        await db.flush()
        org = Organization(name=f"ablation_org_{suffix}", created_by=owner.id)
        db.add(org)
        await db.flush()
        workspace = Workspace(organization_id=org.id, name="Ablation")
        db.add(workspace)
        await db.flush()
        db.add(
            WorkspaceMembership(
                workspace_id=workspace.id, user_id=owner.id, role=WorkspaceRole.owner
            )
        )
        kb = KnowledgeBase(
            owner_id=owner.id, workspace_id=workspace.id, name=f"ablation_kb_{suffix}"
        )
        db.add(kb)
        await db.commit()
        owner_id, kb_id, org_id = owner.id, kb.id, org.id

    print("Loading embedding model and seeding fixtures...")
    await _seed(owner_id, kb_id, Path(args.fixtures))

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_rows = []
    profiles = list(PROFILES)
    if not args.with_llm:
        profiles = [p for p in profiles if p != "multi_query"]

    for profile in profiles:
        print(f"=== profile={profile} ===")
        if profile == "hybrid_rerank" and not settings.rerank_enabled:
            # Force on for this profile only via retrieve(rerank_enabled=True).
            pass
        report = await _evaluate_profile(owner_id, kb_id, cases, profile)
        path = out_dir / f"{profile}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        summary_rows.append(
            {
                "profile": profile,
                "hit_rate_at_5": report["hit_rate_at_5"],
                "mrr": report["mrr"],
                "ndcg_at_5": report["ndcg_at_5"],
                "no_answer_false_positive_rate": report["no_answer_false_positive_rate"],
                "p95_retrieval_ms": report["p95_retrieval_ms"],
                "case_count": report["case_count"],
            }
        )

    summary_path = out_dir / "ablation_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "profiles": summary_rows,
                "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "embedding_model": settings.embedding_model,
                "note": "Measured offline via scripts/run_local_ablation.py with real local embeddings.",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    md_path = out_dir / "ablation_summary.md"
    lines = [
        "# Retrieval ablation (measured)",
        "",
        f"Embedding: `{settings.embedding_model}`  ",
        f"Measured at: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
        "",
        "| Strategy | Hit Rate@5 | MRR | nDCG@5 | No-answer FP | P95 ms |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in summary_rows:
        lines.append(
            "| {profile} | {hit_rate_at_5:.3f} | {mrr:.3f} | {ndcg_at_5:.3f} | "
            "{no_answer_false_positive_rate:.3f} | {p95_retrieval_ms:.1f} |".format(**row)
        )
    lines.append("")
    lines.append("_Generated by `scripts/run_local_ablation.py`. Do not hand-edit numbers._")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Cleanup tenant
    async with AsyncSessionLocal() as db:
        await db.delete(await db.get(Organization, org_id))
        await db.delete(await db.get(User, owner_id))
        await db.commit()

    print(f"Wrote {summary_path} and {md_path}")
    return 0 if summary_rows else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default="eval/golden_set.jsonl")
    parser.add_argument("--fixtures", default="eval/fixtures")
    parser.add_argument("--out-dir", default="eval/reports")
    parser.add_argument(
        "--with-llm",
        action="store_true",
        help="Also run multi_query (needs LLM_API_KEY)",
    )
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
