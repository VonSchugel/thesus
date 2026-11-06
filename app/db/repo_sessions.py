from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SessionBranchORM, SessionORM, SessionRuntimeStateORM
from app.models.enums import ControlHolder, ControlMode, RunStatus, SessionStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SessionRepository:
    async def create_session(self, db: AsyncSession, *, title: str | None = None, root_task: str | None = None) -> tuple[SessionORM, SessionBranchORM, SessionRuntimeStateORM]:
        session_id = str(uuid4())
        branch_id = str(uuid4())
        session = SessionORM(
            id=session_id,
            title=title,
            root_task=root_task,
            status=SessionStatus.ACTIVE.value,
            default_branch_id=branch_id,
        )
        branch = SessionBranchORM(
            id=branch_id,
            session_id=session_id,
            name='main',
            created_by_actor='system',
            lineage_depth=0,
        )
        state = SessionRuntimeStateORM(
            session_id=session_id,
            branch_id=branch_id,
            run_status=RunStatus.IDLE.value,
            control_mode=ControlMode.AGENT.value,
            control_holder=ControlHolder.NONE.value,
            pause_requested=False,
            approval_required=False,
            blackboard_json={'findings': [], 'subtasks': []},
            pending_user_instructions_json=[],
            browser_restore_mode='none',
        )
        db.add_all([session, branch, state])
        await db.flush()
        return session, branch, state

    async def get_session(self, db: AsyncSession, session_id: str) -> SessionORM | None:
        return await db.get(SessionORM, session_id)

    async def list_sessions(self, db: AsyncSession) -> list[SessionORM]:
        rows = await db.scalars(select(SessionORM).order_by(SessionORM.updated_at.desc(), SessionORM.id.desc()))
        return list(rows)

    async def list_branches(self, db: AsyncSession, session_id: str) -> list[SessionBranchORM]:
        rows = await db.scalars(
            select(SessionBranchORM).where(SessionBranchORM.session_id == session_id).order_by(SessionBranchORM.created_at.asc())
        )
        return list(rows)

    async def get_branch(self, db: AsyncSession, branch_id: str) -> SessionBranchORM | None:
        return await db.get(SessionBranchORM, branch_id)

    async def create_branch(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        name: str,
        parent_branch_id: str | None,
        source_checkpoint_id: str | None,
        created_by_actor: str,
        lineage_depth: int,
    ) -> SessionBranchORM:
        branch = SessionBranchORM(
            id=str(uuid4()),
            session_id=session_id,
            name=name,
            parent_branch_id=parent_branch_id,
            source_checkpoint_id=source_checkpoint_id,
            created_by_actor=created_by_actor,
            lineage_depth=lineage_depth,
        )
        db.add(branch)
        await db.flush()
        return branch

    async def touch_session(self, db: AsyncSession, session_id: str) -> None:
        session = await self.get_session(db, session_id)
        if session:
            session.updated_at = utcnow()
            await db.flush()

