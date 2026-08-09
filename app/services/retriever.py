"""混合检索：向量召回 + 全文检索关键词召回 → RRF 融合 →（可选）交叉编码器重排。

为什么要两路召回？
- 向量检索擅长语义匹配（"怎么退款" 能命中 "退货流程"），但对专有名词、型号、
  代码标识符这类精确词不敏感；
- 关键词检索正好相反；
- 用 RRF（Reciprocal Rank Fusion）融合两路排名，不需要调权重就有稳定提升。

关键词召回实现演进：
- v1-v3 用 LIKE '%kw%'：无法走索引，全表顺序扫描，数据量大后是 O(n) 慢查询；
- v4 起用 PostgreSQL 全文检索：入库时 jieba 分词写入 tsvector + GIN 倒排索引，
  查询用 to_tsquery（OR 连接）+ ts_rank 排序——倒排索引查询，数据量增长不慌。
"""
import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass

import jieba
from sqlalchemy import Text, func, literal, or_, select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.metrics import RERANK_DURATION
from app.models import Chunk, DocStatus, Document, KnowledgeBase, WorkspaceMembership
from app.services.embedder import embed_query
from app.services.graph import graph_recall_chunk_ids

logger = logging.getLogger(__name__)

# 检索用停用词（简版）
_STOPWORDS = {"的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一个",
              "什么", "怎么", "如何", "为什么", "请问", "吗", "呢", "啊", "a", "an", "the",
              "is", "are", "what", "how", "why", "to", "of", "in", "on", "for"}


@dataclass
class RetrievedChunk:
    chunk_id: str
    document_id: str
    filename: str
    seq: int
    page: int | None
    content: str
    score: float
    vector_similarity: float = 0.0
    keyword_hit: bool = False
    parent_seq: int = 0
    section: str | None = None
    content_type: str = "text"
    source_url: str | None = None
    created_at: object | None = None
    kb_id: str | None = None


_TSQUERY_SAFE = re.compile(r"^[\w一-鿿]+$")  # 只允许中英文数字下划线，防 tsquery 语法注入


def extract_keywords(query: str, max_terms: int = 8) -> list[str]:
    """jieba 分词 + 去停用词 + 去单字，得到关键词列表。"""
    terms = []
    for term in jieba.cut_for_search(query):
        term = term.strip().lower()
        if len(term) >= 2 and term not in _STOPWORDS and term not in terms:
            terms.append(term)
    return terms[:max_terms]


def build_tsquery(keywords: list[str]) -> str:
    """把关键词拼成 OR 连接的 tsquery 表达式（如 "退款 | 流程"）。
    过滤掉含特殊字符的词——tsquery 语法字符（& | ! ( ) :）会引发语法错误。"""
    safe = [kw for kw in keywords if _TSQUERY_SAFE.match(kw)]
    return " | ".join(safe)


def rrf_fuse(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    """RRF: score(d) = Σ 1 / (k + rank_i(d))。返回 {chunk_id: 融合分}。"""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
    return scores


async def _keyword_search(
    db: AsyncSession,
    *,
    filters: list,
    query: str,
    limit: int,
):
    """PostgreSQL 全文检索关键词召回。抽成独立函数便于故障注入与降级测试。"""
    tsquery_expr = build_tsquery(extract_keywords(query))
    if not tsquery_expr:
        return []
    tsq = func.to_tsquery("simple", tsquery_expr)
    rank = func.ts_rank_cd(Chunk.content_tokens, tsq).label("rank")
    kw_stmt = (
        select(Chunk, Document.filename, rank)
        .join(Document, Chunk.document_id == Document.id)
        .join(KnowledgeBase, KnowledgeBase.id == Chunk.kb_id)
        .join(
            WorkspaceMembership,
            WorkspaceMembership.workspace_id == KnowledgeBase.workspace_id,
        )
        .where(*filters, Chunk.content_tokens.op("@@")(tsq))
        .order_by(rank.desc())
        .limit(limit)
    )
    rows = list((await db.execute(kw_stmt)).all())
    return sorted(rows, key=lambda row: _bm25_score(row[0].content, extract_keywords(query), rows), reverse=True)


def _bm25_score(content: str, keywords: list[str], rows: list, *, k1: float = 1.5, b: float = 0.75) -> float:
    """BM25 score over the GIN-pruned candidate corpus."""
    if not content or not keywords:
        return 0.0
    documents = [row[0].content.casefold() for row in rows] or [content.casefold()]
    avg_len = sum(len(value) for value in documents) / len(documents)
    score = 0.0
    lowered = content.casefold()
    for keyword in keywords:
        term = keyword.casefold()
        tf = lowered.count(term)
        if not tf:
            continue
        df = sum(term in value for value in documents)
        idf = math.log(1 + (len(documents) - df + 0.5) / (df + 0.5))
        score += idf * (tf * (k1 + 1)) / (
            tf + k1 * (1 - b + b * len(lowered) / max(avg_len, 1))
        )
    return score


async def retrieve(
    db: AsyncSession,
    owner_id: str,
    query: str,
    kb_id: str | None = None,
    extra_queries: list[str] | None = None,
    query_vec: list[float] | None = None,
    keyword_enabled: bool = True,
    vector_enabled: bool = True,
    graph_enabled: bool = False,
    exact_vector_search: bool = False,
    metadata_filters: dict | None = None,
    rerank_enabled: bool | None = None,
    parent_child_enabled: bool | None = None,
) -> list[RetrievedChunk]:
    """对指定用户（可限定知识库）执行混合检索，返回 top_k 片段（附带来源文档名与页码）。

    - extra_queries：多查询扩展的变体，每个变体独立做一路向量召回，最后统一 RRF 融合；
    - query_vec：原查询向量（若调用方已算过——如语义缓存查过——传入避免重复编码）。
    """
    n = settings.retrieval_candidates
    filters = [
        WorkspaceMembership.user_id == owner_id,
        # A chunk may only be retrieved through the same knowledge base as its
        # authoritative parent document. This blocks inconsistent cross-KB rows.
        Chunk.kb_id == Document.kb_id,
        Chunk.index_version == Document.active_index_version,
        # Reindex/retry may keep a valid active version queryable. Only deletion
        # tombstones must disappear from retrieval immediately, before the async
        # object-storage cleanup hard-deletes the row and chunks.
        Document.status.notin_([DocStatus.deleting, DocStatus.deleted]),
        Document.quarantined.is_(False),  # 隔离区文档在管理员放行前不参与检索
    ]
    if kb_id:
        filters.append(Chunk.kb_id == kb_id)
    metadata_filters = metadata_filters or {}
    if metadata_filters.get("document_types"):
        filters.append(Chunk.content_type.in_(metadata_filters["document_types"]))
    if metadata_filters.get("sections"):
        filters.append(Chunk.section.in_(metadata_filters["sections"]))
    if metadata_filters.get("created_after"):
        filters.append(Document.created_at >= metadata_filters["created_after"])
    if metadata_filters.get("created_before"):
        filters.append(Document.created_at <= metadata_filters["created_before"])
    if metadata_filters.get("source_types"):
        filters.append(Document.source_type.in_(metadata_filters["source_types"]))
    if metadata_filters.get("departments"):
        filters.append(Document.department.in_(metadata_filters["departments"]))
    for tag in metadata_filters.get("tags") or []:
        filters.append(func.cast(Document.tags, Text).ilike(f'%"{tag}"%'))

    async def _vector_recall(vec) -> list:
        stmt = (
            select(Chunk, Document.filename, Chunk.embedding.cosine_distance(vec).label("dist"))
            .join(Document, Chunk.document_id == Document.id)
            .join(KnowledgeBase, KnowledgeBase.id == Chunk.kb_id)
            .join(
                WorkspaceMembership,
                WorkspaceMembership.workspace_id == KnowledgeBase.workspace_id,
            )
            .where(*filters)
            .order_by("dist")
            .limit(n)
        )
        return (await db.execute(stmt)).all()

    # ---- 向量召回：原查询 + 各扩展变体，各成一路（embedding 是 CPU 密集操作，丢线程池）----
    vector_routes: list[list] = []
    if vector_enabled:
        if exact_vector_search:
            await db.execute(sql_text("SET LOCAL enable_indexscan = off"))
            await db.execute(sql_text("SET LOCAL enable_bitmapscan = off"))
        if query_vec is None:
            query_vec = await asyncio.to_thread(embed_query, query)
        vector_routes = [await _vector_recall(query_vec)]
        for variant in extra_queries or []:
            variant_vec = await asyncio.to_thread(embed_query, variant)
            vector_routes.append(await _vector_recall(variant_vec))

    # ---- 关键词召回：PostgreSQL 全文检索（tsvector + GIN 倒排索引，ts_rank 排序）----
    # 只对原查询做（变体是语义级改写，关键词一路用原词更稳）。
    # 关键词检索是增强路径：失败时降级为纯向量召回，不中断整个问答。
    kw_rows = []
    if keyword_enabled:
        try:
            kw_rows = await _keyword_search(
                db,
                filters=filters,
                query=query,
                limit=n,
            )
        except Exception:
            logger.exception("keyword search failed, degrading to vector-only retrieval")
            kw_rows = []
    keywords = extract_keywords(query)
    title_rows = []
    if keywords:
        title_conditions = []
        for keyword in keywords[:4]:
            title_conditions.extend(
                (
                    Document.filename.ilike(f"%{keyword}%"),
                    Chunk.section.ilike(f"%{keyword}%"),
                    func.cast(Document.tags, Text).ilike(f'%"{keyword}"%'),
                )
            )
        title_stmt = (
            select(Chunk, Document.filename, literal(1.0).label("rank"))
            .join(Document, Chunk.document_id == Document.id)
            .join(KnowledgeBase, KnowledgeBase.id == Chunk.kb_id)
            .join(WorkspaceMembership, WorkspaceMembership.workspace_id == KnowledgeBase.workspace_id)
            .where(*filters, or_(*title_conditions))
            .limit(n)
        )
        try:
            title_rows = list((await db.execute(title_stmt)).all())
        except Exception:
            logger.warning("title recall failed", exc_info=True)
    graph_rows = []
    if graph_enabled and settings.graph_retrieval_enabled:
        try:
            graph_ids = await graph_recall_chunk_ids(
                db, keywords=keywords, kb_id=kb_id, limit=n
            )
            if graph_ids:
                graph_stmt = (
                    select(Chunk, Document.filename, literal(1.0).label("rank"))
                    .join(Document, Chunk.document_id == Document.id)
                    .join(KnowledgeBase, KnowledgeBase.id == Chunk.kb_id)
                    .join(WorkspaceMembership, WorkspaceMembership.workspace_id == KnowledgeBase.workspace_id)
                    .where(*filters, Chunk.id.in_(graph_ids))
                )
                loaded = list((await db.execute(graph_stmt)).all())
                order = {value: index for index, value in enumerate(graph_ids)}
                graph_rows = sorted(loaded, key=lambda row: order.get(row[0].id, n))
        except Exception:
            logger.warning("graph recall failed", exc_info=True)
    # ---- RRF 融合（所有向量路 + 关键词路）----
    by_id: dict[str, tuple[Chunk, str]] = {}
    all_rows = [row for route in vector_routes for row in route] + list(kw_rows) + title_rows + graph_rows
    for chunk, filename, _ in all_rows:
        by_id.setdefault(chunk.id, (chunk, filename))
    fused = rrf_fuse(
        [[row[0].id for row in route] for route in vector_routes]
        + [[row[0].id for row in kw_rows], [row[0].id for row in title_rows], [row[0].id for row in graph_rows]],
        k=settings.rrf_k,
    )
    ranked_ids = sorted(fused, key=fused.get, reverse=True)
    vector_similarity: dict[str, float] = {}
    for route in vector_routes:
        for chunk, _, distance in route:
            vector_similarity[chunk.id] = max(
                vector_similarity.get(chunk.id, 0.0),
                max(0.0, 1.0 - float(distance)),
            )
    keyword_ids = {row[0].id for row in kw_rows}

    use_parent = (
        settings.parent_child_enabled
        if parent_child_enabled is None
        else parent_child_enabled
    )
    results = [
        RetrievedChunk(
            chunk_id=cid,
            document_id=by_id[cid][0].document_id,
            filename=by_id[cid][1],
            seq=by_id[cid][0].seq,
            page=by_id[cid][0].page,
            content=(
                by_id[cid][0].parent_content
                if use_parent and by_id[cid][0].parent_content
                else by_id[cid][0].content
            ),
            score=round(fused[cid], 6),
            vector_similarity=round(vector_similarity.get(cid, 0.0), 6),
            keyword_hit=cid in keyword_ids,
            parent_seq=by_id[cid][0].parent_seq,
            section=by_id[cid][0].section,
            content_type=by_id[cid][0].content_type,
            source_url=by_id[cid][0].source_url,
            created_at=by_id[cid][0].created_at,
            kb_id=by_id[cid][0].kb_id,
        )
        for cid in ranked_ids
    ]
    if use_parent:
        unique_parents: list[RetrievedChunk] = []
        seen_parents: set[tuple[str, int]] = set()
        for result in results:
            key = (result.document_id, result.parent_seq)
            if key not in seen_parents:
                seen_parents.add(key)
                unique_parents.append(result)
        results = unique_parents
    elif settings.adjacent_chunk_window > 0 and results:
        conditions = [
            (Chunk.document_id == result.document_id)
            & (Chunk.seq >= max(0, result.seq - settings.adjacent_chunk_window))
            & (Chunk.seq <= result.seq + settings.adjacent_chunk_window)
            for result in results[: settings.retrieval_top_k]
        ]
        neighbor_stmt = (
            select(Chunk)
            .join(Document, Chunk.document_id == Document.id)
            .join(KnowledgeBase, KnowledgeBase.id == Chunk.kb_id)
            .join(WorkspaceMembership, WorkspaceMembership.workspace_id == KnowledgeBase.workspace_id)
            .where(*filters, or_(*conditions))
            .order_by(Chunk.document_id, Chunk.seq)
        )
        neighbors = list((await db.execute(neighbor_stmt)).scalars().unique())
        for result in results:
            nearby = [
                chunk.content
                for chunk in neighbors
                if chunk.document_id == result.document_id
                and abs(chunk.seq - result.seq) <= settings.adjacent_chunk_window
            ]
            if nearby:
                result.content = "\n".join(dict.fromkeys(nearby))

    # ----（可选）交叉编码器重排：对候选做精排，代价是延迟上升 ----
    # 重排是增强路径：模型加载/推理失败时降级为 RRF 排序，不让检索整体失败。
    use_reranker = settings.rerank_enabled if rerank_enabled is None else rerank_enabled
    if use_reranker and results:
        try:
            started = time.perf_counter()
            results = await asyncio.to_thread(_cross_encoder_rerank, query, results)
            RERANK_DURATION.labels("cross_encoder").observe(time.perf_counter() - started)
        except Exception:
            logger.warning("交叉编码器重排失败，降级为 RRF 排序", exc_info=True)

    if settings.rule_rerank_enabled and results:
        started = time.perf_counter()
        results = _rule_rerank(query, results)
        RERANK_DURATION.labels("rules").observe(time.perf_counter() - started)
    if settings.llm_rerank_enabled and results:
        from app.services.llm import llm_rerank

        started = time.perf_counter()
        results = await llm_rerank(query, results[: min(len(results), 20)])
        RERANK_DURATION.labels("llm").observe(time.perf_counter() - started)

    return results[: settings.retrieval_top_k]


def _rule_rerank(query: str, candidates: list[RetrievedChunk]) -> list[RetrievedChunk]:
    keywords = extract_keywords(query)
    seen: set[str] = set()
    for candidate in candidates:
        bonus = 0.0
        label = f"{candidate.filename} {candidate.section or ''}".casefold()
        bonus += 0.03 * sum(keyword.casefold() in label for keyword in keywords)
        bonus += 0.01 * sum(keyword.casefold() in candidate.content.casefold() for keyword in keywords)
        fingerprint = re.sub(r"\s+", "", candidate.content).casefold()
        if fingerprint in seen:
            bonus -= 0.25
        seen.add(fingerprint)
        candidate.score += bonus
    return sorted(candidates, key=lambda value: value.score, reverse=True)


_reranker = None


def _cross_encoder_rerank(query: str, candidates: list[RetrievedChunk]) -> list[RetrievedChunk]:
    global _reranker
    if _reranker is None:
        from sentence_transformers import CrossEncoder

        _reranker = CrossEncoder(settings.rerank_model, device="cpu")
    scores = _reranker.predict([(query, c.content) for c in candidates])
    for c, s in zip(candidates, scores):
        c.score = float(s)
    return sorted(candidates, key=lambda c: c.score, reverse=True)
