"""用量统计接口：总览 + 近 N 天趋势。"""
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import Chunk, Conversation, Document, KnowledgeBase, UsageRecord, User
from app.schemas import DailyUsage, StatsOverview

router = APIRouter(prefix="/api/stats", tags=["统计"])


@router.get("/overview", response_model=StatsOverview)
async def overview(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    async def _count(model, *where):
        return (await db.execute(select(func.count()).select_from(model).where(*where))).scalar() or 0

    usage = (
        await db.execute(
            select(
                func.count(UsageRecord.id),
                func.coalesce(func.sum(UsageRecord.prompt_tokens), 0),
                func.coalesce(func.sum(UsageRecord.completion_tokens), 0),
                func.coalesce(func.avg(UsageRecord.first_token_ms), 0),
                func.coalesce(func.avg(UsageRecord.total_ms), 0),
                func.coalesce(func.sum(UsageRecord.estimated_cost_microusd), 0),
            ).where(UsageRecord.owner_id == user.id)
        )
    ).one()

    return StatsOverview(
        kb_count=await _count(KnowledgeBase, KnowledgeBase.owner_id == user.id),
        doc_count=await _count(Document, Document.owner_id == user.id),
        chunk_count=await _count(Chunk, Chunk.owner_id == user.id),
        conversation_count=await _count(Conversation, Conversation.owner_id == user.id),
        question_count=usage[0],
        prompt_tokens=int(usage[1]),
        completion_tokens=int(usage[2]),
        avg_first_token_ms=int(usage[3]),
        avg_total_ms=int(usage[4]),
        estimated_cost_usd=round(float(usage[5]) / 1_000_000, 6),
    )


@router.get("/daily", response_model=list[DailyUsage])
async def daily(days: int = 14, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    days = max(1, min(days, 90))
    day_col = func.to_char(func.date_trunc("day", UsageRecord.created_at), "YYYY-MM-DD")
    stmt = (
        select(
            day_col.label("day"),
            func.count(UsageRecord.id),
            func.coalesce(func.sum(UsageRecord.prompt_tokens), 0),
            func.coalesce(func.sum(UsageRecord.completion_tokens), 0),
        )
        .where(
            UsageRecord.owner_id == user.id,
            UsageRecord.created_at >= func.now() - func.make_interval(0, 0, 0, days),
        )
        .group_by("day")
        .order_by("day")
    )
    rows = (await db.execute(stmt)).all()
    return [
        DailyUsage(date=day, questions=q, prompt_tokens=int(pt), completion_tokens=int(ct))
        for day, q, pt, ct in rows
    ]
