"""Context de-duplication, compression, ordering and token budgeting."""

import re
from dataclasses import replace

from app.config import settings
from app.services.retriever import RetrievedChunk, extract_keywords

_SENTENCE = re.compile(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+")


def _fingerprint(value: str) -> set[str]:
    normalized = re.sub(r"\s+", "", value).casefold()
    if len(normalized) < 3:
        return {normalized}
    return {normalized[index : index + 3] for index in range(len(normalized) - 2)}


def _similar(left: str, right: str) -> float:
    a, b = _fingerprint(left), _fingerprint(right)
    return len(a & b) / max(1, len(a | b))


def _compress(content: str, query: str) -> str:
    if not settings.context_compression_enabled or len(content) < settings.context_compression_min_chars:
        return content
    keywords = extract_keywords(query, max_terms=12)
    sentences = [value.strip() for value in _SENTENCE.split(content) if value.strip()]
    selected = [
        sentence
        for sentence in sentences
        if any(keyword.casefold() in sentence.casefold() for keyword in keywords)
    ]
    if not selected:
        return content[: settings.context_compression_min_chars]
    # Keep one neighboring sentence around every match to avoid fact fragments.
    indexes = {index for index, sentence in enumerate(sentences) if sentence in selected}
    expanded = sorted({near for index in indexes for near in (index - 1, index, index + 1) if 0 <= near < len(sentences)})
    return " ".join(sentences[index] for index in expanded)


def _lost_in_middle_order(chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    if not settings.lost_in_middle_enabled or len(chunks) < 3:
        return chunks
    # Highest relevance remains first, second-highest is placed at the end.
    ordered = [chunks[0]]
    middle = chunks[2::2] + list(reversed(chunks[3::2]))
    ordered.extend(middle)
    ordered.append(chunks[1])
    return ordered


def prepare_context(chunks: list[RetrievedChunk], query: str) -> list[RetrievedChunk]:
    unique = []
    for chunk in chunks:
        if any(_similar(chunk.content, existing.content) >= 0.88 for existing in unique):
            continue
        unique.append(replace(chunk, content=_compress(chunk.content, query)))
    ordered = _lost_in_middle_order(unique)
    budget = max(1, settings.context_max_tokens)
    selected = []
    used = 0
    for chunk in ordered:
        estimated = max(1, len(chunk.content) // 2)
        if selected and used + estimated > budget:
            continue
        if estimated > budget:
            chunk = replace(chunk, content=chunk.content[: budget * 2])
            estimated = budget
        selected.append(chunk)
        used += estimated
    return selected
