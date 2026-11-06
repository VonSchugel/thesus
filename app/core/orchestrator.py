from __future__ import annotations

import asyncio
from copy import deepcopy
import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.extractor import ExtractorAgent
from app.agents.navigator import NavigatorAgent
from app.agents.planner import PlannerAgent
from app.core.checkpoints import checkpoint_runtime, create_checkpoint, remap_checkpoint_instructions, snapshot_from_runtime_state
from app.core.config import Settings
from app.core.control_lock import ControlLockRegistry
from app.core.errors import ConflictError, NotFoundError, ValidationError, classify_exception
from app.core.live_workspace import LiveWorkspace
from app.core.task_groups import TaskGroups
from app.core.replay import ReplayService
from app.db.models import CheckpointORM, EventORM, MessageORM
from app.db.repo_approvals import ApprovalRepository
from app.db.repo_checkpoints import CheckpointRepository
from app.db.repo_events import EventRepository
from app.db.repo_messages import MessageRepository
from app.db.repo_sessions import SessionRepository
from app.db.repo_state import RuntimeStateRepository
from app.integrations.browser.base import BrowserAdapter
from app.integrations.browser.playwright_adapter import PlaywrightAdapter
from app.integrations.local_llm import LocalLLMProvider
from app.models.api import (
    BranchResponse,
    CheckpointResponse,
    ControlActionResponse,
    EventResponse,
    MessageResponse,
    ReplayResponse,
    RuntimeStateResponse,
    SessionBranchInfo,
    SessionResponse,
    TimelineItemResponse,
)
from app.models.domain import SessionContext, TaskPlan
from app.models.enums import ActionType, Actor, ControlHolder, ControlMode, MessageRole, RunStatus


@dataclass(slots=True)
class RepoBundle:
    sessions: SessionRepository
    messages: MessageRepository
    events: EventRepository
    checkpoints: CheckpointRepository
    state: RuntimeStateRepository
    approvals: ApprovalRepository


@dataclass(slots=True)
class EnrichmentJob:
    session_id: str
    branch_id: str
    user_message_id: str
    request: str
    items: list[dict[str, Any]]
    message_ids: tuple[str, str]
    event_id: str


class OrchestratorService:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        browser_adapter: BrowserAdapter,
        replay_service: ReplayService,
        lock_registry: ControlLockRegistry,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.browser_adapter = browser_adapter
        self.replay_service = replay_service
        self.lock_registry = lock_registry
        self.repos = RepoBundle(
            sessions=SessionRepository(),
            messages=MessageRepository(),
            events=EventRepository(),
            checkpoints=CheckpointRepository(),
            state=RuntimeStateRepository(),
            approvals=ApprovalRepository(),
        )
        self.planner = PlannerAgent()
        self.local_llm = LocalLLMProvider(settings)
        # Canonical extraction must finish and commit before optional inference.
        self.extractor = ExtractorAgent()
        self._enrichment_tasks: set[asyncio.Task[None]] = set()
        self.navigator = NavigatorAgent(
            browser_adapter=browser_adapter,
            event_repo=self.repos.events,
            state_repo=self.repos.state,
            approval_repo=self.repos.approvals,
            settings=settings,
        )
        self.execution_manager = None
        self.log = logging.getLogger(__name__)
        self.live = LiveWorkspace(self, browser_adapter) if isinstance(browser_adapter, PlaywrightAdapter) else None
        self.task_groups = TaskGroups(self) if self.live else None

    def set_execution_manager(self, execution_manager: Any) -> None:
        self.execution_manager = execution_manager

    async def reconcile_pending_enrichment(self, db: AsyncSession) -> dict[str, int]:
        """Close interrupted advisory metadata before accepting new work.

        This never replays inference or changes canonical content/runtime state.
        Keyset batches bound memory while preserving original source bindings.
        """
        reconciled = {'messages': 0, 'events': 0}
        for model, field, counter in (
            (MessageORM, 'content_json', 'messages'),
            (EventORM, 'action_payload', 'events'),
        ):
            cursor = None
            while True:
                column = getattr(model, field)
                query = select(model).where(column['local_llm']['status'].as_string() == 'pending').order_by(model.id).limit(200)
                if cursor is not None:
                    query = query.where(model.id > cursor)
                rows = list((await db.scalars(query)).all())
                if not rows:
                    break
                for row in rows:
                    payload = dict(getattr(row, field) or {})
                    pending = payload.get('local_llm')
                    if isinstance(pending, dict) and pending.get('status') == 'pending':
                        payload['local_llm'] = {**pending, 'status': 'fallback', 'reason': 'enrichment_interrupted'}
                        setattr(row, field, payload)
                        reconciled[counter] += 1
                await db.flush()
                cursor = rows[-1].id
        return reconciled

    async def wait_for_enrichment(self, timeout: float = 5.0) -> None:
        end = asyncio.get_running_loop().time() + timeout
        while self._enrichment_tasks:
            if asyncio.get_running_loop().time() >= end:
                raise TimeoutError('Local enrichment did not become idle in time')
            await asyncio.sleep(0.01)

    async def shutdown_enrichment(self) -> None:
        tasks = list(self._enrichment_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=6)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=1)

    async def _enrich_committed_result(self, job: EnrichmentJob) -> None:
        cancelled = False
        try:
            advisory = await self.local_llm.enrich(items=job.items, request=job.request)
        except asyncio.CancelledError:
            advisory = {'status': 'fallback', 'reason': 'enrichment_cancelled', 'model': self.local_llm.model}
            cancelled = True
        except Exception as exc:
            advisory = {'status': 'fallback', 'reason': 'unexpected_provider_error',
                        'error_type': type(exc).__name__, 'model': self.local_llm.model}
        await self._persist_advisory(job, advisory)
        if cancelled:
            raise asyncio.CancelledError

    async def _persist_advisory(self, job: EnrichmentJob, advisory: dict[str, Any]) -> None:
        advisory = {**advisory, 'source_user_message_id': job.user_message_id}
        try:
            # No network await or shared AsyncSession inside this transaction.
            async with self.session_factory() as db:
                for message_id in job.message_ids:
                    message = await db.get(MessageORM, message_id)
                    if message is not None and message.session_id == job.session_id and message.branch_id == job.branch_id:
                        payload = dict(message.content_json or {})
                        pending = payload.get('local_llm', {})
                        if pending.get('status') == 'pending' and pending.get('source_user_message_id') == job.user_message_id:
                            payload['local_llm'] = advisory
                            message.content_json = payload
                event = await db.get(EventORM, job.event_id)
                if event is not None and event.session_id == job.session_id and event.branch_id == job.branch_id:
                    payload = dict(event.action_payload or {})
                    pending = payload.get('local_llm', {})
                    if pending.get('status') == 'pending' and pending.get('source_user_message_id') == job.user_message_id:
                        payload['local_llm'] = advisory
                        event.action_payload = payload
                await db.commit()
        except Exception:
            self.log.exception('Unable to persist optional enrichment')

    def profile_path_for(self, ctx: SessionContext) -> str:
        path = self.settings.profile_dir / ctx.session_id / ctx.branch_id
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    async def stage_session(self, db: AsyncSession, *, title: str | None, root_task: str | None,
                            model_profile: str | None, start_task: bool):
        if model_profile not in (None, 'default') and not self.live:
            raise ValidationError('Local model profiles require the live browser workspace')
        selection = self.live.models.selection(model_profile) if self.live else {}
        session, branch, state = await self.repos.sessions.create_session(db, title=title, root_task=root_task)
        ctx = SessionContext(session_id=session.id, branch_id=branch.id)
        # Chromium can create a live profile lazily after the database transaction commits.
        state.browser_profile_path = str(self.settings.profile_dir / ctx.session_id / ctx.branch_id) if self.live else self.profile_path_for(ctx)
        if selection:
            state.blackboard_json = {**state.blackboard_json, **selection}
        if self.live and start_task and root_task and root_task.strip():
            message = await self.repos.messages.add_message(
                db, session_id=session.id, branch_id=branch.id, role=MessageRole.USER.value,
                actor=Actor.USER.value, content=root_task.strip(),
            )
            self.live.update(state, run_status=RunStatus.QUEUED,
                             pending_user_instructions_json=[{'content': message.content, 'message_id': message.id}])
            await self.live.event(db, state, 'note', 'User queued a browser task', actor=Actor.USER,
                                  payload={'message_id': message.id})
        return session, branch

    async def create_session(self, *, title: str | None = None, root_task: str | None = None,
                             model_profile: str | None = None, start_task: bool = False) -> SessionResponse:
        async with self.session_factory() as db:
            session, branch = await self.stage_session(db, title=title, root_task=root_task,
                                                        model_profile=model_profile, start_task=start_task)
            await db.commit()
            response = await self._build_session_response(db, session.id, branch.id)
        if self.live and response.state.run_status == RunStatus.QUEUED:
            await self.execution_manager.schedule(session.id, branch.id)
        return response

    async def list_sessions(self) -> list[dict[str, Any]]:
        async with self.session_factory() as db:
            rows = await self.repos.sessions.list_sessions(db)
            return [
                {
                    'session_id': s.id,
                    'title': s.title,
                    'status': s.status,
                    'default_branch_id': s.default_branch_id,
                    'updated_at': s.updated_at,
                    'created_at': s.created_at,
                }
                for s in rows
            ]

    async def get_session(self, session_id: str, branch_id: str | None = None) -> SessionResponse:
        async with self.session_factory() as db:
            resolved_branch_id = await self._resolve_branch_id(db, session_id, branch_id)
            return await self._build_session_response(db, session_id, resolved_branch_id)

    async def list_messages(
        self,
        session_id: str,
        *,
        branch_id: str | None = None,
        limit: int = 200,
        before: str | None = None,
    ) -> list[MessageResponse]:
        async with self.session_factory() as db:
            resolved_branch_id = await self._resolve_branch_id(db, session_id, branch_id)
            rows = await self.repos.messages.list_messages(
                db, session_id=session_id, branch_id=resolved_branch_id, limit=limit, before=before
            )
            return [self._message_to_api(m) for m in rows]

    async def list_events(
        self,
        session_id: str,
        *,
        branch_id: str | None = None,
        actor: str | None = None,
        action_type: str | None = None,
        limit: int = 500,
        after_event_id: str | None = None,
    ) -> list[EventResponse]:
        async with self.session_factory() as db:
            resolved_branch_id = await self._resolve_branch_id(db, session_id, branch_id)
            rows = await self.repos.events.list_events(
                db,
                session_id=session_id,
                branch_id=resolved_branch_id,
                actor=actor,
                action_type=action_type,
                limit=limit,
                after_event_id=after_event_id,
            )
            return [self._event_to_api(e) for e in rows]

    async def list_checkpoints(self, session_id: str, *, branch_id: str | None = None) -> list[CheckpointResponse]:
        async with self.session_factory() as db:
            resolved_branch_id = await self._resolve_branch_id(db, session_id, branch_id)
            rows = await self.repos.checkpoints.list_checkpoints(db, session_id=session_id, branch_id=resolved_branch_id)
            return [
                CheckpointResponse(
                    checkpoint_id=r.id,
                    session_id=r.session_id,
                    branch_id=r.branch_id,
                    label=r.label,
                    reason=r.reason,
                    step_index=r.step_index,
                    message_cursor=r.message_cursor,
                    event_cursor=r.event_cursor,
                    created_at=r.created_at,
                )
                for r in rows
            ]

    async def replay(
        self,
        session_id: str,
        *,
        branch_id: str | None = None,
        actor: str | None = None,
        action_type: str | None = None,
    ) -> ReplayResponse:
        async with self.session_factory() as db:
            resolved_branch_id = await self._resolve_branch_id(db, session_id, branch_id)
            timeline = await self.replay_service.reconstruct_timeline(
                db, session_id=session_id, branch_id=resolved_branch_id, actor=actor, action_type=action_type
            )
            return ReplayResponse(
                session_id=session_id,
                branch_id=resolved_branch_id,
                timeline=[TimelineItemResponse(**item.model_dump(mode='json')) for item in timeline],
            )

    async def post_user_message(
        self, session_id: str, *, branch_id: str | None, content: str, trigger_agents: bool
    ) -> dict[str, Any]:
        if self.live:
            ctx = await self.live.context(session_id, branch_id)
            async with await self.live.lock(ctx):
                return await self._post_user_message(session_id, branch_id=ctx.branch_id, content=content, trigger_agents=trigger_agents)
        return await self._post_user_message(session_id, branch_id=branch_id, content=content, trigger_agents=trigger_agents)

    async def _post_user_message(
        self, session_id: str, *, branch_id: str | None, content: str, trigger_agents: bool
    ) -> dict[str, Any]:
        async with self.session_factory() as db:
            resolved_branch_id = await self._resolve_branch_id(db, session_id, branch_id)
            message = await self.repos.messages.add_message(
                db,
                session_id=session_id,
                branch_id=resolved_branch_id,
                role=MessageRole.USER.value,
                actor=Actor.USER.value,
                content=content,
            )
            state = await self.repos.state.append_pending_user_instruction(
                db,
                session_id=session_id,
                branch_id=resolved_branch_id,
                instruction={'content': content, 'message_id': message.id},
            )
            if not state.browser_profile_path:
                state.browser_profile_path = self.profile_path_for(SessionContext(session_id=session_id, branch_id=resolved_branch_id))
            if self.live:
                await self.live.on_instruction(db, SessionContext(session_id=session_id, branch_id=resolved_branch_id), state, trigger_agents)
            await self.repos.events.add_event(
                db,
                session_id=session_id,
                branch_id=resolved_branch_id,
                step_index=int((state.blackboard_json or {}).get('last_step_index', 0)),
                actor=Actor.USER.value,
                action_type=ActionType.NOTE.value,
                action_payload={'message_id': message.id},
                observation_summary='User message received',
            )
            await self.repos.sessions.touch_session(db, session_id)
            await db.commit()
            if self.live:
                self.live.committed_control_change(SessionContext(session_id=session_id, branch_id=resolved_branch_id), state)
            manual = state.control_mode == ControlMode.MANUAL.value
        scheduled = False
        if trigger_agents and not manual and self.execution_manager is not None:
            scheduled = await self.execution_manager.schedule(session_id, resolved_branch_id)
        return {'accepted': True, 'scheduled': scheduled, 'session_id': session_id, 'branch_id': resolved_branch_id, 'manual_mode': manual}

    async def pause(self, session_id: str, *, branch_id: str | None = None) -> ControlActionResponse:
        if self.live:
            return await self.live.change_run(await self.live.context(session_id, branch_id), 'pause')
        async with self.session_factory() as db:
            resolved_branch_id = await self._resolve_branch_id(db, session_id, branch_id)
            state = await self.repos.state.get(db, session_id=session_id, branch_id=resolved_branch_id)
            if state is None:
                raise NotFoundError('Runtime state not found')
            step_idx = int((state.blackboard_json or {}).get('last_step_index', 0))
            await self.repos.state.upsert(db, session_id=session_id, branch_id=resolved_branch_id, pause_requested=True, run_status=RunStatus.PAUSED.value)
            await self.repos.events.add_event(
                db,
                session_id=session_id,
                branch_id=resolved_branch_id,
                step_index=step_idx,
                actor=Actor.USER.value,
                action_type=ActionType.PAUSE.value,
                action_payload={},
                observation_summary='User paused agent execution',
            )
            await db.commit()
        await self.browser_adapter.pause(SessionContext(session_id=session_id, branch_id=resolved_branch_id))
        return ControlActionResponse(ok=True, run_status=RunStatus.PAUSED.value, control_mode=state.control_mode, message='Pause requested')

    async def resume(self, session_id: str, *, branch_id: str | None = None) -> ControlActionResponse:
        if self.live:
            return await self.live.change_run(await self.live.context(session_id, branch_id), 'resume')
        async with self.session_factory() as db:
            resolved_branch_id = await self._resolve_branch_id(db, session_id, branch_id)
            state = await self.repos.state.get(db, session_id=session_id, branch_id=resolved_branch_id)
            if state is None:
                raise NotFoundError('Runtime state not found')
            if state.control_mode == ControlMode.MANUAL.value:
                raise ConflictError('Cannot resume while in manual mode. Release control first.')
            if state.approval_required:
                raise ConflictError('Approval is still pending for a sensitive action.')
            await self.repos.state.upsert(
                db,
                session_id=session_id,
                branch_id=resolved_branch_id,
                pause_requested=False,
                run_status=RunStatus.PAUSED.value,
                control_holder=ControlHolder.NONE.value,
            )
            await self.repos.events.add_event(
                db,
                session_id=session_id,
                branch_id=resolved_branch_id,
                step_index=int((state.blackboard_json or {}).get('last_step_index', 0)),
                actor=Actor.USER.value,
                action_type=ActionType.RESUME.value,
                action_payload={},
                observation_summary='User resumed agent execution',
            )
            await db.commit()
        await self.browser_adapter.resume(SessionContext(session_id=session_id, branch_id=resolved_branch_id))
        if self.execution_manager is not None:
            await self.execution_manager.schedule(session_id, resolved_branch_id)
        return ControlActionResponse(ok=True, run_status=RunStatus.PAUSED.value, control_mode=ControlMode.AGENT.value, message='Resume requested')

    async def takeover(self, session_id: str, *, branch_id: str | None = None) -> ControlActionResponse:
        async with self.session_factory() as db:
            resolved_branch_id = await self._resolve_branch_id(db, session_id, branch_id)
            snap = await snapshot_from_runtime_state(db, session_id, resolved_branch_id)
            checkpoint = await create_checkpoint(
                db,
                session_id=session_id,
                branch_id=resolved_branch_id,
                label='Manual takeover checkpoint',
                reason='manual_before_takeover',
                step_index=int((snap.get('blackboard_json') or {}).get('last_step_index', 0)),
                snapshot_json=snap,
                created_by_actor=Actor.USER.value,
            )
            state = await self.repos.state.upsert(
                db,
                session_id=session_id,
                branch_id=resolved_branch_id,
                control_mode=ControlMode.MANUAL.value,
                control_holder=ControlHolder.USER.value,
                pause_requested=True,
                run_status=RunStatus.MANUAL.value,
            )
            await self.repos.events.add_event(
                db,
                session_id=session_id,
                branch_id=resolved_branch_id,
                step_index=int((state.blackboard_json or {}).get('last_step_index', 0)),
                actor=Actor.USER.value,
                action_type=ActionType.TAKEOVER.value,
                action_payload={'checkpoint_id': checkpoint.id},
                observation_summary='User took manual browser control',
                checkpoint_id=checkpoint.id,
            )
            await db.commit()
        await self.browser_adapter.pause(SessionContext(session_id=session_id, branch_id=resolved_branch_id))
        return ControlActionResponse(ok=True, run_status=RunStatus.MANUAL.value, control_mode=ControlMode.MANUAL.value, message='Manual takeover enabled')

    async def release(self, session_id: str, *, branch_id: str | None = None, note: str | None = None) -> ControlActionResponse:
        async with self.session_factory() as db:
            resolved_branch_id = await self._resolve_branch_id(db, session_id, branch_id)
            ctx = SessionContext(session_id=session_id, branch_id=resolved_branch_id)
            snapshot = await self.browser_adapter.capture_snapshot(ctx)
            state = await self.repos.state.get(db, session_id=session_id, branch_id=resolved_branch_id)
            blackboard = dict((state.blackboard_json if state else {}) or {})
            if note:
                notes = list(blackboard.get('notes', []))
                notes.append(note)
                blackboard['notes'] = notes
            checkpoint = await create_checkpoint(
                db,
                session_id=session_id,
                branch_id=resolved_branch_id,
                label='Manual release checkpoint',
                reason='manual_release',
                step_index=int(blackboard.get('last_step_index', 0)),
                snapshot_json={**(await snapshot_from_runtime_state(db, session_id, resolved_branch_id)), 'manual_snapshot': snapshot.model_dump(mode='json')},
                created_by_actor=Actor.USER.value,
            )
            await self.repos.state.upsert(
                db,
                session_id=session_id,
                branch_id=resolved_branch_id,
                control_mode=ControlMode.AGENT.value,
                control_holder=ControlHolder.NONE.value,
                pause_requested=True,
                run_status=RunStatus.PAUSED.value,
                last_known_url=snapshot.live_state.url,
                last_known_title=snapshot.live_state.title,
                tabs_json=[t.model_dump(mode='json') for t in snapshot.live_state.tabs],
                latest_observation=snapshot.live_state.latest_observation,
                blackboard_json=blackboard,
            )
            await self.repos.events.add_event(
                db,
                session_id=session_id,
                branch_id=resolved_branch_id,
                step_index=int(blackboard.get('last_step_index', 0)),
                actor=Actor.USER.value,
                action_type=ActionType.RELEASE.value,
                action_payload={'checkpoint_id': checkpoint.id, 'note': note},
                observation_summary='Manual control released back to agent',
                page_url=snapshot.live_state.url,
                checkpoint_id=checkpoint.id,
            )
            await db.commit()
        await self.browser_adapter.resume(SessionContext(session_id=session_id, branch_id=resolved_branch_id))
        return ControlActionResponse(ok=True, run_status=RunStatus.PAUSED.value, control_mode=ControlMode.AGENT.value, message='Manual control released; resume to continue')

    async def approve(self, session_id: str, *, branch_id: str | None, correlation_id: str, note: str | None) -> ControlActionResponse:
        async with self.session_factory() as db:
            resolved_branch_id = await self._resolve_branch_id(db, session_id, branch_id)
            state = await self.repos.state.get(db, session_id=session_id, branch_id=resolved_branch_id)
            if state is None:
                raise NotFoundError('Runtime state not found')
            if state.approval_correlation_id != correlation_id:
                raise ValidationError('Correlation ID does not match pending action')
            row = await self.repos.approvals.decide(
                db,
                session_id=session_id,
                branch_id=resolved_branch_id,
                correlation_id=correlation_id,
                status='approved',
                decided_by=Actor.USER.value,
                reason=note,
            )
            if row is None:
                raise NotFoundError('Pending approval not found')
            await self.repos.state.upsert(db, session_id=session_id, branch_id=resolved_branch_id, approval_required=False, run_status=RunStatus.PAUSED.value)
            await self.repos.events.add_event(
                db,
                session_id=session_id,
                branch_id=resolved_branch_id,
                step_index=int((state.blackboard_json or {}).get('last_step_index', 0)),
                actor=Actor.USER.value,
                action_type=ActionType.APPROVE.value,
                action_payload={'correlation_id': correlation_id, 'note': note},
                observation_summary='User approved pending sensitive action',
                correlation_id=correlation_id,
            )
            await db.commit()
        return ControlActionResponse(ok=True, run_status=RunStatus.PAUSED.value, control_mode=ControlMode.AGENT.value, message='Action approved; resume to continue')

    async def reject(self, session_id: str, *, branch_id: str | None, correlation_id: str, reason: str | None) -> ControlActionResponse:
        async with self.session_factory() as db:
            resolved_branch_id = await self._resolve_branch_id(db, session_id, branch_id)
            state = await self.repos.state.get(db, session_id=session_id, branch_id=resolved_branch_id)
            if state is None:
                raise NotFoundError('Runtime state not found')
            row = await self.repos.approvals.decide(
                db,
                session_id=session_id,
                branch_id=resolved_branch_id,
                correlation_id=correlation_id,
                status='rejected',
                decided_by=Actor.USER.value,
                reason=reason,
            )
            if row is None:
                raise NotFoundError('Pending approval not found')
            blackboard = dict(state.blackboard_json or {})
            notes = list(blackboard.get('notes', []))
            notes.append(f"Rejected action {correlation_id}: {reason or 'no reason'}")
            blackboard['notes'] = notes
            await self.repos.state.upsert(
                db,
                session_id=session_id,
                branch_id=resolved_branch_id,
                approval_required=False,
                approval_pending_action=None,
                approval_correlation_id=None,
                run_status=RunStatus.PAUSED.value,
                pending_action_summary=None,
                blackboard_json=blackboard,
            )
            await self.repos.events.add_event(
                db,
                session_id=session_id,
                branch_id=resolved_branch_id,
                step_index=int((state.blackboard_json or {}).get('last_step_index', 0)),
                actor=Actor.USER.value,
                action_type=ActionType.REJECT.value,
                action_payload={'correlation_id': correlation_id, 'reason': reason},
                observation_summary='User rejected pending sensitive action',
                correlation_id=correlation_id,
            )
            await db.commit()
        return ControlActionResponse(ok=True, run_status=RunStatus.PAUSED.value, control_mode=ControlMode.AGENT.value, message='Action rejected; replan on next resume')

    async def create_branch_from_checkpoint(self, session_id: str, *, source_branch_id: str | None, checkpoint_id: str | None, name: str | None) -> BranchResponse:
        async with self.session_factory() as db:
            source_branch_id = await self._resolve_branch_id(db, session_id, source_branch_id)
            source_branch = await self.repos.sessions.get_branch(db, source_branch_id)
            if source_branch is None:
                raise NotFoundError('Source branch not found')
            checkpoint = await (self.repos.checkpoints.get(db, checkpoint_id) if checkpoint_id else self.repos.checkpoints.latest(db, session_id=session_id, branch_id=source_branch_id))
            if checkpoint is None:
                raise ValidationError('No checkpoint available for branching')
            if checkpoint.session_id != session_id or checkpoint.branch_id != source_branch_id:
                raise NotFoundError('Checkpoint not found in the source branch')
            branch_name = name or f"branch-{checkpoint.id[:8]}"
            new_branch = await self.repos.sessions.create_branch(
                db,
                session_id=session_id,
                name=branch_name,
                parent_branch_id=source_branch_id,
                source_checkpoint_id=checkpoint.id,
                created_by_actor=Actor.USER.value,
                lineage_depth=(source_branch.lineage_depth or 0) + 1,
            )
            src_messages = await self.repos.messages.list_all_ordered(
                db, session_id=session_id, branch_id=source_branch_id, prefix_limit=checkpoint.message_cursor)
            src_events = await self.repos.events.list_all_ordered(
                db, session_id=session_id, branch_id=source_branch_id, prefix_limit=checkpoint.event_cursor)
            checkpoints = await self.repos.checkpoints.list_checkpoints(db, session_id=session_id, branch_id=source_branch_id)
            cutoff = next(index for index, row in enumerate(checkpoints) if row.id == checkpoint.id)
            src_checkpoints = checkpoints[:cutoff + 1]
            # Local IDs preserve source ordering when timestamps tie and keep references scoped to the fork.
            copy_ids: dict[type, dict[str, str]] = {MessageORM: {}, EventORM: {}, CheckpointORM: {}}
            source_records = sorted([*src_messages, *src_events, *src_checkpoints], key=lambda row: (row.created_at, row.id))
            record_count = len(src_messages) + len(src_events) + len(src_checkpoints)
            local_ids = sorted(str(uuid4()) for _ in range(record_count))
            for row, local_id in zip(source_records, local_ids, strict=True):
                copy_ids[type(row)][row.id] = local_id
            message_ids = copy_ids[MessageORM]
            event_ids = copy_ids[EventORM]
            checkpoint_ids = copy_ids[CheckpointORM]
            for c in src_checkpoints:
                await create_checkpoint(
                    db,
                    session_id=session_id,
                    branch_id=new_branch.id,
                    label=c.label,
                    reason=c.reason,
                    step_index=c.step_index,
                    snapshot_json=remap_checkpoint_instructions(c.snapshot_json or {}, message_ids),
                    created_by_actor=c.created_by_actor,
                    cursors=(c.message_cursor, c.event_cursor),
                    record_id=checkpoint_ids[c.id],
                    created_at=c.created_at,
                )
            for m in src_messages:
                await self.repos.messages.add_message(
                    db,
                    session_id=session_id,
                    branch_id=new_branch.id,
                    role=m.role,
                    actor=m.actor,
                    content=m.content,
                    content_json=m.content_json,
                    parent_message_id=message_ids.get(m.parent_message_id) if m.parent_message_id else None,
                    checkpoint_id=checkpoint_ids.get(m.checkpoint_id) if m.checkpoint_id else None,
                    record_id=message_ids[m.id],
                    created_at=m.created_at,
                )
            for e in src_events:
                payload = dict(e.action_payload or {})
                for key, copied_ids in (('message_id', message_ids), ('source_message_id', message_ids),
                                        ('checkpoint_id', checkpoint_ids)):
                    value = payload.get(key)
                    if isinstance(value, str) and value in copied_ids:
                        payload[key] = copied_ids[value]
                await self.repos.events.add_event(
                    db,
                    session_id=session_id,
                    branch_id=new_branch.id,
                    step_index=e.step_index,
                    actor=e.actor,
                    action_type=e.action_type,
                    action_payload=payload,
                    observation_summary=e.observation_summary,
                    page_url=e.page_url,
                    screenshot_path=e.screenshot_path,
                    checkpoint_id=checkpoint_ids.get(e.checkpoint_id) if e.checkpoint_id else None,
                    browser_event_name=e.browser_event_name,
                    raw_event_json=e.raw_event_json,
                    correlation_id=e.correlation_id,
                    is_sensitive=e.is_sensitive,
                    record_id=event_ids[e.id],
                    created_at=e.created_at,
                )
            source_snapshot = remap_checkpoint_instructions(checkpoint.snapshot_json or {}, message_ids)
            await self.repos.state.create_for_branch(
                db,
                session_id=session_id,
                branch_id=new_branch.id,
                browser_profile_path=self.profile_path_for(SessionContext(session_id=session_id, branch_id=new_branch.id)),
                initial_snapshot={
                    **checkpoint_runtime(source_snapshot),
                    'run_status': RunStatus.PAUSED.value,
                    'control_mode': ControlMode.AGENT.value,
                    'control_holder': ControlHolder.NONE.value,
                    'pause_requested': True,
                    'approval_required': False,
                    'approval_pending_action': None,
                    'approval_correlation_id': None,
                },
            )
            await db.commit()
            return BranchResponse(session_id=session_id, branch_id=new_branch.id, source_branch_id=source_branch_id, checkpoint_id=checkpoint.id, name=new_branch.name)

    async def snapshot(self, session_id: str, *, branch_id: str | None, note: str | None) -> ControlActionResponse:
        if self.live:
            ctx = await self.live.context(session_id, branch_id)
            async with await self.live.lock(ctx):
                return await self._snapshot(session_id, branch_id=ctx.branch_id, note=note)
        return await self._snapshot(session_id, branch_id=branch_id, note=note)

    async def _snapshot(self, session_id: str, *, branch_id: str | None, note: str | None) -> ControlActionResponse:
        async with self.session_factory() as db:
            resolved_branch_id = await self._resolve_branch_id(db, session_id, branch_id)
            ctx = SessionContext(session_id=session_id, branch_id=resolved_branch_id)
            snap = await self.browser_adapter.capture_snapshot(ctx)
            state = await self.repos.state.get(db, session_id=session_id, branch_id=resolved_branch_id)
            blackboard = dict((state.blackboard_json if state else {}) or {})
            if note:
                notes = list(blackboard.get('notes', []))
                notes.append(note)
                blackboard['notes'] = notes
            browser_updates = {}
            if not self.live or snap.live_state.revision is not None:
                browser_updates = dict(last_known_url=snap.live_state.url, last_known_title=snap.live_state.title,
                                       tabs_json=[t.model_dump(mode='json') for t in snap.live_state.tabs],
                                       latest_observation=snap.live_state.latest_observation)
            state = await self.repos.state.upsert(db, session_id=session_id, branch_id=resolved_branch_id,
                                                  blackboard_json=blackboard, **browser_updates)
            cp = await create_checkpoint(
                db,
                session_id=session_id,
                branch_id=resolved_branch_id,
                label='Manual snapshot',
                reason='user_request',
                step_index=int(blackboard.get('last_step_index', 0)),
                snapshot_json={'live_state': snap.model_dump(mode='json'), 'runtime': await snapshot_from_runtime_state(db, session_id, resolved_branch_id)},
                created_by_actor=Actor.USER.value,
            )
            await self.repos.events.add_event(
                db,
                session_id=session_id,
                branch_id=resolved_branch_id,
                step_index=int(blackboard.get('last_step_index', 0)),
                actor=Actor.USER.value,
                action_type=ActionType.SNAPSHOT.value,
                action_payload={'note': note, 'checkpoint_id': cp.id},
                observation_summary='User snapshot captured',
                page_url=state.last_known_url,
                checkpoint_id=cp.id,
            )
            await db.commit()
        return ControlActionResponse(ok=True, run_status=(state.run_status if state else RunStatus.IDLE.value), control_mode=(state.control_mode if state else ControlMode.AGENT.value), message='Snapshot recorded')

    async def process_branch(self, session_id: str, branch_id: str) -> None:
        if self.live:
            await self.live.process(SessionContext(session_id=session_id, branch_id=branch_id))
            return
        enrichment_job = None
        lock = await self.lock_registry.get(session_id, branch_id)
        async with lock:
            async with self.session_factory() as db:
                try:
                    enrichment_job = await self._process_branch_locked(db, session_id=session_id, branch_id=branch_id)
                    await db.commit()
                except Exception as exc:
                    enrichment_job = None
                    classification = classify_exception(exc)
                    self.log.exception('Branch processing failed', extra={'extra_data': {'session_id': session_id, 'branch_id': branch_id}})
                    await self.repos.state.upsert(
                        db,
                        session_id=session_id,
                        branch_id=branch_id,
                        run_status=RunStatus.FAILED.value,
                        last_error_code=classification.code,
                        last_error_message=classification.message,
                        control_holder=ControlHolder.NONE.value,
                    )
                    await self.repos.events.add_event(
                        db,
                        session_id=session_id,
                        branch_id=branch_id,
                        step_index=0,
                        actor=Actor.SYSTEM.value,
                        action_type=ActionType.ERROR.value,
                        action_payload={
                            'error_type': type(exc).__name__,
                            'error_code': classification.code,
                            'error_category': classification.category,
                            'retryable': classification.retryable,
                            'recovery_options': classification.recovery_options,
                        },
                        observation_summary=classification.message,
                    )
                    await db.commit()
        if enrichment_job is not None:
            if len(self._enrichment_tasks) >= 4:
                await self._persist_advisory(enrichment_job, {'status': 'skipped', 'reason': 'task_budget_exhausted', 'model': self.local_llm.model})
            else:
                task = asyncio.create_task(self._enrich_committed_result(enrichment_job), name=f'local-enrichment:{enrichment_job.user_message_id}')
                self._enrichment_tasks.add(task)
                task.add_done_callback(self._enrichment_tasks.discard)

    async def _process_branch_locked(self, db: AsyncSession, *, session_id: str, branch_id: str) -> EnrichmentJob | None:
        session = await self.repos.sessions.get_session(db, session_id)
        if session is None:
            raise NotFoundError('Session not found')
        state = await self.repos.state.get(db, session_id=session_id, branch_id=branch_id)
        if state is None:
            raise NotFoundError('Runtime state not found')
        ctx = SessionContext(session_id=session_id, branch_id=branch_id)
        if not state.browser_profile_path:
            state.browser_profile_path = self.profile_path_for(ctx)
        if state.control_mode == ControlMode.MANUAL.value:
            state.run_status = RunStatus.MANUAL.value
            return
        if state.pause_requested:
            state.run_status = RunStatus.PAUSED.value
            return

        state.control_holder = ControlHolder.AGENT.value
        state.last_error_code = None
        state.last_error_message = None

        await self.navigator.execute_pending_approved_action(
            db, ctx=ctx, step_index=int((state.blackboard_json or {}).get('last_step_index', 0)) + 1
        )
        refreshed = await self.repos.state.get(db, session_id=session_id, branch_id=branch_id)
        if refreshed and refreshed.approval_pending_action and refreshed.approval_correlation_id and not refreshed.approval_required:
            decision = await self.repos.approvals.latest_by_correlation(
                db,
                session_id=session_id,
                branch_id=branch_id,
                correlation_id=refreshed.approval_correlation_id,
            )
            if decision and decision.status == 'approved':
                await self.repos.state.upsert(
                    db,
                    session_id=session_id,
                    branch_id=branch_id,
                    approval_pending_action=None,
                    approval_correlation_id=None,
                    approval_required=False,
                )

        messages = await self.repos.messages.list_messages(db, session_id=session_id, branch_id=branch_id, limit=500)
        latest_user_message = next((m for m in reversed(messages) if m.role == MessageRole.USER.value), None)
        if latest_user_message is None:
            await self.repos.state.upsert(db, session_id=session_id, branch_id=branch_id, run_status=RunStatus.IDLE.value, control_holder=ControlHolder.NONE.value)
            return

        state = await self.repos.state.get(db, session_id=session_id, branch_id=branch_id)
        if state is None:
            return
        need_plan = state.task_plan_json is None or bool(state.pending_user_instructions_json)
        if need_plan:
            await self._run_planner(db, session_id=session_id, branch_id=branch_id, user_message=latest_user_message)
            state = await self.repos.state.get(db, session_id=session_id, branch_id=branch_id)
            if state is None or state.control_mode == ControlMode.MANUAL.value or state.pause_requested or state.approval_required:
                return

        if state.task_plan_json is None:
            await self.repos.state.upsert(db, session_id=session_id, branch_id=branch_id, run_status=RunStatus.IDLE.value, control_holder=ControlHolder.NONE.value)
            return
        plan = TaskPlan.model_validate(state.task_plan_json)
        await self.navigator.navigate_plan(db, ctx=ctx, plan=plan)
        await self.repos.state.upsert(db, session_id=session_id, branch_id=branch_id, task_plan_json=plan.model_dump(mode='json'))

        post_nav = await self.repos.state.get(db, session_id=session_id, branch_id=branch_id)
        if post_nav is None:
            return
        if post_nav.control_mode == ControlMode.MANUAL.value or post_nav.pause_requested or post_nav.approval_required:
            if post_nav.control_mode == ControlMode.MANUAL.value:
                post_nav.run_status = RunStatus.MANUAL.value
            elif post_nav.approval_required:
                post_nav.run_status = RunStatus.AWAITING_APPROVAL.value
            else:
                post_nav.run_status = RunStatus.PAUSED.value
            post_nav.control_holder = ControlHolder.NONE.value
            return

        if all(st.status == 'done' for st in plan.subtasks):
            enrichment_job = await self._run_extractor(db, session_id=session_id, branch_id=branch_id, user_message=latest_user_message, plan=plan)
            await self.repos.state.upsert(
                db,
                session_id=session_id,
                branch_id=branch_id,
                run_status=RunStatus.COMPLETED.value,
                control_holder=ControlHolder.NONE.value,
                pending_action_summary=None,
            )
            return enrichment_job
        else:
            await self.repos.state.upsert(db, session_id=session_id, branch_id=branch_id, run_status=RunStatus.PAUSED.value, control_holder=ControlHolder.NONE.value)

    async def _run_planner(self, db: AsyncSession, *, session_id: str, branch_id: str, user_message: MessageORM) -> None:
        state = await self.repos.state.get(db, session_id=session_id, branch_id=branch_id)
        if state is None:
            raise NotFoundError('Runtime state not found')
        await self.repos.state.upsert(db, session_id=session_id, branch_id=branch_id, run_status=RunStatus.PLANNING.value, control_holder=Actor.PLANNER_AGENT.value)
        plan = await self.planner.create_plan(user_request=user_message.content, prior_findings=list((state.blackboard_json or {}).get('findings', [])))
        await self.repos.messages.add_message(
            db,
            session_id=session_id,
            branch_id=branch_id,
            role=MessageRole.PLANNER_AGENT.value,
            actor=Actor.PLANNER_AGENT.value,
            content=f"Planned {len(plan.subtasks)} subtasks for request: {plan.goal}",
            content_json=plan.model_dump(mode='json'),
        )
        await self.repos.events.add_event(
            db,
            session_id=session_id,
            branch_id=branch_id,
            step_index=int((state.blackboard_json or {}).get('last_step_index', 0)),
            actor=Actor.PLANNER_AGENT.value,
            action_type=ActionType.PLAN.value,
            action_payload=plan.model_dump(mode='json'),
            observation_summary='Planner created/updated task plan',
        )
        await self.repos.state.upsert(
            db,
            session_id=session_id,
            branch_id=branch_id,
            task_plan_json=plan.model_dump(mode='json'),
            pending_user_instructions_json=[],
            blackboard_json={**(state.blackboard_json or {'findings': [], 'subtasks': []}), 'subtasks': [s.model_dump(mode='json') for s in plan.subtasks]},
        )
        for idx, cp in enumerate(plan.checkpoints, start=1):
            snap = await snapshot_from_runtime_state(db, session_id, branch_id)
            checkpoint_row = await create_checkpoint(
                db,
                session_id=session_id,
                branch_id=branch_id,
                label=cp.label,
                reason=cp.reason,
                step_index=int((state.blackboard_json or {}).get('last_step_index', 0)) + idx,
                snapshot_json=snap,
                created_by_actor=Actor.PLANNER_AGENT.value,
            )
            await self.repos.events.add_event(
                db,
                session_id=session_id,
                branch_id=branch_id,
                step_index=int((state.blackboard_json or {}).get('last_step_index', 0)) + idx,
                actor=Actor.PLANNER_AGENT.value,
                action_type=ActionType.CHECKPOINT.value,
                action_payload={'checkpoint_id': checkpoint_row.id, 'label': cp.label, 'reason': cp.reason},
                observation_summary='Planner checkpoint recorded',
                checkpoint_id=checkpoint_row.id,
            )

    async def _run_extractor(self, db: AsyncSession, *, session_id: str, branch_id: str, user_message: MessageORM, plan: TaskPlan) -> EnrichmentJob | None:
        state = await self.repos.state.get(db, session_id=session_id, branch_id=branch_id)
        if state is None:
            raise NotFoundError('Runtime state not found')
        await self.repos.state.upsert(db, session_id=session_id, branch_id=branch_id, run_status=RunStatus.EXTRACTING.value, control_holder=Actor.EXTRACTOR_AGENT.value)
        structured, text = await self.extractor.summarize(user_request=user_message.content, blackboard=state.blackboard_json or {}, plan=plan)
        if self.settings.local_llm_enrichment_enabled:
            structured['local_llm'] = {'status': 'pending', 'model': self.local_llm.model, 'source_user_message_id': user_message.id}
        extractor_message = await self.repos.messages.add_message(db, session_id=session_id, branch_id=branch_id, role=MessageRole.EXTRACTOR_AGENT.value, actor=Actor.EXTRACTOR_AGENT.value, content=text, content_json=structured)
        assistant_message = await self.repos.messages.add_message(db, session_id=session_id, branch_id=branch_id, role=MessageRole.ASSISTANT.value, actor=Actor.ASSISTANT.value, content=text, content_json=structured)
        event = await self.repos.events.add_event(
            db,
            session_id=session_id,
            branch_id=branch_id,
            step_index=int((state.blackboard_json or {}).get('last_step_index', 0)),
            actor=Actor.EXTRACTOR_AGENT.value,
            action_type=ActionType.EXTRACT.value,
            action_payload=structured,
            observation_summary='Extractor summarized findings',
            page_url=state.last_known_url,
        )
        if self.settings.local_llm_enrichment_enabled:
            return EnrichmentJob(session_id=session_id, branch_id=branch_id, user_message_id=user_message.id,
                request=user_message.content, items=deepcopy(structured['items']),
                message_ids=(extractor_message.id, assistant_message.id), event_id=event.id)

    async def _resolve_branch_id(self, db: AsyncSession, session_id: str, branch_id: str | None) -> str:
        session = await self.repos.sessions.get_session(db, session_id)
        if session is None:
            raise NotFoundError('Session not found')
        resolved = branch_id or session.default_branch_id
        if not resolved:
            raise ValidationError('Session has no default branch')
        branch = await self.repos.sessions.get_branch(db, resolved)
        if branch is None or branch.session_id != session_id:
            raise NotFoundError('Branch not found for session')
        return resolved

    async def _build_session_response(self, db: AsyncSession, session_id: str, branch_id: str) -> SessionResponse:
        session = await self.repos.sessions.get_session(db, session_id)
        if session is None:
            raise NotFoundError('Session not found')
        state = await self.repos.state.get(db, session_id=session_id, branch_id=branch_id)
        if state is None:
            raise NotFoundError('Runtime state not found')
        branches = await self.repos.sessions.list_branches(db, session_id)
        return SessionResponse(
            session_id=session.id,
            default_branch_id=session.default_branch_id or branch_id,
            status=session.status,
            title=session.title,
            root_task=session.root_task,
            created_at=session.created_at,
            updated_at=session.updated_at,
            selected_branch_id=branch_id,
            state=self._runtime_state_to_api(state),
            available_branches=[
                SessionBranchInfo(
                    branch_id=b.id,
                    name=b.name,
                    parent_branch_id=b.parent_branch_id,
                    source_checkpoint_id=b.source_checkpoint_id,
                    created_at=b.created_at,
                )
                for b in branches
            ],
        )

    def _runtime_state_to_api(self, state: Any) -> RuntimeStateResponse:
        return RuntimeStateResponse(
            run_status=state.run_status,
            control_mode=state.control_mode,
            control_holder=state.control_holder,
            pause_requested=bool(state.pause_requested),
            operator_paused=bool((state.blackboard_json or {}).get('operator_paused')),
            approval_required=bool(state.approval_required),
            approval_correlation_id=state.approval_correlation_id,
            last_known_url=state.last_known_url,
            last_known_title=state.last_known_title,
            tabs_json=state.tabs_json or [],
            latest_observation=state.latest_observation,
            pending_action_summary=state.pending_action_summary,
            pending_approval=self.live.approval_details(state) if self.live else None,
            browser_profile_path=None if self.live else state.browser_profile_path,
            last_error_code=state.last_error_code,
            last_error_message=state.last_error_message,
            updated_at=state.updated_at,
            **(self.live.model_fields(state) if self.live else {}),
        )

    def _message_to_api(self, row: Any) -> MessageResponse:
        return MessageResponse(
            id=row.id,
            session_id=row.session_id,
            branch_id=row.branch_id,
            role=row.role,
            actor=row.actor,
            content=row.content,
            content_json=row.content_json,
            created_at=row.created_at,
            parent_message_id=row.parent_message_id,
            checkpoint_id=row.checkpoint_id,
        )

    def _event_to_api(self, row: Any) -> EventResponse:
        return EventResponse(
            event_id=row.id,
            session_id=row.session_id,
            branch_id=row.branch_id,
            step_index=row.step_index,
            actor=row.actor,
            action_type=row.action_type,
            action_payload=row.action_payload or {},
            observation_summary=row.observation_summary,
            page_url=row.page_url,
            screenshot_path=row.screenshot_path,
            created_at=row.created_at,
            checkpoint_id=row.checkpoint_id,
            browser_event_name=row.browser_event_name,
            correlation_id=row.correlation_id,
            is_sensitive=bool(row.is_sensitive),
        )

