"""Agent 模式：基于 Function Calling 的多步工具调用循环（ReAct 风格）。

与 RAG 模式的区别：
- RAG 模式是固定管道：改写 → 检索一次 → 生成，检索策略由代码写死；
- Agent 模式把决策权交给模型：模型自主决定检索几次、每次用什么关键词、
  要不要先看文档清单，直到它认为信息足够才作答。
- 优势：复杂问题（对比类、多跳类，如"A 方案和 B 方案的价格差多少"）需要
  多次不同关键词的检索，固定管道一次检索往往覆盖不全；
- 代价：多轮 LLM 调用，延迟与成本上升——所以两种模式并存，由用户选择。

安全护栏：步数上限（agent_max_steps）防死循环；工具只读（检索/列清单），
模型无法通过工具修改任何数据。
"""
import json
import logging
from collections.abc import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Document, KnowledgeBase, WorkspaceMembership
from app.services.evidence import apply_injection_policy
from app.services.llm import chat_completion
from app.services.memory import build_memory_context
from app.services.retriever import RetrievedChunk, retrieve

logger = logging.getLogger(__name__)

AGENT_SYSTEM = """你是知识库智能助手，可以调用工具查资料后回答问题。

工作方式：
1. 先思考回答这个问题需要哪些信息，用 search_knowledge_base 检索；
2. 一次检索不够就换不同的关键词多检索几次（对比类问题要分别检索每个对象）；
3. 不确定知识库里有什么资料时，可用 list_documents 查看文档清单；
4. 信息足够后直接回答：严格基于检索到的资料，引用处标注 [n] 编号，
   资料不足要明说"知识库中没有找到相关信息"。用中文回答，可用 Markdown。"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": "在用户的知识库中检索相关内容。可多次调用，每次换不同的关键词或角度。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索查询，具体明确的短语效果最好"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_documents",
            "description": "列出知识库中所有可检索的文档名称，用于了解有哪些资料。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


async def run_agent(
    db: AsyncSession,
    owner_id: str,
    kb_id: str | None,
    question: str,
    history: list[dict] | None = None,
    long_term_memory: list[str] | None = None,
) -> AsyncIterator[dict]:
    """执行 Agent 循环，产出事件：
    {"type": "tool_call", "name": ..., "args": {...}}
    {"type": "tool_result", "name": ..., "summary": ...}
    {"type": "sources", "chunks": [RetrievedChunk, ...]}
    {"type": "delta", "text": ...}
    {"type": "usage", "prompt_tokens": ..., "completion_tokens": ...}
    """
    messages: list[dict] = [{"role": "system", "content": AGENT_SYSTEM}]
    memory_context = build_memory_context(long_term_memory or [])
    if memory_context:
        messages.append({"role": "system", "content": memory_context})
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": question})

    cited: dict[str, RetrievedChunk] = {}  # chunk_id -> chunk，跨多次检索全局编号
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0}

    for step in range(settings.agent_max_steps):
        # 最后一步不再给工具，强制模型收敛作答
        tools = TOOLS if step < settings.agent_max_steps - 1 else None
        resp = await chat_completion(messages, tools=tools)
        if resp.usage:
            total_usage["prompt_tokens"] += resp.usage.prompt_tokens or 0
            total_usage["completion_tokens"] += resp.usage.completion_tokens or 0
        msg = resp.choices[0].message

        if msg.tool_calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in msg.tool_calls
                    ],
                }
            )
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                yield {"type": "tool_call", "name": tc.function.name, "args": args}
                result = await _exec_tool(db, owner_id, kb_id, tc.function.name, args, cited)
                yield {"type": "tool_result", "name": tc.function.name, "summary": result[:300]}
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            continue

        # 无工具调用 → 最终回答
        yield {"type": "sources", "chunks": list(cited.values())}
        yield {"type": "delta", "text": msg.content or ""}
        yield {"type": "usage", **total_usage}
        return

    # 步数耗尽仍未作答（理论上被"最后一步无工具"兜住，防御性代码）
    yield {"type": "sources", "chunks": list(cited.values())}
    yield {"type": "delta", "text": "（已达到最大推理步数，请换个问法或缩小问题范围）"}
    yield {"type": "usage", **total_usage}


async def _exec_tool(
    db: AsyncSession,
    owner_id: str,
    kb_id: str | None,
    name: str,
    args: dict,
    cited: dict[str, RetrievedChunk],
) -> str:
    """执行工具（全部只读），返回给模型的文本结果。"""
    try:
        if name == "search_knowledge_base":
            query = str(args.get("query") or "").strip()
            if not query:
                return "错误：query 参数为空"
            chunks = await retrieve(db, owner_id, query, kb_id=kb_id)
            chunks, _injection = apply_injection_policy(chunks)
            if not chunks:
                return "没有检索到相关内容，可以换其他关键词再试。"
            lines = []
            for c in chunks:
                if c.chunk_id not in cited:
                    cited[c.chunk_id] = c
                idx = list(cited).index(c.chunk_id) + 1
                where = f"第 {c.page} 页" if c.page else f"第 {c.seq + 1} 段"
                lines.append(f"[{idx}]（{c.filename} {where}）\n{c.content}")
            return "\n\n".join(lines)

        if name == "list_documents":
            stmt = (
                select(Document)
                .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
                .join(
                    WorkspaceMembership,
                    WorkspaceMembership.workspace_id == KnowledgeBase.workspace_id,
                )
                .where(
                    WorkspaceMembership.user_id == owner_id,
                    Document.active_index_version.is_not(None),
                    Document.quarantined.is_(False),
                )
            )
            if kb_id:
                stmt = stmt.where(Document.kb_id == kb_id)
            docs = list((await db.execute(stmt)).scalars())
            if not docs:
                return "知识库中还没有可检索的文档。"
            return "可检索的文档：\n" + "\n".join(f"- {d.filename}（{d.chunk_count} 个片段）" for d in docs)

        return f"未知工具: {name}"
    except Exception as exc:  # 工具失败不终止整个循环，把错误告诉模型让它自行调整策略
        logger.warning("工具执行失败: %s", name, exc_info=True)
        return f"工具执行出错: {exc}"


__all__ = ["run_agent", "AGENT_SYSTEM", "TOOLS"]
