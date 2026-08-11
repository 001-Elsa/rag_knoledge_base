"""问答接口：RAG 固定管道 / Agent 工具循环，SSE 流式输出。

SSE 事件（按出现顺序）：
  event: cached       →  语义缓存命中（含相似度与原问题，后续直接推 sources+delta）
  event: rewrite      →  改写后的检索问题（仅多轮改写生效时）
  event: tool_call    →  Agent 调用工具（名称+参数）
  event: tool_result  →  工具返回摘要
  event: sources      →  引用来源数组
  event: delta        →  回答文本增量
  event: done         →  含 conversation_id 与本次用量
  event: suggestions  →  推荐追问（done 之后异步生成）
  event: error        →  错误信息
"""
import asyncio
import hashlib
import json
import logging
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import AsyncSessionLocal, get_db, set_tenant_context
from app.deps import get_current_user
from app.limiter import limiter
from app.metrics import (
    CHAT_CONCURRENCY_ACTIVE,
    CHAT_CONCURRENCY_REJECTED,
    CITATION_ENTAILMENT_FAILURES,
    CITATION_VALIDATION_FAILURES,
    CONTEXT_BUILD_DURATION,
    CONTEXT_CHUNKS,
    LLM_FIRST_TOKEN,
    LLM_GENERATION_DURATION,
    LLM_TOKEN_USAGE,
    NO_ANSWER_TOTAL,
    QA_TOTAL,
    RAG_END_TO_END_DURATION,
    RAG_FEEDBACK,
    RAG_QUERY_ROUTES,
    RAG_USER_INTERACTIONS,
    RETRIEVAL_DURATION,
    SEMANTIC_CACHE_HITS,
    SEMANTIC_CACHE_LOOKUPS,
)
from app.models import AnswerFeedback, Conversation, Message, UsageRecord, User
from app.schemas import (
    ChatRequest,
    ConversationOut,
    ConversationRenameRequest,
    FeedbackRequest,
    InteractionRequest,
    MessageOut,
)
from app.services import history as history_svc
from app.services import memory as memory_svc
from app.services import semantic_cache, summarizer
from app.services.agent import run_agent
from app.services.audit import add_audit_event
from app.services.concurrency import (
    acquire_chat_slot,
    keep_chat_slot_alive,
    release_chat_slot,
)
from app.services.context import prepare_context
from app.services.embedder import embed_query
from app.services.evidence import (
    apply_injection_policy,
    assess_evidence,
    repair_citations,
    validate_citations,
    verify_claim_entailment,
)
from app.services.llm import (
    expand_queries,
    generate_hypothetical_answer,
    rewrite_query,
    stream_answer,
    suggest_followups,
)
from app.services.permissions import get_kb_with_permission
from app.services.query import clean_query, route_query
from app.services.retriever import extract_keywords, retrieve

router = APIRouter(prefix="/api/chat", tags=["问答"])
logger = logging.getLogger(__name__)


async def _chat_capacity(
    body: ChatRequest,
    user: User = Depends(get_current_user),
):
    if not body.kb_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "请选择一个具体知识库后再提问",
        )
    lease = await acquire_chat_slot(user.id, body.conversation_id)
    if lease is None:
        CHAT_CONCURRENCY_REJECTED.labels(
            "conversation" if body.conversation_id else "user_or_global"
        ).inc()
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "当前并发请求较多，请稍后重试",
            headers={"Retry-After": "2"},
        )
    return lease


async def _capacity_stream(stream, lease):
    """Hold capacity for the actual SSE lifetime, including client disconnects."""
    heartbeat = asyncio.create_task(keep_chat_slot_alive(lease))
    CHAT_CONCURRENCY_ACTIVE.inc()
    try:
        async for event in stream:
            yield event
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass
        await release_chat_slot(lease)
        CHAT_CONCURRENCY_ACTIVE.dec()


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _chunk_to_source(c) -> dict:
    return {
        "kb_id": getattr(c, "kb_id", None),
        "document_id": c.document_id,
        "filename": c.filename,
        "seq": c.seq,
        "page": c.page,
        "content": c.content,
        "score": c.score,
        "vector_similarity": c.vector_similarity,
        "keyword_hit": c.keyword_hit,
        "section": c.section,
        "content_type": c.content_type,
        "source_url": c.source_url,
        "chunk_id": c.chunk_id,
        "indexed_at": c.created_at.isoformat() if getattr(c.created_at, "isoformat", None) else None,
    }


def _scope_chunks_to_kb(chunks: list, kb_id: str) -> list:
    scoped = [chunk for chunk in chunks if getattr(chunk, "kb_id", None) == kb_id]
    dropped = len(chunks) - len(scoped)
    if dropped:
        logger.error(
            "dropped cross-knowledge-base chunks from chat context",
            extra={"kb_id": kb_id, "dropped_chunks": dropped},
        )
    return scoped


def _build_grounded_fallback(chunks: list, *, limit: int = 5) -> str:
    """Build a citation-bound extractive answer without model synthesis."""
    lines = [
        "> ⚠️ 大模型暂时不可用。以下内容直接摘自当前知识库的检索结果，未添加文件外信息。",
        "",
    ]
    for index, chunk in enumerate(chunks[:limit], start=1):
        document_name = (
            getattr(chunk, "filename", None)
            or getattr(chunk, "document_name", None)
            or "知识库文档"
        )
        section = getattr(chunk, "section", None)
        label = f"{document_name} · {section}" if section else document_name
        excerpt = " ".join(str(getattr(chunk, "content", "")).split())
        if len(excerpt) > 420:
            excerpt = excerpt[:419].rstrip() + "…"
        lines.append(f"- **{label}**：{excerpt} [{index}]")
    return "\n".join(lines)


def _has_substantive_answer(answer: str) -> bool:
    """Reject citation-only or Markdown-only output as an empty answer."""
    without_citations = re.sub(r"\[\d+]", "", answer)
    plain_text = re.sub(r"[#>*_`\-\s]", "", without_citations)
    return len(plain_text) >= 12


def _cache_hit_is_grounded(cache_hit: dict, kb_id: str) -> bool:
    """Accept cached answers only when content and every source match this KB."""
    sources = cache_hit.get("sources") or []
    return (
        _has_substantive_answer(str(cache_hit.get("answer") or ""))
        and bool(sources)
        and all(source.get("kb_id") == kb_id for source in sources)
    )


async def _persist_round(conv_id: str, user_id: str, question: str, answer: str,
                         sources: list[dict], usage: dict, first_token_ms: int, total_ms: int,
                         record_usage: bool = True) -> None:
    """流式结束后落库（独立 session：流式期间请求级 session 可能已被框架回收）。"""
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, user_id)
        rows = [
            Message(conversation_id=conv_id, role="user", content=question),
            Message(conversation_id=conv_id, role="assistant", content=answer,
                    sources=json.dumps(sources, ensure_ascii=False)),
        ]
        if record_usage:
            rows.append(
                UsageRecord(
                    owner_id=user_id, conversation_id=conv_id, model=settings.llm_model,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    first_token_ms=first_token_ms, total_ms=total_ms,
                    estimated_cost_microusd=int(round(
                        usage.get("prompt_tokens", 0) * settings.llm_input_cost_per_million
                        + usage.get("completion_tokens", 0) * settings.llm_output_cost_per_million
                    )),
                )
            )
        session.add_all(rows)
        await session.commit()
    await history_svc.append_history(conv_id, "user", question)
    await history_svc.append_history(conv_id, "assistant", answer)
    await summarizer.maybe_summarize(
        conv_id, user_id
    )  # 长对话滚动摘要（未到阈值时立即返回）


@router.post("")
@limiter.limit(settings.rate_limit_chat)
async def chat(
    request: Request,
    body: ChatRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _capacity=Depends(_chat_capacity),
):
    request_started = time.perf_counter()
    # ---- 会话与知识库校验 ----
    try:
        selected_kb = await get_kb_with_permission(db, body.kb_id, user.id, "query")
    except HTTPException:
        await release_chat_slot(_capacity)
        raise

    if body.conversation_id:
        conv = (
            await db.execute(select(Conversation).where(Conversation.id == body.conversation_id))
        ).scalar_one_or_none()
        if conv is None or conv.owner_id != user.id:
            await release_chat_slot(_capacity)
            raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
        if conv.kb_id != body.kb_id:
            await release_chat_slot(_capacity)
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "该对话属于另一个知识库，请新建对话后再提问",
            )
        conv_id = conv.id
        conv_summary = conv.summary  # 长对话滚动摘要（可能为 None）
    else:
        conv = Conversation(owner_id=user.id, kb_id=body.kb_id, title=body.question[:50])
        db.add(conv)
        await db.commit()
        conv_id = conv.id
        conv_summary = None

    history = await history_svc.load_history(db, conv_id)

    if body.mode == "agent":
        return StreamingResponse(
            _capacity_stream(
                _agent_stream(user, body, conv_id, history), _capacity
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ================= RAG 模式 =================
    # 1. 清洗、术语规范化、知识库/检索模式路由，再做多轮独立问题改写。
    knowledge_bases = [selected_kb]
    route = route_query(
        body.question,
        requested_profile=body.retrieval_profile,
        knowledge_bases=knowledge_bases,
    )
    effective_kb_id = body.kb_id
    profile = route.retrieval_mode
    RAG_QUERY_ROUTES.labels(profile, route.reason).inc()
    search_query = route.query
    if settings.query_rewrite_enabled and history:
        search_query = await rewrite_query(body.question, history)
        search_query = clean_query(search_query)

    if not route.needs_retrieval:
        async def social_stream():
            answer = "你好！请告诉我你想查询的知识库问题，我会基于可验证的资料回答并给出引用。"
            yield _sse("route", {"profile": "none", "reason": route.reason, "kb_id": effective_kb_id})
            yield _sse("delta", {"text": answer})
            await _persist_round(conv_id, user.id, body.question, answer, [], {}, 0, 0, record_usage=False)
            yield _sse("done", {"conversation_id": conv_id, "usage": {"prompt_tokens": 0, "completion_tokens": 0, "first_token_ms": 0, "total_ms": 0}})
        return StreamingResponse(_capacity_stream(social_stream(), _capacity), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # 2. 查询向量化（语义缓存与向量检索共用，只算一次）
    query_vec = await asyncio.to_thread(embed_query, search_query)

    # 3. 语义缓存：相似问题直接秒回，跳过检索与生成
    cache_hit = None
    if semantic_cache.is_eligible(history):
        SEMANTIC_CACHE_LOOKUPS.inc()
        cache_hit = await semantic_cache.lookup(user.id, effective_kb_id, query_vec)
    if cache_hit is not None and not _cache_hit_is_grounded(cache_hit, effective_kb_id):
        logger.warning(
            "discarded empty or cross-knowledge-base semantic cache hit",
            extra={"kb_id": effective_kb_id},
        )
        cache_hit = None
    if cache_hit is not None:
        async def cached_stream():
            QA_TOTAL.inc()
            SEMANTIC_CACHE_HITS.inc()
            yield _sse("cached", {"similarity": cache_hit["similarity"], "matched_question": cache_hit["question"]})
            if search_query != body.question:
                yield _sse("rewrite", {"query": search_query})
            yield _sse("sources", cache_hit["sources"])
            yield _sse("delta", {"text": cache_hit["answer"]})
            await _persist_round(conv_id, user.id, body.question, cache_hit["answer"],
                                 cache_hit["sources"], {}, 0, 0, record_usage=False)
            yield _sse("done", {"conversation_id": conv_id, "cached": True,
                                "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                                          "first_token_ms": 0, "total_ms": 0}})
        return StreamingResponse(_capacity_stream(cached_stream(), _capacity), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # 4. 混合检索（可选多查询扩展）
    use_multi_query = profile == "multi_query" or settings.multi_query_enabled
    extra_queries = await expand_queries(search_query) if use_multi_query and settings.query_expansion_enabled else []
    if history:
        current_terms = set(extract_keywords(search_query))
        for message in reversed(history[-10:]):
            if message.get("role") != "user":
                continue
            previous = clean_query(message.get("content", ""))
            if previous and current_terms.intersection(extract_keywords(previous)):
                extra_queries.append(previous[:300])
                break
    if profile == "hyde" or settings.hyde_enabled:
        hypothetical = await generate_hypothetical_answer(search_query)
        if hypothetical:
            extra_queries.append(hypothetical)
    t_retrieval = time.perf_counter()
    chunks = await retrieve(db, user.id, search_query, kb_id=effective_kb_id,
                            extra_queries=extra_queries, query_vec=query_vec,
                            keyword_enabled=profile != "vector",
                            vector_enabled=profile != "keyword",
                            exact_vector_search=profile == "flat",
                            graph_enabled=profile == "graph",
                            metadata_filters={
                                "document_types": body.document_types,
                                "source_types": body.source_types,
                                "departments": body.departments,
                                "tags": body.tags,
                                "sections": body.sections,
                                "created_after": body.created_after,
                                "created_before": body.created_before,
                            },
                            rerank_enabled=profile == "hybrid_rerank",
                            parent_child_enabled=profile == "parent_child")
    RETRIEVAL_DURATION.observe(time.perf_counter() - t_retrieval)
    chunks = _scope_chunks_to_kb(chunks, effective_kb_id)
    chunks, injection_info = apply_injection_policy(chunks)
    chunks = _scope_chunks_to_kb(chunks, effective_kb_id)
    context_started = time.perf_counter()
    chunks = prepare_context(chunks, search_query)
    chunks = _scope_chunks_to_kb(chunks, effective_kb_id)
    CONTEXT_BUILD_DURATION.observe(time.perf_counter() - context_started)
    CONTEXT_CHUNKS.observe(len(chunks))
    sources = [_chunk_to_source(c) for c in chunks]
    evidence = assess_evidence(chunks)
    add_audit_event(
        db,
        action="knowledge_base.query",
        resource_type="knowledge_base",
        resource_id=effective_kb_id,
        actor_user_id=user.id,
        workspace_id=selected_kb.workspace_id,
        request=request,
        after={
            "question_hash": hashlib.sha256(body.question.encode()).hexdigest(),
            "route": profile,
            "source_count": len(sources),
            "answerable": evidence.answerable,
        },
    )
    await db.commit()

    async def event_stream():
        QA_TOTAL.inc()
        yield _sse("route", {"profile": profile, "reason": route.reason, "kb_id": effective_kb_id})
        if search_query != body.question:
            yield _sse("rewrite", {"query": search_query})
        yield _sse("sources", sources)
        yield _sse("evidence", evidence.as_dict())
        if injection_info.get("high_risk_count"):
            yield _sse("injection", injection_info)

        if not evidence.answerable:
            NO_ANSWER_TOTAL.inc()
            answer = "知识库中没有找到足够可靠的证据，暂时无法回答这个问题。"
            yield _sse("delta", {"text": answer})
            await _persist_round(
                conv_id,
                user.id,
                body.question,
                answer,
                sources,
                {},
                0,
                0,
                record_usage=False,
            )
            yield _sse(
                "done",
                {
                    "conversation_id": conv_id,
                    "no_answer": True,
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "first_token_ms": 0,
                        "total_ms": 0,
                    },
                },
            )
            return

        answer_parts: list[str] = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        t_start = time.perf_counter()
        first_token_ms = 0
        try:
            async for event in stream_answer(
                body.question,
                chunks,
                history,
                summary=conv_summary,
                response_format=body.response_format,
                response_schema=body.response_schema,
                knowledge_base_name=selected_kb.name,
            ):
                if event["type"] == "delta":
                    if not answer_parts:
                        first_token_ms = int((time.perf_counter() - t_start) * 1000)
                        LLM_FIRST_TOKEN.observe(first_token_ms / 1000)
                    answer_parts.append(event["text"])
                    if body.response_format != "json":
                        yield _sse("delta", {"text": event["text"]})
                elif event["type"] == "usage":
                    usage = event
        except Exception as exc:
            logger.warning(
                "LLM generation failed; returning tenant-scoped evidence fallback",
                extra={"kb_id": effective_kb_id, "source_count": len(chunks)},
                exc_info=True,
            )
            if body.response_format == "json":
                yield _sse("error", {"message": f"生成失败: {exc}"})
                return
            fallback = _build_grounded_fallback(chunks)
            answer_parts = [fallback]
            # Clear any partial model tokens and replace them with cited evidence.
            yield _sse("replacement", {"text": fallback})

        answer = "".join(answer_parts)
        if body.response_format == "json":
            try:
                structured = json.loads(answer)
                if body.response_schema:
                    from jsonschema import validate as validate_json_schema

                    validate_json_schema(structured, body.response_schema)
                yield _sse("delta", {"text": answer})
            except Exception:
                yield _sse("error", {"message": "模型返回内容不符合 JSON 结构"})
                return
        total_ms = int((time.perf_counter() - t_start) * 1000)
        LLM_GENERATION_DURATION.observe(total_ms / 1000)
        validation = validate_citations(answer, len(sources))
        entailment = {"checked": False, "supported": True, "unsupported_claims": []}
        if validation["valid"] and settings.citation_entailment_enabled:
            entailment = await verify_claim_entailment(
                answer, [c.content for c in chunks]
            )
            validation = {
                **validation,
                "entailment": entailment,
                "valid": validation["valid"]
                and (entailment["supported"] if entailment["checked"] else entailment["supported"]),
            }
            if entailment.get("checked") and not entailment.get("supported"):
                CITATION_ENTAILMENT_FAILURES.inc()
        persisted_answer = answer
        if not validation["valid"]:
            repair = repair_citations(answer, len(sources))
            if repair["validation"]["valid"]:
                persisted_answer = repair["answer"]
                validation = {
                    **repair["validation"],
                    "repaired": True,
                    "removed_lines": repair["removed_lines"],
                }
            else:
                CITATION_VALIDATION_FAILURES.inc()
                persisted_answer = _build_grounded_fallback(chunks)
                validation = {
                    **validate_citations(persisted_answer, len(sources)),
                    "fallback": "extractive",
                    "fallback_reason": "citation_validation_failed",
                }
        if not _has_substantive_answer(persisted_answer):
            persisted_answer = _build_grounded_fallback(chunks)
            validation = {
                **validate_citations(persisted_answer, len(sources)),
                "fallback": "extractive",
                "fallback_reason": "empty_or_citation_only_answer",
            }
        yield _sse("validation", validation)
        if persisted_answer != answer:
            yield _sse("replacement", {"text": persisted_answer})
        await _persist_round(conv_id, user.id, body.question, persisted_answer, sources,
                             usage, first_token_ms, total_ms)
        LLM_TOKEN_USAGE.labels(settings.llm_model, "prompt").inc(
            usage.get("prompt_tokens", 0)
        )
        LLM_TOKEN_USAGE.labels(settings.llm_model, "completion").inc(
            usage.get("completion_tokens", 0)
        )
        if semantic_cache.is_eligible(history) and validation["valid"]:
            await semantic_cache.store(user.id, effective_kb_id, body.question, query_vec, persisted_answer, sources)

        yield _sse("done", {"conversation_id": conv_id,
                            "usage": {**usage, "first_token_ms": first_token_ms, "total_ms": total_ms}})
        RAG_END_TO_END_DURATION.observe(time.perf_counter() - request_started)

        if settings.suggestions_enabled and validation["valid"] and persisted_answer:
            followups = await suggest_followups(body.question, persisted_answer)
            if followups:
                yield _sse("suggestions", {"questions": followups})

    return StreamingResponse(_capacity_stream(event_stream(), _capacity), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _agent_stream(user: User, body: ChatRequest, conv_id: str, history: list[dict]):
    """Agent 模式事件流：工具调用过程实时推送。"""
    QA_TOTAL.inc()
    answer_parts: list[str] = []
    sources: list[dict] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    t_start = time.perf_counter()
    first_token_ms = 0
    try:
        # FastAPI 0.115 finalizes request dependencies before a StreamingResponse
        # finishes, so agent tools use a dedicated tenant-bound session.
        async with AsyncSessionLocal() as agent_db:
            await set_tenant_context(agent_db, user.id)
            memories = await memory_svc.load_agent_memories(
                agent_db,
                owner_id=user.id,
                kb_id=body.kb_id,
                current_conversation_id=conv_id,
                question=body.question,
            )
            if memories:
                yield _sse("memory", {"count": len(memories)})
            async for event in run_agent(
                agent_db,
                user.id,
                body.kb_id,
                body.question,
                history,
                long_term_memory=memories,
            ):
                if event["type"] in ("tool_call", "tool_result"):
                    yield _sse(
                        event["type"],
                        {key: value for key, value in event.items() if key != "type"},
                    )
                elif event["type"] == "sources":
                    scoped_chunks = _scope_chunks_to_kb(event["chunks"], body.kb_id)
                    sources = [_chunk_to_source(c) for c in scoped_chunks]
                    yield _sse("sources", sources)
                elif event["type"] == "delta":
                    if not answer_parts:
                        first_token_ms = int((time.perf_counter() - t_start) * 1000)
                        LLM_FIRST_TOKEN.observe(first_token_ms / 1000)
                    answer_parts.append(event["text"])
                    yield _sse("delta", {"text": event["text"]})
                elif event["type"] == "usage":
                    usage = {
                        "prompt_tokens": event["prompt_tokens"],
                        "completion_tokens": event["completion_tokens"],
                    }
    except Exception as exc:
        yield _sse("error", {"message": f"Agent 执行失败: {exc}"})
        return

    answer = "".join(answer_parts)
    total_ms = int((time.perf_counter() - t_start) * 1000)
    validation = validate_citations(answer, len(sources))
    if validation["valid"] and settings.citation_entailment_enabled and sources:
        entailment = await verify_claim_entailment(
            answer, [s["content"] for s in sources]
        )
        validation = {
            **validation,
            "entailment": entailment,
            "valid": validation["valid"]
            and (entailment["supported"] if entailment["checked"] else entailment["supported"]),
        }
        if entailment.get("checked") and not entailment.get("supported"):
            CITATION_ENTAILMENT_FAILURES.inc()
    persisted_answer = answer
    if not validation["valid"]:
        repair = repair_citations(answer, len(sources))
        if repair["validation"]["valid"]:
            persisted_answer = repair["answer"]
            validation = {
                **repair["validation"],
                "repaired": True,
                "removed_lines": repair["removed_lines"],
            }
        else:
            CITATION_VALIDATION_FAILURES.inc()
            persisted_answer = "回答未通过引用一致性校验，已阻止展示。请缩小问题范围后重试。"
    yield _sse("validation", validation)
    if persisted_answer != answer:
        yield _sse("replacement", {"text": persisted_answer})
    await _persist_round(
        conv_id,
        user.id,
        body.question,
        persisted_answer,
        sources,
        usage,
        first_token_ms,
        total_ms,
    )
    yield _sse("done", {"conversation_id": conv_id,
                        "usage": {**usage, "first_token_ms": first_token_ms, "total_ms": total_ms}})

    if settings.suggestions_enabled and validation["valid"] and persisted_answer:
        followups = await suggest_followups(body.question, persisted_answer)
        if followups:
            yield _sse("suggestions", {"questions": followups})


@router.post("/feedback", status_code=status.HTTP_201_CREATED)
async def submit_feedback(
    body: FeedbackRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conversation = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == body.conversation_id,
                Conversation.owner_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    if body.rating not in {-1, 1}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "rating 只能是 -1 或 1")
    if body.message_id:
        exists = (
            await db.execute(
                select(Message.id).where(
                    Message.id == body.message_id,
                    Message.conversation_id == conversation.id,
                    Message.role == "assistant",
                )
            )
        ).scalar_one_or_none()
        if not exists:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "回答消息不存在")
    feedback = AnswerFeedback(
        owner_id=user.id,
        conversation_id=conversation.id,
        message_id=body.message_id,
        rating=body.rating,
        reason=body.reason,
        comment=body.comment,
    )
    db.add(feedback)
    add_audit_event(
        db,
        action="answer.feedback",
        resource_type="conversation",
        resource_id=conversation.id,
        actor_user_id=user.id,
        request=request,
        after={"rating": body.rating, "reason": body.reason},
    )
    await db.commit()
    RAG_FEEDBACK.labels(str(body.rating), body.reason or "unspecified").inc()
    return {"id": feedback.id, "accepted": True}


@router.post("/interactions", status_code=status.HTTP_202_ACCEPTED)
async def record_interaction(
    body: InteractionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.conversation_id:
        await _owned_conversation(db, body.conversation_id, user.id)
    RAG_USER_INTERACTIONS.labels(body.action).inc()
    return {"accepted": True}

# ---------------- 会话管理 ----------------
@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    stmt = (
        select(Conversation)
        .where(Conversation.owner_id == user.id)
        .order_by(Conversation.created_at.desc())
        .limit(50)
    )
    return list((await db.execute(stmt)).scalars())


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _owned_conversation(db, conversation_id, user.id)
    stmt = select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at)
    return list((await db.execute(stmt)).scalars())


@router.patch("/conversations/{conversation_id}", response_model=ConversationOut)
async def rename_conversation(
    conversation_id: str,
    body: ConversationRenameRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = await _owned_conversation(db, conversation_id, user.id)
    conv.title = body.title
    await db.commit()
    await db.refresh(conv)
    return conv


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = await _owned_conversation(db, conversation_id, user.id)
    await db.delete(conv)
    await db.commit()


async def _owned_conversation(db: AsyncSession, conversation_id: str, owner_id: str) -> Conversation:
    conv = (
        await db.execute(select(Conversation).where(Conversation.id == conversation_id))
    ).scalar_one_or_none()
    if conv is None or conv.owner_id != owner_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return conv
