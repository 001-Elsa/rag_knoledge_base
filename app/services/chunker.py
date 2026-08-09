"""Fixed, paragraph, recursive, section-aware and semantic chunking."""

import math
import re
from dataclasses import dataclass

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+")


@dataclass(frozen=True)
class StructuredChunk:
    content: str
    parent_content: str
    parent_seq: int = 0
    page: int | None = None
    section: str | None = None
    content_type: str = "text"
    source_url: str | None = None


def split_text(text: str, chunk_size: int = 512, overlap: int = 64) -> list[str]:
    return _recursive_split(text, chunk_size, overlap)


def split_segments(
    segments,
    *,
    strategy: str = "recursive",
    chunk_size: int = 512,
    overlap: int = 64,
    semantic_threshold: float = 0.42,
) -> list[StructuredChunk]:
    if strategy not in {"fixed", "paragraph", "recursive", "section", "semantic"}:
        raise ValueError(f"未知切分策略: {strategy}")
    chunks: list[StructuredChunk] = []
    for parent_seq, segment in enumerate(segments):
        text = _normalize(segment.text)
        if not text:
            continue
        if strategy == "fixed":
            pieces = _fixed_split(text, chunk_size, overlap)
        elif strategy == "paragraph":
            pieces = _paragraph_split(text, chunk_size, overlap)
        elif strategy == "semantic":
            pieces = _semantic_split(text, chunk_size, overlap, semantic_threshold)
        else:  # recursive and section both preserve ParsedSegment section metadata
            pieces = _recursive_split(text, chunk_size, overlap)
        for piece in pieces:
            chunks.append(
                StructuredChunk(
                    piece,
                    text,
                    parent_seq,
                    getattr(segment, "page", None),
                    getattr(segment, "section", None),
                    getattr(segment, "content_type", "text"),
                    getattr(segment, "source_url", None),
                )
            )
    return chunks


def _validate(chunk_size: int, overlap: int) -> None:
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须为正数")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap 必须大于等于 0 且小于 chunk_size")


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _fixed_split(text: str, chunk_size: int, overlap: int) -> list[str]:
    _validate(chunk_size, overlap)
    text = _normalize(text)
    if not text:
        return []
    step = chunk_size - overlap
    return [text[start : start + chunk_size] for start in range(0, len(text), step)]


def _paragraph_split(text: str, chunk_size: int, overlap: int) -> list[str]:
    _validate(chunk_size, overlap)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", _normalize(text)) if p.strip()]
    units = []
    for paragraph in paragraphs:
        units.extend(_fixed_split(paragraph, chunk_size, 0) if len(paragraph) > chunk_size else [paragraph])
    return _merge_units(units, chunk_size, overlap)


def _recursive_split(text: str, chunk_size: int, overlap: int) -> list[str]:
    _validate(chunk_size, overlap)
    text = _normalize(text)
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    units: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= chunk_size:
            units.append(paragraph)
        else:
            units.extend(_split_long_paragraph(paragraph, chunk_size))
    return _merge_units(units, chunk_size, overlap)


def _split_long_paragraph(paragraph: str, chunk_size: int) -> list[str]:
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(paragraph) if s.strip()]
    units = []
    for sentence in sentences:
        units.extend(
            sentence[i : i + chunk_size]
            for i in range(0, len(sentence), chunk_size)
        ) if len(sentence) > chunk_size else units.append(sentence)
    return units


def _merge_units(units: list[str], chunk_size: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current}\n{unit}" if current else unit
        if len(candidate) <= chunk_size:
            current = candidate
            continue
        if current:
            chunks.append(current)
        tail = current[-overlap:] if current and overlap else ""
        candidate = f"{tail}\n{unit}" if tail else unit
        if len(candidate) <= chunk_size:
            current = candidate
        else:
            hard = _fixed_split(unit, chunk_size, overlap)
            chunks.extend(hard[:-1])
            current = hard[-1]
    if current:
        chunks.append(current)
    return chunks


def _semantic_split(text: str, chunk_size: int, overlap: int, threshold: float) -> list[str]:
    """Split paragraph groups where neighboring embedding similarity drops."""
    _validate(chunk_size, overlap)
    units = [p.strip() for p in re.split(r"\n\s*\n", _normalize(text)) if p.strip()]
    if len(units) < 2:
        return _recursive_split(text, chunk_size, overlap)
    try:
        from app.services.embedder import embed_documents

        vectors = embed_documents(units)
    except Exception:
        return _recursive_split(text, chunk_size, overlap)
    grouped: list[str] = []
    current = units[0]
    previous = vectors[0]
    for unit, vector in zip(units[1:], vectors[1:]):
        similarity = sum(a * b for a, b in zip(previous, vector)) / max(
            math.sqrt(sum(a * a for a in previous)) * math.sqrt(sum(b * b for b in vector)),
            1e-9,
        )
        if similarity < threshold or len(current) + len(unit) + 1 > chunk_size:
            grouped.append(current)
            current = unit
        else:
            current = f"{current}\n{unit}"
        previous = vector
    grouped.append(current)
    return _merge_units(grouped, chunk_size, overlap)
