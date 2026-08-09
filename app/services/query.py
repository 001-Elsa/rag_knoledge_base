"""Deterministic query cleaning, terminology normalization, routing and KB selection."""

import re
import unicodedata
from dataclasses import dataclass

from app.config import settings

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_GREETINGS = re.compile(r"^(你好|您好|hello|hi|谢谢|thanks)[!！。,.\s]*$", re.IGNORECASE)
_EXACT_TOKEN = re.compile(r"\b(?:[A-Z]{2,}[A-Z0-9_-]*|[A-Z]+-?\d{2,}|\d{3,})\b")


@dataclass(frozen=True)
class QueryRoute:
    query: str
    retrieval_mode: str
    kb_id: str | None
    needs_retrieval: bool = True
    reason: str = "hybrid_default"


def _term_dictionary() -> dict[str, str]:
    result = {}
    for item in settings.query_term_dictionary.split(","):
        if "=" in item:
            alias, canonical = item.split("=", 1)
            if alias.strip() and canonical.strip():
                result[alias.strip().casefold()] = canonical.strip()
    return result


def clean_query(value: str) -> str:
    query = unicodedata.normalize("NFKC", value or "")
    query = _CONTROL.sub(" ", query)
    query = re.sub(r"\s+", " ", query).strip()
    for alias, canonical in _term_dictionary().items():
        query = re.sub(re.escape(alias), canonical, query, flags=re.IGNORECASE)
    # Common Chinese typo/variant normalization; the configurable dictionary handles domains.
    for wrong, correct in {"帐户": "账户", "登入": "登录", "数据库库": "数据库"}.items():
        query = query.replace(wrong, correct)
    return query


def route_query(query: str, *, requested_profile: str, knowledge_bases=()) -> QueryRoute:
    cleaned = clean_query(query) if settings.query_clean_enabled else query.strip()
    selected_kb = None
    for kb in knowledge_bases:
        if kb.name and kb.name.casefold() in cleaned.casefold():
            selected_kb = kb.id
            break
    if _GREETINGS.match(cleaned):
        return QueryRoute(cleaned, "none", selected_kb, False, "social_query")
    if requested_profile != "auto":
        return QueryRoute(cleaned, requested_profile, selected_kb, True, "explicit_profile")
    if not settings.auto_route_enabled:
        return QueryRoute(cleaned, "hybrid", selected_kb, True, "auto_route_disabled")
    if _EXACT_TOKEN.search(cleaned):
        return QueryRoute(cleaned, "keyword", selected_kb, True, "exact_identifier")
    if any(word in cleaned for word in ("关系", "关联", "影响", "上下游", "多跳")):
        return QueryRoute(cleaned, "graph", selected_kb, True, "relationship_query")
    if any(word in cleaned for word in ("类似", "含义", "概念", "是什么")):
        return QueryRoute(cleaned, "vector", selected_kb, True, "semantic_query")
    return QueryRoute(cleaned, "hybrid", selected_kb, True, "hybrid_default")
