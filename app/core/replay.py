from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CheckpointORM, EventORM, MessageORM
from app.models.domain import TimelineItem


class ReplayService:
    async def reconstruct_timeline(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        branch_id: str,
        actor: str | None = None,
        action_type: str | None = None,
    ) -> list[TimelineItem]:
        messages = (
            await db.scalars(
                select(MessageORM)
                .where(MessageORM.session_id == session_id, MessageORM.branch_id == branch_id)
                .order_by(MessageORM.created_at.asc(), MessageORM.id.asc())
            )
        ).all()
        events_query = (
            select(EventORM)
            .where(EventORM.session_id == session_id, EventORM.branch_id == branch_id)
            .order_by(EventORM.created_at.asc(), EventORM.id.asc())
        )
        if actor:
            events_query = events_query.where(EventORM.actor == actor)
        if action_type:
            events_query = events_query.where(EventORM.action_type == action_type)
        events = (await db.scalars(events_query)).all()
        checkpoints = (
            await db.scalars(
                select(CheckpointORM)
                .where(CheckpointORM.session_id == session_id, CheckpointORM.branch_id == branch_id)
                .order_by(CheckpointORM.created_at.asc(), CheckpointORM.id.asc())
            )
        ).all()

        timeline: list[TimelineItem] = []
        for m in messages:
            timeline.append(
                TimelineItem(
                    kind='message',
                    created_at=m.created_at,
                    actor=m.actor,
                    message_role=m.role,
                    content=m.content,
                    payload=m.content_json or {},
                    source_id=m.id,
                )
            )
        for e in events:
            timeline.append(
                TimelineItem(
                    kind='event',
                    created_at=e.created_at,
                    actor=e.actor,
                    action_type=e.action_type,
                    content=e.observation_summary,
                    payload=e.action_payload or {},
                    step_index=e.step_index,
                    source_id=e.id,
                )
            )
        for c in checkpoints:
            timeline.append(
                TimelineItem(
                    kind='checkpoint',
                    created_at=c.created_at,
                    actor=c.created_by_actor,
                    action_type='checkpoint',
                    content=f'{c.label} ({c.reason})',
                    payload={'reason': c.reason, 'label': c.label, 'snapshot': c.snapshot_json},
                    step_index=c.step_index,
                    source_id=c.id,
                )
            )
        timeline.sort(key=lambda x: (x.created_at, x.step_index or -1, x.source_id))
        return timeline

