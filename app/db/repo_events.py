from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.history_order import history_order
from app.db.models import EventORM


class EventRepository:
    async def add_event(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        branch_id: str,
        step_index: int,
        actor: str,
        action_type: str,
        action_payload: dict[str, Any] | None = None,
        observation_summary: str | None = None,
        page_url: str | None = None,
        screenshot_path: str | None = None,
        checkpoint_id: str | None = None,
        browser_event_name: str | None = None,
        raw_event_json: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        is_sensitive: bool = False,
        record_id: str | None = None,
        created_at: datetime | None = None,
    ) -> EventORM:
        row = EventORM(
            id=record_id or str(uuid4()),
            session_id=session_id,
            branch_id=branch_id,
            step_index=step_index,
            actor=actor,
            action_type=action_type,
            action_payload=action_payload or {},
            observation_summary=observation_summary,
            page_url=page_url,
            screenshot_path=screenshot_path,
            checkpoint_id=checkpoint_id,
            browser_event_name=browser_event_name,
            raw_event_json=raw_event_json,
            correlation_id=correlation_id,
            is_sensitive=is_sensitive,
        )
        if created_at is not None:
            row.created_at = created_at
        db.add(row)
        await db.flush()
        return row

    async def list_events(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        branch_id: str,
        actor: str | None = None,
        action_type: str | None = None,
        limit: int = 500,
        after_event_id: str | None = None,
    ) -> list[EventORM]:
        q = select(EventORM).where(EventORM.session_id == session_id, EventORM.branch_id == branch_id)
        order = history_order(db, EventORM)
        if actor:
            q = q.where(EventORM.actor == actor)
        if action_type:
            q = q.where(EventORM.action_type == action_type)
        if after_event_id:
            cursor = (await db.execute(select(*order).where(
                EventORM.id == after_event_id, EventORM.session_id == session_id, EventORM.branch_id == branch_id,
            ))).one_or_none()
            if cursor is None:
                return []
            q = q.where(tuple_(*order) > tuple(cursor)).order_by(*order)
            return list((await db.scalars(q.limit(limit) if limit else q)).all())
        if limit:
            q = q.order_by(*(column.desc() for column in order)).limit(limit)
            return list(reversed((await db.scalars(q)).all()))
        return list((await db.scalars(q.order_by(*order))).all())

    async def list_all_ordered(self, db: AsyncSession, *, session_id: str, branch_id: str,
                               prefix_limit: int | None = None) -> list[EventORM]:
        query = (select(EventORM).where(EventORM.session_id == session_id, EventORM.branch_id == branch_id)
                 .order_by(*history_order(db, EventORM)).limit(prefix_limit))
        return list((await db.scalars(query)).all())

    async def count_events(self, db: AsyncSession, *, session_id: str, branch_id: str) -> int:
        return int(await db.scalar(select(func.count(EventORM.id)).where(
            EventORM.session_id == session_id, EventORM.branch_id == branch_id,
        )) or 0)

