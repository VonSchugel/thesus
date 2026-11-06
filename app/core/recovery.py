from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SessionRuntimeStateORM
from app.db.repo_events import EventRepository
from app.db.repo_state import utcnow
from app.models.enums import Actor, ControlHolder, RunStatus


async def recover_runtime_states(db: AsyncSession) -> None:
    rows = (
        await db.scalars(
            select(SessionRuntimeStateORM).where(
                SessionRuntimeStateORM.run_status.in_(
                    [RunStatus.PLANNING.value, RunStatus.NAVIGATING.value, RunStatus.EXTRACTING.value]
                )
            )
        )
    ).all()
    events = EventRepository()
    for row in rows:
        previous = row.run_status
        row.run_status = RunStatus.INTERRUPTED.value
        row.control_holder = ControlHolder.NONE.value
        row.pause_requested = True
        row.last_error_code = row.last_error_code or 'process_restart'
        row.last_error_message = row.last_error_message or 'Execution interrupted by process restart'
        row.updated_at = utcnow()
        await events.add_event(
            db, session_id=row.session_id, branch_id=row.branch_id,
            step_index=int((row.blackboard_json or {}).get('last_step_index', 0)),
            actor=Actor.SYSTEM.value, action_type='process_recovery',
            action_payload={'previous_status': previous},
            observation_summary='Execution interrupted by process restart. Resume explicitly to continue.',
        )

