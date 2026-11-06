from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApprovalDecisionORM


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ApprovalRepository:
    async def create_pending(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        branch_id: str,
        correlation_id: str,
        requested_action_json: dict[str, Any],
    ) -> ApprovalDecisionORM:
        row = ApprovalDecisionORM(
            id=str(uuid4()),
            session_id=session_id,
            branch_id=branch_id,
            correlation_id=correlation_id,
            status='pending',
            requested_action_json=requested_action_json,
        )
        db.add(row)
        await db.flush()
        return row

    async def latest_by_correlation(
        self, db: AsyncSession, *, session_id: str, branch_id: str, correlation_id: str
    ) -> ApprovalDecisionORM | None:
        rows = await db.scalars(
            select(ApprovalDecisionORM)
            .where(
                ApprovalDecisionORM.session_id == session_id,
                ApprovalDecisionORM.branch_id == branch_id,
                ApprovalDecisionORM.correlation_id == correlation_id,
            )
            .order_by(ApprovalDecisionORM.requested_at.desc(), ApprovalDecisionORM.id.desc())
        )
        return rows.first()

    async def decide(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        branch_id: str,
        correlation_id: str,
        status: str,
        decided_by: str,
        reason: str | None,
    ) -> ApprovalDecisionORM | None:
        row = await self.latest_by_correlation(db, session_id=session_id, branch_id=branch_id, correlation_id=correlation_id)
        if row is None:
            return None
        row.status = status
        row.decided_by = decided_by
        row.reason = reason
        row.decided_at = utcnow()
        await db.flush()
        return row

