from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.history_order import history_order
from app.db.models import MessageORM


class MessageRepository:
    async def add_message(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        branch_id: str,
        role: str,
        actor: str,
        content: str,
        content_json: dict[str, Any] | None = None,
        parent_message_id: str | None = None,
        checkpoint_id: str | None = None,
        record_id: str | None = None,
        created_at: datetime | None = None,
    ) -> MessageORM:
        row = MessageORM(
            id=record_id or str(uuid4()),
            session_id=session_id,
            branch_id=branch_id,
            role=role,
            actor=actor,
            content=content,
            content_json=content_json,
            parent_message_id=parent_message_id,
            checkpoint_id=checkpoint_id,
        )
        if created_at is not None:
            row.created_at = created_at
        db.add(row)
        await db.flush()
        return row

    async def list_messages(self, db: AsyncSession, *, session_id: str, branch_id: str, limit: int = 200, before: str | None = None) -> list[MessageORM]:
        q = select(MessageORM).where(MessageORM.session_id == session_id, MessageORM.branch_id == branch_id)
        order = history_order(db, MessageORM)
        if before:
            cursor = (await db.execute(select(*order).where(
                MessageORM.id == before, MessageORM.session_id == session_id, MessageORM.branch_id == branch_id,
            ))).one_or_none()
            if cursor is None:
                return []
            q = q.where(tuple_(*order) < tuple(cursor))
        if limit:
            q = q.order_by(*(column.desc() for column in order)).limit(limit)
            return list(reversed((await db.scalars(q)).all()))
        return list((await db.scalars(q.order_by(*order))).all())

    async def count_messages(self, db: AsyncSession, *, session_id: str, branch_id: str) -> int:
        return int(await db.scalar(select(func.count(MessageORM.id)).where(
            MessageORM.session_id == session_id, MessageORM.branch_id == branch_id,
        )) or 0)

    async def list_all_ordered(self, db: AsyncSession, *, session_id: str, branch_id: str,
                               prefix_limit: int | None = None) -> list[MessageORM]:
        query = (select(MessageORM).where(MessageORM.session_id == session_id, MessageORM.branch_id == branch_id)
                 .order_by(*history_order(db, MessageORM)).limit(prefix_limit))
        return list((await db.scalars(query)).all())

