from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CheckpointORM, EventORM, MessageORM, SessionRuntimeStateORM
from app.models.enums import Actor


async def create_checkpoint(
    db: AsyncSession,
    *,
    session_id: str,
    branch_id: str,
    label: str,
    reason: str,
    step_index: int,
    snapshot_json: dict[str, Any],
    created_by_actor: str = Actor.SYSTEM.value,
    cursors: tuple[int, int] | None = None,
    record_id: str | None = None,
    created_at: datetime | None = None,
) -> CheckpointORM:
    if cursors is None:
        message_cursor = await db.scalar(
            select(func.count(MessageORM.id)).where(MessageORM.session_id == session_id, MessageORM.branch_id == branch_id)
        )
        event_cursor = await db.scalar(
            select(func.count(EventORM.id)).where(EventORM.session_id == session_id, EventORM.branch_id == branch_id)
        )
    else:
        message_cursor, event_cursor = cursors
    checkpoint = CheckpointORM(
        id=record_id or str(uuid4()),
        session_id=session_id,
        branch_id=branch_id,
        label=label,
        reason=reason,
        step_index=step_index,
        message_cursor=int(message_cursor or 0),
        event_cursor=int(event_cursor or 0),
        snapshot_json=snapshot_json,
        created_by_actor=created_by_actor,
    )
    if created_at is not None:
        checkpoint.created_at = created_at
    db.add(checkpoint)
    await db.flush()
    return checkpoint


async def snapshot_from_runtime_state(db: AsyncSession, session_id: str, branch_id: str) -> dict[str, Any]:
    state = await db.get(SessionRuntimeStateORM, {'session_id': session_id, 'branch_id': branch_id})
    if not state:
        return {}
    runtime = {
        'run_status': state.run_status,
        'control_mode': state.control_mode,
        'control_holder': state.control_holder,
        'last_known_url': state.last_known_url,
        'last_known_title': state.last_known_title,
        'tabs_json': state.tabs_json or [],
        'latest_observation': state.latest_observation,
        'pending_action_summary': state.pending_action_summary,
        'task_plan_json': state.task_plan_json or {},
        'blackboard_json': state.blackboard_json or {},
        'pending_user_instructions_json': state.pending_user_instructions_json or [],
    }
    uncertainty = checkpoint_uncertainty(state.browser_session_meta_json)
    if uncertainty:
        runtime['browser_session_meta_json'] = uncertainty
    return runtime


def checkpoint_uncertainty(meta: dict[str, Any] | None) -> dict[str, Any]:
    state = meta or {}
    intent = state.get('uncertain') or state.get('in_flight')
    if not intent:
        return {}
    kind = intent.get('kind') if isinstance(intent, dict) else None
    return {'uncertain': {'kind': kind if isinstance(kind, str) and kind.isidentifier() else 'browser_command'}}


def checkpoint_runtime(snapshot: dict[str, Any]) -> dict[str, Any]:
    runtime = dict(snapshot.get('runtime') or snapshot)
    browser = (snapshot.get('live_state') or {}).get('live_state') or {}
    # Older manual snapshots captured Chromium before updating the runtime row.
    if browser.get('revision') is not None:
        runtime.update(last_known_url=browser.get('url'), last_known_title=browser.get('title'),
                       tabs_json=browser.get('tabs') or [], latest_observation=browser.get('latest_observation'))
    uncertainty = checkpoint_uncertainty(runtime.pop('browser_session_meta_json', None))
    if uncertainty:
        runtime.update(browser_session_meta_json=uncertainty, last_error_code='browser_outcome_unknown',
                       last_error_message='This checkpoint followed a browser command whose outcome could not be verified.')
    return runtime


def remap_checkpoint_instructions(snapshot: dict[str, Any], message_ids: dict[str, str]) -> dict[str, Any]:
    runtime = snapshot.get('runtime') or snapshot
    pending = runtime.get('pending_user_instructions_json') or []
    if not pending:
        return snapshot
    remapped = {**runtime, 'pending_user_instructions_json': [
        {**instruction, 'message_id': message_ids[instruction['message_id']]}
        for instruction in pending if instruction.get('message_id') in message_ids
    ]}
    return {**snapshot, 'runtime': remapped} if 'runtime' in snapshot else remapped

