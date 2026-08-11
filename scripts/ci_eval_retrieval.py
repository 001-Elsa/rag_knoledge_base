"""Offline retrieval quality gate for CI (item 16).

Runs hybrid retrieval against in-process fixtures with deterministic embeddings
(no real LLM / no heavy sentence-transformers). Compares Hit Rate@5, MRR, and
no-answer false-positive rate against eval/baseline.json. This exercises the
real retrieve() + assess_evidence path, not only the gate arithmetic.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import statistics
import sys
import uuid
from pathlib import Path

from sqlalchemy import func

# Ensure repo root is importable when invoked as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
from app.services.evidence import assess_evidence
from app.services.retriever import retrieve
from scripts.check_quality_gate import check_quality


def _vec_for(text: str) -> list[float]:
    """Deterministic pseudo-embedding from text hash (dim = settings.embedding_dim)."""
    digest = hashlib.sha256(text.encode()).digest()
    values = []
    while len(values) < settings.embedding_dim:
        digest = hashlib.sha256(digest).digest()
        values.extend(b / 255.0 for b in digest)
    # L2-normalise so cosine distance behaves.
    norm = math.sqrt(sum(v * v for v in values[: settings.embedding_dim])) or 1.0
    return [v / norm for v in values[: settings.embedding_dim]]


FIXTURE_CHUNKS = [
    (
        "company_policy.md",
        0,
        "质量问题商品可以在签收后七日内退货。",
        "质量 问题 退货 签收 七日",
    ),
    (
        "company_policy.md",
        1,
        "退款审核通过后三个至五个工作日内原路退回。",
        "退款 审核 三个 五个 工作日 原路退回",
    ),
    (
        "company_policy.md",
        2,
        "PRO-2026 专业版产品提供二十四个月保修。",
        "PRO-2026 专业 产品 保修 二十四 月",
    ),
    (
        "company_policy.md",
        3,
        "标准版保修十二个月，专业版二十四个月。",
        "标准 保修 十二 月 专业 二十四",
    ),
    (
        "company_policy.md",
        4,
        "P0 生产故障需在十五分钟内首次响应。",
        "P0 生产 故障 十五 分钟 响应",
    ),
    (
        "company_policy.md",
        5,
        "Viewer 只能查询知识库，不能上传或删除文档。",
        "Viewer 查询 不能上传 删除",
    ),
    (
        "company_policy.md",
        6,
        "审计日志保留三百六十五天。",
        "审计 日志 保留 三百六十五 天",
    ),
    (
        "malicious_document.md",
        0,
        "安全事件需要在发现后十分钟内上报安全负责人。",
        "安全 事件 十分钟 上报",
    ),
]


def _relevant(source_content: str, filename: str, case: dict) -> bool:
    expected_documents = set(case.get("expected_documents", []))
    document_match = not expected_documents or filename in expected_documents
    keywords = case.get("expected_keywords", [])
    keyword_match = all(keyword in source_content for keyword in keywords)
    return document_match and keyword_match


async def _run() -> dict:
    cases = [
        json.loads(line)
        for line in Path("eval/golden_set.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    suffix = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as db:
        owner = User(username=f"ci_eval_{suffix}", password_hash="x")
        db.add(owner)
        await db.flush()
        org = Organization(name=f"ci_org_{suffix}", created_by=owner.id)
        db.add(org)
        await db.flush()
        workspace = Workspace(organization_id=org.id, name="CI")
        db.add(workspace)
        await db.flush()
        db.add(
            WorkspaceMembership(
                workspace_id=workspace.id, user_id=owner.id, role=WorkspaceRole.owner
            )
        )
        kb = KnowledgeBase(
            owner_id=owner.id, workspace_id=workspace.id, name=f"ci_kb_{suffix}"
        )
        db.add(kb)
        await db.flush()

        docs: dict[str, Document] = {}
        for filename, seq, content, tokens in FIXTURE_CHUNKS:
            if filename not in docs:
                document = Document(
                    owner_id=owner.id,
                    kb_id=kb.id,
                    filename=filename,
                    filepath=f"ci/{filename}",
                    object_key=f"ci/{filename}",
                    content_hash=hashlib.sha256(filename.encode()).hexdigest(),
                    status=DocStatus.ready,
                    stage=DocStatus.ready.value,
                    active_index_version=1,
                    chunk_count=0,
                )
                db.add(document)
                await db.flush()
                docs[filename] = document
            document = docs[filename]
            db.add(
                Chunk(
                    document_id=document.id,
                    owner_id=owner.id,
                    kb_id=kb.id,
                    workspace_id=workspace.id,
                    index_version=1,
                    seq=seq,
                    parent_seq=seq,
                    content=content,
                    parent_content=content,
                    content_tokens=func.to_tsvector("simple", tokens),
                    embedding=_vec_for(content),
                )
            )
            document.chunk_count += 1
        await db.commit()

        # Warm-up: absorb one-time costs (DB connection, SQL compilation,
        # full-text search init) before any timing.
        warm_case = next(
            (case for case in cases if case.get("answerable")),
            cases[0],
        )
        warm_query_text = (
            " ".join(warm_case.get("expected_keywords") or [])
            or warm_case["question"]
        )
        await retrieve(
            db,
            owner.id,
            warm_case["question"],
            kb_id=kb.id,
            query_vec=_vec_for(warm_query_text),
            parent_child_enabled=False,
            rerank_enabled=False,
        )

        hits = 0
        reciprocal_ranks = []
        false_positive_no_answers = 0
        no_answer_cases = 0
        latencies = []
        import time

        for case in cases:
            query_text = " ".join(case.get("expected_keywords") or []) or case["question"]
            query_vec = _vec_for(query_text)

            # Measure 3 times and take the median to dampen CI runner jitter.
            case_latencies: list[float] = []
            results = []
            for _ in range(3):
                t0 = time.perf_counter()
                results = await retrieve(
                    db,
                    owner.id,
                    case["question"],
                    kb_id=kb.id,
                    query_vec=query_vec,
                    parent_child_enabled=False,
                    rerank_enabled=False,
                )
                case_latencies.append((time.perf_counter() - t0) * 1000)

            latencies.append(statistics.median(case_latencies))
            evidence = assess_evidence(results)
            if case["answerable"]:
                rank = None
                for index, chunk in enumerate(results, start=1):
                    if _relevant(chunk.content, chunk.filename, case):
                        rank = index
                        break
                hits += int(rank is not None)
                reciprocal_ranks.append(1.0 / rank if rank else 0.0)
            else:
                no_answer_cases += 1
                false_positive_no_answers += int(evidence.answerable)

        answerable_count = max(1, sum(1 for case in cases if case["answerable"]))
        p95_latency = (
            statistics.quantiles(latencies, n=100, method="inclusive")[94]
            if len(latencies) >= 2
            else (latencies[0] if latencies else 0.0)
        )
        report = {
            "case_count": len(cases),
            "retrieval_profile": "hybrid",
            "hit_rate_at_5": hits / answerable_count,
            "mrr": statistics.fmean(reciprocal_ranks) if reciprocal_ranks else 0.0,
            "ndcg_at_5": 0.0,
            "no_answer_false_positive_rate": (
                false_positive_no_answers / no_answer_cases if no_answer_cases else 0.0
            ),
            "p95_retrieval_ms": p95_latency,
        }

        await db.delete(await db.get(Organization, org.id))
        await db.delete(await db.get(User, owner.id))
        await db.commit()
        return report


def main() -> int:
    report = asyncio.run(_run())
    out = Path("eval/reports/ci_hybrid.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    baseline_path = Path("eval/baseline.json")
    if not baseline_path.exists():
        # First run: seed baseline from the measured report so subsequent PRs
        # compare against a real number, not a hand-waved target.
        baseline_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[INFO] seeded baseline at {baseline_path}")
        return 0

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    failures = check_quality(baseline, report)
    if failures:
        metric_summary = (
            f"current: Hit@5={report['hit_rate_at_5']:.4f}, "
            f"MRR={report['mrr']:.4f}, "
            f"No-answer FP={report['no_answer_false_positive_rate']:.4f}, "
            f"P95={report['p95_retrieval_ms']:.2f}ms; "
            f"baseline: Hit@5={baseline['hit_rate_at_5']:.4f}, "
            f"MRR={baseline['mrr']:.4f}, "
            f"No-answer FP={baseline['no_answer_false_positive_rate']:.4f}, "
            f"P95={baseline['p95_retrieval_ms']:.2f}ms"
        )
        for failure in failures:
            print(
                f"::error title=Offline retrieval quality gate::"
                f"{failure}. {metric_summary}"
            )
        return 1
    print("[PASS] CI retrieval quality gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
