"""Runtime evidence gating, prompt-injection signals, and citation validation."""

import re
from dataclasses import asdict, dataclass

from app.config import settings
from app.services.retriever import RetrievedChunk

_INJECTION_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"ignore (all |the )?(previous|system) instructions?",
        r"忽略(以上|之前|系统)(所有)?(指令|提示)",
        r"(reveal|print|泄露|输出).{0,20}(system prompt|系统提示词)",
        r"(call|invoke|调用).{0,20}(tool|工具)",
        r"do not (cite|follow)",
    )
]
_CITATION = re.compile(r"\[(\d+)]")


@dataclass
class EvidenceDecision:
    answerable: bool
    reason: str
    top_vector_similarity: float
    top_rrf_score: float
    cross_route_hits: int
    suspicious_chunk_ids: list[str]

    def as_dict(self) -> dict:
        return asdict(self)


def detect_prompt_injection(text: str) -> bool:
    return settings.prompt_injection_detection_enabled and any(
        pattern.search(text) for pattern in _INJECTION_PATTERNS
    )


def assess_evidence(chunks: list[RetrievedChunk]) -> EvidenceDecision:
    suspicious = [
        chunk.chunk_id for chunk in chunks if detect_prompt_injection(chunk.content)
    ]
    top_similarity = max((chunk.vector_similarity for chunk in chunks), default=0.0)
    top_rrf = max((chunk.score for chunk in chunks), default=0.0)
    cross_route_hits = sum(
        chunk.keyword_hit and chunk.vector_similarity > 0 for chunk in chunks
    )
    if len(chunks) < settings.evidence_min_chunks:
        return EvidenceDecision(
            False,
            "insufficient_evidence_count",
            top_similarity,
            top_rrf,
            cross_route_hits,
            suspicious,
        )
    if top_rrf < settings.evidence_min_rrf_score:
        return EvidenceDecision(
            False,
            "retrieval_score_below_threshold",
            top_similarity,
            top_rrf,
            cross_route_hits,
            suspicious,
        )
    return EvidenceDecision(
        True,
        "evidence_threshold_met",
        top_similarity,
        top_rrf,
        cross_route_hits,
        suspicious,
    )


def validate_citations(answer: str, source_count: int) -> dict:
    cited = [int(value) for value in _CITATION.findall(answer)]
    invalid = sorted({value for value in cited if value < 1 or value > source_count})
    factual_lines = [
        line.strip()
        for line in answer.splitlines()
        if line.strip()
        and not line.lstrip().startswith(("#", ">", "-", "*"))
        and len(line.strip()) >= 12
    ]
    uncited_claims = [
        line[:160] for line in factual_lines if not _CITATION.search(line)
    ]
    valid = source_count > 0 and bool(cited) and not invalid and not uncited_claims
    return {
        "valid": valid,
        "cited_source_numbers": sorted(set(cited)),
        "invalid_source_numbers": invalid,
        "uncited_claims": uncited_claims[:5],
    }
