from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SessionRuntimeStateORM
from app.models.enums import BrowserRestoreMode, ControlHolder, ControlMode, RunStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RuntimeStateRepository:
    async def get(self, db: AsyncSession, *, session_id: str, branch_id: str) -> SessionRuntimeStateORM | None:
        return await db.get(SessionRuntimeStateORM, {'session_id': session_id, 'branch_id': branch_id})

    async def create_for_branch(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        branch_id: str,
        browser_profile_path: str | None = None,
        initial_snapshot: dict[str, Any] | None = None,
    ) -> SessionRuntimeStateORM:
        snap = initial_snapshot or {}
        row = SessionRuntimeStateORM(
            session_id=session_id,
            branch_id=branch_id,
            run_status=snap.get('run_status', RunStatus.IDLE.value),
            control_mode=snap.get('control_mode', ControlMode.AGENT.value),
            control_holder=snap.get('control_holder', ControlHolder.NONE.value),
            pause_requested=snap.get('pause_requested', False),
            approval_required=snap.get('approval_required', False),
            approval_pending_action=snap.get('approval_pending_action'),
            approval_correlation_id=snap.get('approval_correlation_id'),
            last_known_url=snap.get('last_known_url'),
            last_known_title=snap.get('last_known_title'),
            tabs_json=snap.get('tabs_json', []),
            latest_observation=snap.get('latest_observation'),
            pending_action_summary=snap.get('pending_action_summary'),
            task_plan_json=snap.get('task_plan_json'),
            blackboard_json=snap.get('blackboard_json', {'findings': [], 'subtasks': []}),
            pending_user_instructions_json=snap.get('pending_user_instructions_json', []),
            browser_profile_path=browser_profile_path or snap.get('browser_profile_path'),
            browser_session_meta_json=snap.get('browser_session_meta_json', {}),
            browser_restore_mode=snap.get('browser_restore_mode', BrowserRestoreMode.NONE.value),
            last_error_code=snap.get('last_error_code'),
            last_error_message=snap.get('last_error_message'),
            retry_count=snap.get('retry_count', 0),
            updated_at=utcnow(),
        )
        db.add(row)
        await db.flush()
        return row

    async def upsert(self, db: AsyncSession, *, session_id: str, branch_id: str, **updates: Any) -> SessionRuntimeStateORM:
        row = await self.get(db, session_id=session_id, branch_id=branch_id)
        if row is None:
            row = await self.create_for_branch(db, session_id=session_id, branch_id=branch_id)
        for key, value in updates.items():
            if hasattr(row, key):
                setattr(row, key, value)
        row.updated_at = utcnow()
        await db.flush()
        return row

    async def append_pending_user_instruction(self, db: AsyncSession, *, session_id: str, branch_id: str, instruction: dict[str, Any]) -> SessionRuntimeStateORM:
        row = await self.upsert(db, session_id=session_id, branch_id=branch_id)
        pending = list(row.pending_user_instructions_json or [])
        pending.append(instruction)
        row.pending_user_instructions_json = pending
        row.updated_at = utcnow()
        await db.flush()
        return row

    async def clear_pending_user_instructions(self, db: AsyncSession, *, session_id: str, branch_id: str) -> SessionRuntimeStateORM:
        return await self.upsert(db, session_id=session_id, branch_id=branch_id, pending_user_instructions_json=[])

