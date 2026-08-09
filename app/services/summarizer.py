"""长对话滚动摘要：控制多轮对话的上下文长度与成本。

问题：对话到几十轮后，把全部历史塞进 Prompt 会 token 爆炸（贵且可能超上下文窗口），
只带最近几轮又会"失忆"（用户 20 轮前说过的关键信息丢了）。

方案（滚动摘要）：
- Prompt = 摘要（覆盖旧轮次）+ 最近 N 轮原文；
- 消息总数超过阈值后，把「旧摘要 + 新增的旧轮次」交给 LLM 合并压缩成新摘要；
- summary_upto 记录摘要已覆盖到第几条消息，每次只增量压缩新超出的部分。

失败处理：摘要是增强，不是必需——失败静默跳过，下轮再试。
"""
import logging

from sqlalchemy import select

from app.config import settings
from app.db import AsyncSessionLocal, set_tenant_context
from app.models import Conversation, Message
from app.services.llm import chat_completion

logger = logging.getLogger(__name__)

SUMMARY_PROMPT = """把对话内容压缩成客观的要点摘要（200 字以内）：
- 保留：用户关心的主题、已给出的关键结论、用户明确表达的偏好和约束；
- 丢弃：寒暄、重复内容、具体的引用编号；
- 如果提供了已有摘要，把新内容合并进去输出完整的新摘要。
只输出摘要本身。"""


async def maybe_summarize(conversation_id: str, user_id: str) -> None:
    """检查并（在需要时）更新会话摘要。独立 session，可在流式响应结束后调用。"""
    if not settings.history_summary_enabled:
        return
    try:
        async with AsyncSessionLocal() as db:
            await set_tenant_context(db, user_id)
            conv = await db.get(Conversation, conversation_id)
            if conv is None:
                return
            messages = list(
                (
                    await db.execute(
                        select(Message)
                        .where(Message.conversation_id == conversation_id)
                        .order_by(Message.created_at)
                    )
                ).scalars()
            )
            total = len(messages)
            boundary = total - settings.summary_keep_recent
            if total < settings.summary_trigger_messages or boundary <= conv.summary_upto:
                return  # 还没到阈值，或没有新的旧轮次要压缩

            to_compress = messages[conv.summary_upto : boundary]
            dialogue = "\n".join(f"{m.role}: {m.content[:400]}" for m in to_compress)
            parts = []
            if conv.summary:
                parts.append(f"已有摘要：\n{conv.summary}")
            parts.append(f"新增对话内容：\n{dialogue}")

            resp = await chat_completion(
                [
                    {"role": "system", "content": SUMMARY_PROMPT},
                    {"role": "user", "content": "\n\n".join(parts)},
                ],
                temperature=0.2,
                max_tokens=400,
            )
            new_summary = (resp.choices[0].message.content or "").strip()
            if not new_summary:
                return
            conv.summary = new_summary[:2000]
            conv.summary_upto = boundary
            await db.commit()
            logger.info("会话摘要已更新: %s（覆盖到第 %d 条）", conversation_id, boundary)
    except Exception:
        logger.warning("会话摘要更新失败（跳过，下轮重试）", exc_info=True)
