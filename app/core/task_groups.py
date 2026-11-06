from __future__ import annotations

import asyncio
from collections import defaultdict
from hashlib import sha256
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.db.models import MessageORM, SessionORM, SessionRuntimeStateORM, TaskGroupMemberORM, TaskGroupORM
from app.models.enums import RunStatus
from app.models.task_groups import CreateTaskGroupRequest, GroupCounts, GroupTaskResponse, TaskGroupResponse, WaitFor

if TYPE_CHECKING:
    from app.core.orchestrator import OrchestratorService


TERMINAL = {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED}
ATTENTION = {RunStatus.AWAITING_APPROVAL, RunStatus.AWAITING_INPUT, RunStatus.MANUAL, RunStatus.INTERRUPTED}


class TaskGroups:
    def __init__(self, service: OrchestratorService) -> None:
        self.service = service

    async def create(self, payload: CreateTaskGroupRequest) -> TaskGroupResponse:
        live = self.service.live
        if len(payload.tasks) > self.service.settings.browser_max_group_tasks:
            raise ValidationError('This group exceeds the configured task limit')
        group_id = str(payload.request_id or uuid4())
        fingerprint = sha256(payload.model_dump_json(exclude={'request_id'}).encode()).hexdigest()
        branches = []
        existing = False
        async with self.service.session_factory() as db:
            try:
                db.add(TaskGroupORM(id=group_id, title=payload.title, fingerprint=fingerprint))
                await db.flush()
                for position, task in enumerate(payload.tasks):
                    session, branch = await self.service.stage_session(
                        db, title=task.title or f'Task {position + 1}', root_task=task.goal,
                        model_profile=task.model_profile, start_task=True,
                    )
                    db.add(TaskGroupMemberORM(group_id=group_id, position=position,
                                              session_id=session.id, branch_id=branch.id))
                    branches.append((session.id, branch.id))
                await db.commit()
                # Dispatch committed work even if the response snapshot later fails.
                await asyncio.shield(live.schedule_queued(branches))
            except IntegrityError:
                await db.rollback()
                group = await db.get(TaskGroupORM, group_id)
                if group is None:
                    raise
                if group.fingerprint != fingerprint:
                    raise ConflictError('This request ID already belongs to a different task group') from None
                existing = True
        snapshot = await self.get(group_id)
        if existing:
            await live.schedule_queued((task.session_id, task.branch_id) for task in snapshot.tasks
                                       if task.run_status == RunStatus.QUEUED)
        return snapshot

    async def get(self, group_id: str) -> TaskGroupResponse:
        groups = await self._read(select(TaskGroupORM.id).where(TaskGroupORM.id == group_id))
        if not groups:
            raise NotFoundError('Task group not found')
        return groups[0]

    async def list(self) -> list[TaskGroupResponse]:
        return await self._read(select(TaskGroupORM.id).order_by(TaskGroupORM.created_at.desc(), TaskGroupORM.id.desc()).limit(25))

    async def wait(self, group_id: str, condition: WaitFor, timeout: float) -> TaskGroupResponse:
        deadline = monotonic() + timeout
        while True:
            group = await self.get(group_id)
            if group.status == 'settled' or (condition == 'attention' and group.status == 'attention'):
                return group
            remaining = deadline - monotonic()
            if remaining <= 0:
                group.timed_out = True
                return group
            await asyncio.sleep(min(remaining, 0.35))

    async def _read(self, group_ids) -> list[TaskGroupResponse]:
        latest_answer = select(MessageORM.content).where(
            MessageORM.session_id == TaskGroupMemberORM.session_id,
            MessageORM.branch_id == TaskGroupMemberORM.branch_id,
            MessageORM.role == 'assistant',
        ).order_by(MessageORM.created_at.desc(), MessageORM.id.desc()).limit(1).correlate(TaskGroupMemberORM).scalar_subquery()
        query = (select(TaskGroupORM, SessionORM, SessionRuntimeStateORM, latest_answer)
                 .join(TaskGroupMemberORM, TaskGroupMemberORM.group_id == TaskGroupORM.id)
                 .join(SessionORM, SessionORM.id == TaskGroupMemberORM.session_id)
                 .join(SessionRuntimeStateORM, and_(SessionRuntimeStateORM.session_id == TaskGroupMemberORM.session_id,
                                                    SessionRuntimeStateORM.branch_id == TaskGroupMemberORM.branch_id))
                 .where(TaskGroupORM.id.in_(group_ids))
                 .order_by(TaskGroupORM.created_at.desc(), TaskGroupORM.id.desc(), TaskGroupMemberORM.position))
        async with self.service.session_factory() as db:
            rows = (await db.execute(query)).all()
        groups = {}
        tasks = defaultdict(list)
        for group, session, state, answer in rows:
            groups[group.id] = group
            status = state.run_status
            attention = status in ATTENTION or (status == RunStatus.PAUSED and state.pause_requested)
            tasks[group.id].append(GroupTaskResponse(
                session_id=session.id, branch_id=state.branch_id, title=session.title,
                goal=(state.task_plan_json or {}).get('goal') or session.root_task or '',
                run_status=status, attention=attention, error=state.last_error_message, activity=state.pending_action_summary,
                answer=answer if status in {RunStatus.COMPLETED, RunStatus.AWAITING_INPUT} else None,
                **self.service.live.model_fields(state),
            ))
        result = []
        for group_id, group in groups.items():
            members = tasks[group_id]
            total = len(members)
            terminal = sum(task.run_status in TERMINAL for task in members)
            attention = sum(task.attention for task in members)
            queued = sum(task.run_status == RunStatus.QUEUED for task in members)
            result.append(TaskGroupResponse(
                group_id=group_id, title=group.title, created_at=group.created_at, tasks=members,
                status='settled' if terminal == total else 'attention' if attention else 'active',
                counts=GroupCounts(total=total, queued=queued, terminal=terminal,
                                   attention=attention, running=total - terminal - attention - queued),
            ))
        return result
