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
import re
from dataclasses import dataclass

import jieba
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Chunk, Document, KnowledgeBase, WorkspaceMembership
from app.services.embedder import embed_query

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


async def retrieve(
    db: AsyncSession,
    owner_id: str,
    query: str,
    kb_id: str | None = None,
    extra_queries: list[str] | None = None,
    query_vec: list[float] | None = None,
    keyword_enabled: bool = True,
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
        Chunk.index_version == Document.active_index_version,
        Document.quarantined.is_(False),  # 隔离区文档在管理员放行前不参与检索
    ]
    if kb_id:
        filters.append(Chunk.kb_id == kb_id)

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
    if query_vec is None:
        query_vec = await asyncio.to_thread(embed_query, query)
    vector_routes: list[list] = [await _vector_recall(query_vec)]
    for variant in extra_queries or []:
        variant_vec = await asyncio.to_thread(embed_query, variant)
        vector_routes.append(await _vector_recall(variant_vec))

    # ---- 关键词召回：PostgreSQL 全文检索（tsvector + GIN 倒排索引，ts_rank 排序）----
    # 只对原查询做（变体是语义级改写，关键词一路用原词更稳）
    kw_rows = []
    tsquery_expr = build_tsquery(extract_keywords(query))
    if keyword_enabled and tsquery_expr:
        tsq = func.to_tsquery("simple", tsquery_expr)
        rank = func.ts_rank(Chunk.content_tokens, tsq).label("rank")
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
            .limit(n)
        )
        kw_rows = (await db.execute(kw_stmt)).all()

    # ---- RRF 融合（所有向量路 + 关键词路）----
    by_id: dict[str, tuple[Chunk, str]] = {}
    for chunk, filename, _ in [row for route in vector_routes for row in route] + list(kw_rows):
        by_id.setdefault(chunk.id, (chunk, filename))
    fused = rrf_fuse(
        [[row[0].id for row in route] for route in vector_routes] + [[row[0].id for row in kw_rows]],
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

    # ----（可选）交叉编码器重排：对候选做精排，代价是延迟上升 ----
    # 重排是增强路径：模型加载/推理失败时降级为 RRF 排序，不让检索整体失败。
    use_reranker = settings.rerank_enabled if rerank_enabled is None else rerank_enabled
    if use_reranker and results:
        try:
            results = await asyncio.to_thread(_cross_encoder_rerank, query, results)
        except Exception:
            logger.warning("交叉编码器重排失败，降级为 RRF 排序", exc_info=True)

    return results[: settings.retrieval_top_k]


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
