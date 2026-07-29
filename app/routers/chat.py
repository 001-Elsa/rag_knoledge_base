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
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import AsyncSessionLocal, get_db
from app.deps import get_current_user
from app.limiter import limiter
from app.metrics import (
    CITATION_ENTAILMENT_FAILURES,
    CITATION_VALIDATION_FAILURES,
    LLM_FIRST_TOKEN,
    LLM_TOKEN_USAGE,
    NO_ANSWER_TOTAL,
    QA_TOTAL,
    RETRIEVAL_DURATION,
    SEMANTIC_CACHE_HITS,
)
from app.models import Conversation, Message, UsageRecord, User
from app.schemas import ChatRequest, ConversationOut, ConversationRenameRequest, MessageOut
from app.services import history as history_svc
from app.services import semantic_cache, summarizer
from app.services.agent import run_agent
from app.services.embedder import embed_query
from app.services.evidence import (
    apply_injection_policy,
    assess_evidence,
    validate_citations,
    verify_claim_entailment,
)
from app.services.llm import expand_queries, rewrite_query, stream_answer, suggest_followups
from app.services.permissions import get_kb_with_permission
from app.services.retriever import retrieve

router = APIRouter(prefix="/api/chat", tags=["问答"])


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _chunk_to_source(c) -> dict:
    return {
        "document_id": c.document_id,
        "filename": c.filename,
        "seq": c.seq,
        "page": c.page,
        "content": c.content,
        "score": c.score,
        "vector_similarity": c.vector_similarity,
        "keyword_hit": c.keyword_hit,
    }


async def _persist_round(conv_id: str, user_id: str, question: str, answer: str,
                         sources: list[dict], usage: dict, first_token_ms: int, total_ms: int,
                         record_usage: bool = True) -> None:
    """流式结束后落库（独立 session：流式期间请求级 session 可能已被框架回收）。"""
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT set_config('app.user_id', :user_id, true)"),
            {"user_id": user_id},
        )
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
):
    # ---- 会话与知识库校验 ----
    if body.kb_id:
        await get_kb_with_permission(db, body.kb_id, user.id, "query")

    if body.conversation_id:
        conv = (
            await db.execute(select(Conversation).where(Conversation.id == body.conversation_id))
        ).scalar_one_or_none()
        if conv is None or conv.owner_id != user.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
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
            _agent_stream(db, user, body, conv_id, history),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ================= RAG 模式 =================
    # 1. 查询改写（多轮时把省略指代补全，用于检索；Prompt 里仍用原问题）
    search_query = body.question
    if settings.query_rewrite_enabled and history:
        search_query = await rewrite_query(body.question, history)

    # 2. 查询向量化（语义缓存与向量检索共用，只算一次）
    query_vec = await asyncio.to_thread(embed_query, search_query)

    # 3. 语义缓存：相似问题直接秒回，跳过检索与生成
    cache_hit = None
    if semantic_cache.is_eligible(history):
        cache_hit = await semantic_cache.lookup(user.id, body.kb_id, query_vec)
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
        return StreamingResponse(cached_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # 4. 混合检索（可选多查询扩展）
    use_multi_query = (
        body.retrieval_profile == "multi_query" or settings.multi_query_enabled
    )
    extra_queries = await expand_queries(search_query) if use_multi_query else []
    t_retrieval = time.perf_counter()
    chunks = await retrieve(db, user.id, search_query, kb_id=body.kb_id,
                            extra_queries=extra_queries, query_vec=query_vec,
                            keyword_enabled=body.retrieval_profile != "vector",
                            rerank_enabled=body.retrieval_profile == "hybrid_rerank",
                            parent_child_enabled=body.retrieval_profile == "parent_child")
    RETRIEVAL_DURATION.observe(time.perf_counter() - t_retrieval)
    chunks, injection_info = apply_injection_policy(chunks)
    sources = [_chunk_to_source(c) for c in chunks]
    evidence = assess_evidence(chunks)

    async def event_stream():
        QA_TOTAL.inc()
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
            async for event in stream_answer(body.question, chunks, history, summary=conv_summary):
                if event["type"] == "delta":
                    if not answer_parts:
                        first_token_ms = int((time.perf_counter() - t_start) * 1000)
                        LLM_FIRST_TOKEN.observe(first_token_ms / 1000)
                    answer_parts.append(event["text"])
                    yield _sse("delta", {"text": event["text"]})
                elif event["type"] == "usage":
                    usage = event
        except Exception as exc:
            yield _sse("error", {"message": f"生成失败: {exc}"})
            return

        answer = "".join(answer_parts)
        total_ms = int((time.perf_counter() - t_start) * 1000)
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
            CITATION_VALIDATION_FAILURES.inc()
            persisted_answer = (
                "回答未通过引用一致性校验，已阻止展示。请缩小问题范围后重试。"
            )
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
            await semantic_cache.store(user.id, body.kb_id, body.question, query_vec, persisted_answer, sources)

        yield _sse("done", {"conversation_id": conv_id,
                            "usage": {**usage, "first_token_ms": first_token_ms, "total_ms": total_ms}})

        if settings.suggestions_enabled and validation["valid"] and persisted_answer:
            followups = await suggest_followups(body.question, persisted_answer)
            if followups:
                yield _sse("suggestions", {"questions": followups})

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _agent_stream(db: AsyncSession, user: User, body: ChatRequest, conv_id: str, history: list[dict]):
    """Agent 模式事件流：工具调用过程实时推送。"""
    QA_TOTAL.inc()
    answer_parts: list[str] = []
    sources: list[dict] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    t_start = time.perf_counter()
    first_token_ms = 0
    try:
        async for event in run_agent(db, user.id, body.kb_id, body.question, history):
            if event["type"] in ("tool_call", "tool_result"):
                yield _sse(event["type"], {k: v for k, v in event.items() if k != "type"})
            elif event["type"] == "sources":
                sources = [_chunk_to_source(c) for c in event["chunks"]]
                yield _sse("sources", sources)
            elif event["type"] == "delta":
                if not answer_parts:
                    first_token_ms = int((time.perf_counter() - t_start) * 1000)
                    LLM_FIRST_TOKEN.observe(first_token_ms / 1000)
                answer_parts.append(event["text"])
                yield _sse("delta", {"text": event["text"]})
            elif event["type"] == "usage":
                usage = {"prompt_tokens": event["prompt_tokens"], "completion_tokens": event["completion_tokens"]}
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
