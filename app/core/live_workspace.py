from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from hashlib import sha256
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import select

from app.agents.browser_planner import AgentDecision, BrowserPlanner
from app.core.browser_models import BrowserModels
from app.core.checkpoints import create_checkpoint, snapshot_from_runtime_state
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.safety import url_allowed
from app.db.models import SessionRuntimeStateORM
from app.db.repo_state import utcnow
from app.integrations.browser.playwright_adapter import public_url
from app.models.api import ControlActionResponse
from app.models.browser import BrowserCapacity, BrowserCommand, BrowserObservation, BrowserOutcomeUnknown, BrowserUnavailable, StaleBrowserState
from app.models.domain import BrowserLiveState, SessionContext, Subtask, TaskPlan, TabState
from app.models.enums import Actor, ControlHolder, ControlMode, RunStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.orchestrator import OrchestratorService
    from app.integrations.browser.playwright_adapter import PlaywrightAdapter


@dataclass(slots=True)
class Proposal:
    id: str
    command: BrowserCommand
    revision: str
    summary: str
    context: dict[str, Any]


@dataclass(slots=True)
class Lease:
    id: str
    client: str
    last_seen: float = field(default_factory=monotonic)
    commands: OrderedDict[str, tuple[str, dict | None]] = field(default_factory=OrderedDict)


@dataclass(slots=True)
class BranchControl:
    version: int = 0
    lease: Lease | None = None
    proposal: Proposal | None = None
    model_task: asyncio.Task[AgentDecision] | None = None
    model_status: str | None = None
    capacity_task: asyncio.Task[None] | None = None


class LiveWorkspace:
    def __init__(self, service: OrchestratorService, browser: PlaywrightAdapter) -> None:
        self.service = service
        self.settings = service.settings
        self.db = service.session_factory
        self.repos = service.repos
        self.browser = browser
        self.planner = BrowserPlanner(self.settings)
        self.models = BrowserModels(self.settings)
        self.model_slots = asyncio.Semaphore(self.settings.browser_model_parallelism)
        self.controls: dict[tuple[str, str], BranchControl] = {}
        self._manual_commands: set[asyncio.Task[dict]] = set()

    async def drain_manual_commands(self) -> None:
        if self._manual_commands:
            await asyncio.gather(*self._manual_commands, return_exceptions=True)

    async def schedule_queued(self, branches: Iterable[tuple[str, str]] | None = None) -> None:
        if branches is None:
            async with self.db() as db:
                rows = await db.execute(select(SessionRuntimeStateORM.session_id, SessionRuntimeStateORM.branch_id)
                                        .where(SessionRuntimeStateORM.run_status == RunStatus.QUEUED)
                                        .order_by(SessionRuntimeStateORM.updated_at, SessionRuntimeStateORM.session_id))
                branches = [(row.session_id, row.branch_id) for row in rows]
        await self.service.execution_manager.schedule_many(branches)

    def model_fields(self, state: SessionRuntimeStateORM) -> dict:
        selection = (state.blackboard_json or {}).get('model_selection')
        control = self.controls.get((state.session_id, state.branch_id))
        return {'model_profile': selection['id'] if selection else 'default',
                'model_name': selection['name'] if selection else self.models.default()['model'],
                'model_status': control.model_status if control else None}

    async def recover(self, db: AsyncSession) -> None:
        rows = await db.scalars(select(SessionRuntimeStateORM).where(
            (SessionRuntimeStateORM.approval_correlation_id.is_not(None))
            | (SessionRuntimeStateORM.browser_session_meta_json['in_flight']['kind'].as_string().is_not(None))
        ))
        for state in rows:
            ctx = SessionContext(session_id=state.session_id, branch_id=state.branch_id)
            if state.approval_correlation_id:
                await self.invalidate(db, ctx, state, 'Pending approval expired when the workspace restarted')
                self.update(state, pause_requested=True, run_status=RunStatus.INTERRUPTED, control_holder=ControlHolder.NONE)
            await self.reconcile_effect(db, state)

    async def context(self, session: str, branch: str | None) -> SessionContext:
        async with self.db() as db:
            branch_id = await self.service._resolve_branch_id(db, session, branch)
        return SessionContext(session_id=session, branch_id=branch_id)

    async def lock(self, ctx: SessionContext) -> asyncio.Lock:
        return await self.service.lock_registry.get(ctx.session_id, ctx.branch_id)

    def control(self, ctx: SessionContext) -> BranchControl:
        return self.controls.setdefault((ctx.session_id, ctx.branch_id), BranchControl())

    def current_lease(self, ctx: SessionContext) -> Lease | None:
        control = self.controls.get((ctx.session_id, ctx.branch_id))
        if control and control.lease and monotonic() - control.lease.last_seen > self.settings.workspace_lease_timeout_s:
            control.lease = None
        return control.lease if control else None

    def approval_details(self, state: SessionRuntimeStateORM) -> dict | None:
        control = self.controls.get((state.session_id, state.branch_id))
        pending = control.proposal if control else None
        if pending and state.approval_required and pending.id == state.approval_correlation_id:
            return {**pending.context, 'command': pending.command.model_dump(mode='json', exclude_unset=True)}
        return state.approval_pending_action

    async def state(self, db: AsyncSession, ctx: SessionContext) -> SessionRuntimeStateORM:
        row = await self.repos.state.get(db, session_id=ctx.session_id, branch_id=ctx.branch_id)
        if row is None:
            raise NotFoundError('Browser branch not found')
        return row

    async def event(self, db: AsyncSession, state: SessionRuntimeStateORM, action: str, summary: str, *,
                    actor: Actor = Actor.NAVIGATOR_AGENT, payload: dict | None = None,
                    correlation: str | None = None, sensitive: bool = False, checkpoint: str | None = None) -> None:
        await self.repos.events.add_event(
            db, session_id=state.session_id, branch_id=state.branch_id,
            step_index=int((state.blackboard_json or {}).get('last_step_index', 0)),
            actor=actor.value, action_type=action, action_payload=payload, observation_summary=summary,
            page_url=state.last_known_url, correlation_id=correlation, is_sensitive=sensitive, checkpoint_id=checkpoint,
        )

    def update(self, state: SessionRuntimeStateORM, **changes: Any) -> None:
        for name, value in changes.items():
            setattr(state, name, value)
        state.updated_at = utcnow()

    def browser_state(self, state: SessionRuntimeStateORM, live: BrowserLiveState) -> None:
        self.update(state, last_known_url=live.url, last_known_title=live.title,
                    tabs_json=[tab.model_dump(mode='json') for tab in live.tabs],
                    latest_observation=live.latest_observation)

    async def ensure(self, ctx: SessionContext, restore: BrowserLiveState) -> None:
        current = await self.browser.get_state(ctx)
        if current.revision is not None:
            return
        if restore.url or restore.tabs:
            await self.browser.restore(ctx, restore)
        else:
            await self.browser.ensure_browser(ctx)

    @staticmethod
    def restore_state(state: SessionRuntimeStateORM) -> BrowserLiveState:
        if LiveWorkspace.needs_inspection(state):
            return BrowserLiveState()
        return BrowserLiveState(url=state.last_known_url, title=state.last_known_title,
                                tabs=[TabState.model_validate(tab) for tab in (state.tabs_json or [])])

    @staticmethod
    def needs_inspection(state: SessionRuntimeStateORM) -> bool:
        meta = state.browser_session_meta_json or {}
        return bool(meta.get('in_flight') or meta.get('uncertain'))

    async def record_unknown(self, db: AsyncSession, state: SessionRuntimeStateORM, intent: dict) -> None:
        manual = state.control_mode == ControlMode.MANUAL
        message = 'The browser command may already have taken effect. Inspect the page before continuing.'
        self.update(state, browser_session_meta_json={'uncertain': intent},
                    run_status=RunStatus.MANUAL if manual else RunStatus.INTERRUPTED,
                    pause_requested=True, control_holder=ControlHolder.USER if manual else ControlHolder.NONE,
                    last_error_code='browser_outcome_unknown', last_error_message=message,
                    pending_action_summary=None, blackboard_json=self.remember(state, message))
        await self.event(db, state, 'error', message, actor=Actor.SYSTEM,
                         payload={'error_code': 'browser_outcome_unknown', **intent}, correlation=intent.get('correlation_id'))

    async def reconcile_effect(self, db: AsyncSession, state: SessionRuntimeStateORM) -> bool:
        intent = (state.browser_session_meta_json or {}).get('in_flight')
        if intent is None:
            return False
        await self.record_unknown(db, state, intent)
        return True

    def interrupt(self, ctx: SessionContext) -> None:
        control = self.control(ctx)
        control.version += 1
        if control.model_task and not control.model_task.done():
            control.model_task.cancel()
        if control.capacity_task and not control.capacity_task.done():
            control.capacity_task.cancel()

    async def invalidate(self, db: AsyncSession, ctx: SessionContext, state: SessionRuntimeStateORM, reason: str) -> None:
        correlation = state.approval_correlation_id
        if correlation:
            row = await self.repos.approvals.latest_by_correlation(
                db, session_id=ctx.session_id, branch_id=ctx.branch_id, correlation_id=correlation)
            if row and row.status in {'pending', 'approved'}:
                await self.repos.approvals.decide(db, session_id=ctx.session_id, branch_id=ctx.branch_id,
                                                 correlation_id=correlation, status='rejected',
                                                 decided_by=Actor.SYSTEM.value, reason=reason)
            await self.event(db, state, 'approval_invalidated', reason, actor=Actor.SYSTEM, correlation=correlation)
        self.update(state, approval_required=False, approval_correlation_id=None, approval_pending_action=None,
                    pending_action_summary=None)

    def committed_control_change(self, ctx: SessionContext, state: SessionRuntimeStateORM) -> None:
        control = self.control(ctx)
        if control.proposal and control.proposal.id != state.approval_correlation_id:
            control.proposal = None
        self.interrupt(ctx)

    async def on_instruction(self, db: AsyncSession, ctx: SessionContext, state: SessionRuntimeStateORM, trigger: bool) -> None:
        await self.invalidate(db, ctx, state, 'New user instructions changed the plan')
        if state.control_mode == ControlMode.AGENT:
            self.update(state, pause_requested=not trigger, run_status=RunStatus.QUEUED if trigger else RunStatus.PAUSED,
                        control_holder=ControlHolder.NONE, blackboard_json={**(state.blackboard_json or {}), 'operator_paused': False})

    @staticmethod
    def remember(state: SessionRuntimeStateORM, summary: str, *, step: bool = False) -> dict:
        board = dict(state.blackboard_json or {})
        board['live_history'] = [*(board.get('live_history') or [])[-7:], summary[:320]]
        if step:
            board['agent_steps'] = int(board.get('agent_steps', 0)) + 1
            board['last_step_index'] = int(board.get('last_step_index', 0)) + 1
        return board

    async def prepare_plan(self, db: AsyncSession, state: SessionRuntimeStateORM) -> str | None:
        pending = state.pending_user_instructions_json or []
        board = state.blackboard_json or {}
        goal = (state.task_plan_json or {}).get('goal')
        if pending:
            latest = pending[-1]
            if goal and not board.get('live_done'):
                updates = '\n'.join(entry['content'] for entry in pending)
                new_goal = f'{goal[:5000]}\nUser updates, newest last:\n{updates[-6500:]}'
            else:
                new_goal = latest['content']
                board = {**board, 'live_history': []}
            goal = new_goal
            plan = TaskPlan(goal=goal, subtasks=[Subtask(id=latest['message_id'], title='Browser task', goal=goal)])
            self.update(state, task_plan_json=plan.model_dump(mode='json'), pending_user_instructions_json=[],
                        blackboard_json={**board, 'live_done': False, 'agent_steps': 0}, retry_count=0)
            checkpoint = await self._checkpoint(db, state, 'Task plan', 'planner_checkpoint', actor=Actor.PLANNER_AGENT)
            await self.event(db, state, 'plan', 'Observe the current page and choose one browser action at a time.',
                             actor=Actor.PLANNER_AGENT, payload={'source_message_id': latest['message_id']}, checkpoint=checkpoint)
        if (state.blackboard_json or {}).get('live_done'):
            return None
        return goal

    @staticmethod
    def runnable(state: SessionRuntimeStateORM) -> bool:
        return (state.control_mode == ControlMode.AGENT and not state.pause_requested and not state.approval_required
                and state.run_status != RunStatus.CANCELLED)

    async def process(self, ctx: SessionContext) -> None:
        version = self.control(ctx).version
        try:
            await self._process(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Provider and browser exceptions can contain request bodies or page data.
            self.service.log.warning('Live browser worker failed (%s)', type(exc).__name__)
            async with await self.lock(ctx), self.db() as db:
                state = await self.state(db, ctx)
                if await self.reconcile_effect(db, state):
                    await db.commit()
                    return
                if (version == self.control(ctx).version and state.run_status in {RunStatus.NAVIGATING, RunStatus.QUEUED}
                        and self.runnable(state)):
                    profile_error = isinstance(exc, ConflictError) and exc.error_code == 'model_profile_changed'
                    self.update(state, run_status=RunStatus.FAILED, control_holder=ControlHolder.NONE,
                                last_error_code=exc.error_code if profile_error else 'browser_worker_error',
                                last_error_message=str(exc) if profile_error else 'Browser task failed. Resume to observe the browser again.')
                    await self.event(db, state, 'error', state.last_error_message, actor=Actor.SYSTEM,
                                     payload={'error_type': type(exc).__name__})
                    await db.commit()

    async def wait_browser(self, ctx: SessionContext, lock: asyncio.Lock, control: BranchControl) -> bool:
        async with lock:
            if self.browser.has_capacity(ctx):
                return True
            async with self.db() as db:
                state = await self.state(db, ctx)
                if not self.runnable(state):
                    return False
                self.update(state, run_status=RunStatus.QUEUED, control_holder=ControlHolder.NONE,
                            pending_action_summary='Waiting for a browser slot. Close an unused browser to continue.')
                await db.commit()
            task = asyncio.create_task(self.browser.wait_for_capacity(ctx))
            control.capacity_task = task
        try:
            await task
            return True
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            return False
        finally:
            control.capacity_task = None

    async def decide_with_slot(self, control: BranchControl, planner: BrowserPlanner, *, goal: str,
                               observation: BrowserObservation, history: list[str]) -> AgentDecision:
        control.model_status = 'waiting'
        queued = monotonic()
        try:
            async with self.model_slots:
                control.model_status = 'thinking'
                started = monotonic()
                async with asyncio.timeout(self.settings.browser_agent_timeout_s + 1):
                    decision = await planner.decide(goal=goal, observation=observation, history=history)
                if decision.usage:
                    decision = decision.model_copy(update={'usage': {
                        **decision.usage, 'queue_ms': round((started - queued) * 1000),
                        'duration_ms': round((monotonic() - started) * 1000),
                    }})
                return decision
        finally:
            control.model_status = None

    async def _process(self, ctx: SessionContext) -> None:
        lock = await self.lock(ctx)
        control = self.control(ctx)
        visual = False
        observation_failures = 0
        while True:
            if not self.browser.has_capacity(ctx) and not await self.wait_browser(ctx, lock, control):
                return
            async with lock:
                async with self.db() as db:
                    state = await self.state(db, ctx)
                    if await self.reconcile_effect(db, state):
                        await db.commit()
                        return
                    if not self.runnable(state):
                        return
                    goal = await self.prepare_plan(db, state)
                    if not goal:
                        return
                    board = state.blackboard_json or {}
                    planner, model = self.models.resolve(board, self.planner)
                    if int(board.get('agent_steps', 0)) >= self.settings.browser_agent_max_steps:
                        await self._reply(db, state, 'The action limit was reached. Request a replan to continue.', 'clarify')
                        await db.commit()
                        return
                    self.update(state, run_status=RunStatus.NAVIGATING, control_holder=ControlHolder.AGENT, pending_action_summary=None)
                    if not self.needs_inspection(state):
                        self.update(state, last_error_code=None, last_error_message=None)
                    restore = self.restore_state(state)
                    history = list(board.get('live_history', []))
                    approved = control.proposal if control.proposal and control.proposal.id == state.approval_correlation_id else None
                    await db.commit()
                try:
                    await self.ensure(ctx, restore)
                except BrowserCapacity:
                    continue
                if approved:
                    if not await self._dispatch(ctx, approved.command, approved.revision, approved.id, approved.summary):
                        return
                    continue
                try:
                    observation = await self.browser.observe(ctx, visual=visual)
                except (StaleBrowserState, BrowserUnavailable):
                    observation_failures += 1
                    if observation_failures > self.settings.max_step_retries:
                        raise
                    async with self.db() as db:
                        state = await self.state(db, ctx)
                        await self.event(db, state, 'retry', 'Browser changed during observation. Refreshing current state.',
                                         actor=Actor.SYSTEM, payload={'attempt': observation_failures})
                        await db.commit()
                    continue
                observation_failures = 0
                version = control.version
                task = asyncio.create_task(self.decide_with_slot(control, planner, goal=goal, observation=observation, history=history))
                control.model_task = task
            try:
                decision = await task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                return
            finally:
                if control.model_task is task:
                    control.model_task = None

            async with lock:
                if version != control.version:
                    return
                if decision.kind == 'visual':
                    if model['visual'] and not visual:
                        visual = True
                        continue
                    decision = AgentDecision(kind='clarify', summary='I need a clearer view of this page. Take browser control or give me a more specific instruction.')
                visual = False
                async with self.db() as db:
                    state = await self.state(db, ctx)
                    if not self.runnable(state) or state.run_status != RunStatus.NAVIGATING or state.pending_user_instructions_json:
                        return
                    if decision.usage:
                        await self.event(db, state, 'model_usage', 'Model usage for the latest browser decision',
                                         payload={**decision.usage, 'model_profile': model['id'], 'model_name': model['model']})
                    if decision.kind == 'finish' and observation.url == 'about:blank' and self.requested_urls(goal):
                        retry = await self.retry_invalid_decision(
                            db, state, 'The browser is still blank. Open the requested address and read its page before answering.')
                        if not retry:
                            await self._reply(db, state, 'I could not verify this task in the browser. Open the requested page or give me a more specific instruction.', 'clarify')
                        await db.commit()
                        if retry:
                            continue
                        return
                    if decision.command is None:
                        self.browser_state(state, await self.browser.get_state(ctx))
                        await self._reply(db, state, decision.summary, decision.kind)
                        await db.commit()
                        return
                    command = decision.command
                    if self.needs_inspection(state) or self._needs_approval(command, goal, observation):
                        proposal = await self._request_approval(db, state, command, observation)
                        await db.commit()
                        if proposal:
                            control.proposal = proposal
                        if state.pause_requested:
                            return
                        continue
                    await db.commit()
                if not await self._dispatch(ctx, command, observation.revision, None, decision.summary):
                    return
            await asyncio.sleep(max(0, self.settings.action_rate_limit_ms) / 1000)

    async def _reply(self, db: AsyncSession, state: SessionRuntimeStateORM, summary: str, kind: str) -> None:
        done = kind == 'finish'
        if done and state.task_plan_json:
            plan = TaskPlan.model_validate(state.task_plan_json)
            for subtask in plan.subtasks:
                subtask.status = 'done'
            state.task_plan_json = plan.model_dump(mode='json')
        self.update(state, run_status=RunStatus.COMPLETED if done else RunStatus.AWAITING_INPUT,
                    pause_requested=not done, control_holder=ControlHolder.NONE, pending_action_summary=None,
                    blackboard_json={**(state.blackboard_json or {}), 'live_done': done,
                                     'live_outcome': 'completed' if done else None})
        await self.repos.messages.add_message(db, session_id=state.session_id, branch_id=state.branch_id,
                                              role='assistant', actor=Actor.ASSISTANT.value, content=summary)
        checkpoint = await self._checkpoint(db, state, 'Task complete', 'auto_periodic', actor=Actor.SYSTEM) if done else None
        await self.event(db, state, 'complete' if done else 'clarification', summary, checkpoint=checkpoint)

    async def retry_invalid_decision(self, db: AsyncSession, state: SessionRuntimeStateORM, note: str) -> bool:
        if state.retry_count >= self.settings.max_step_retries:
            return False
        self.update(state, retry_count=state.retry_count + 1, blackboard_json=self.remember(state, note, step=True))
        await self.event(db, state, 'retry', note, actor=Actor.SYSTEM)
        return True

    @staticmethod
    def requested_urls(goal: str) -> set[str]:
        return {url.rstrip('/.,;') for url in re.findall(r'https?://[^\s<>"\']+', goal)}

    def _needs_approval(self, command: BrowserCommand, goal: str, observation: BrowserObservation) -> bool:
        target = next((element for element in observation.elements if element.target_id == command.target_id), None)
        destination = command.url or (target.href if target else None)
        if any(url and not url_allowed(url, [], self.settings.sensitive_domains) for url in (observation.url, destination)):
            return True
        if command.kind == 'open':
            return (command.url or '').rstrip('/') not in self.requested_urls(goal)
        return command.kind not in {'open_link', 'wheel', 'switch_tab'}

    async def _request_approval(self, db: AsyncSession, state: SessionRuntimeStateORM,
                                command: BrowserCommand, observation: BrowserObservation) -> Proposal | None:
        target = next((element for element in observation.elements if element.target_id == command.target_id), None)
        if command.kind in {'type', 'upload', 'click'} and (not target or target.input_type == 'password'):
            retry = target is None and await self.retry_invalid_decision(
                db, state, f'The {command.kind} target was absent from the current page. Use visible evidence or a listed target.',
            )
            if not retry:
                await self._reply(db, state, 'This action needs a visible, non-password target. Use browser control to continue.', 'clarify')
            return None
        summary = f'Allow browser action “{command.kind}”' + (f' on “{target.label[:160]}”' if target else '')
        context = {'kind': command.kind, 'target': target.label[:160] if target else None,
                   'url': public_url(command.url) if command.url else observation.url, 'tab_id': observation.tab_id,
                   'revision': observation.revision}
        if target and target.form_action:
            context.update(form_action=target.form_action, form_method=target.form_method)
        proposal = Proposal(str(uuid4()), command, observation.revision, summary, context)
        checkpoint = await self._checkpoint(db, state, 'Before action approval', 'pre_sensitive_action', actor=Actor.SYSTEM)
        await self.repos.approvals.create_pending(db, session_id=state.session_id, branch_id=state.branch_id,
                                                 correlation_id=proposal.id, requested_action_json=context)
        self.update(state, approval_required=True, approval_correlation_id=proposal.id, approval_pending_action=context,
                    pending_action_summary=summary, run_status=RunStatus.AWAITING_APPROVAL,
                    control_holder=ControlHolder.NONE, pause_requested=True)
        await self.event(db, state, 'approval_requested', summary, payload=context, correlation=proposal.id,
                         sensitive=True, checkpoint=checkpoint)
        return proposal

    async def _dispatch(self, ctx: SessionContext, command: BrowserCommand, revision: str,
                        correlation: str | None, summary: str) -> bool:
        # The caller owns the branch lock. Commit intent before performing the side effect.
        intent = {'kind': command.kind, 'correlation_id': correlation}
        async with self.db() as db:
            state = await self.state(db, ctx)
            self.update(state, approval_correlation_id=None, approval_pending_action=None,
                        pending_action_summary=summary, browser_session_meta_json={
                            **(state.browser_session_meta_json or {}), 'in_flight': intent})
            await db.commit()
        if correlation is not None:
            self.control(ctx).proposal = None
        error = None
        started = monotonic()
        try:
            result = await self.browser.execute(ctx, command, expected_revision=revision)
        except Exception as exc:
            error = exc
        live = await self.browser.get_state(ctx)
        async with self.db() as db:
            state = await self.state(db, ctx)
            self.browser_state(state, live)
            payload = {'kind': command.kind, 'duration_ms': round((monotonic() - started) * 1000)}
            if error is not None:
                if not isinstance(error, (ConflictError, ValidationError)):
                    await self.record_unknown(db, state, intent)
                    await db.commit()
                    return False
                meta = dict(state.browser_session_meta_json or {})
                meta.pop('in_flight', None)
                self.update(state, pending_action_summary=None, browser_session_meta_json=meta)
                failures = state.retry_count + 1
                retry = isinstance(error, StaleBrowserState) and correlation is None and failures <= self.settings.max_step_retries
                note = (f'{command.kind} did not complete. Observe the page before choosing another action.' if retry else
                        f'{command.kind} did not complete. Review the page before resuming.')
                self.update(state, blackboard_json=self.remember(state, note, step=True), retry_count=failures)
                if not retry:
                    self.update(state, run_status=RunStatus.INTERRUPTED, pause_requested=True, control_holder=ControlHolder.NONE,
                                last_error_code='browser_action_failed', last_error_message=note)
                await self.event(db, state, 'retry' if retry else 'error', note, payload={**payload, 'error_type': type(error).__name__},
                                 correlation=correlation)
                await db.commit()
                return retry
            result_context = result.summary
            if command.kind in {'open', 'open_link', 'new_tab', 'back', 'forward', 'switch_tab'}:
                result_context = f'{result.summary}. Current page is {live.title or "untitled"} at {live.url or "unknown URL"}.'
            self.update(state, blackboard_json=self.remember(state, result_context, step=True), retry_count=0,
                        pending_action_summary=None, browser_session_meta_json={}, last_error_code=None, last_error_message=None)
            await self.event(db, state, command.kind, result.summary, payload=payload, correlation=correlation,
                             sensitive=correlation is not None)
            await db.commit()
            return True

    def response(self, state: SessionRuntimeStateORM, message: str, lease: Lease | None = None) -> ControlActionResponse:
        return ControlActionResponse(ok=True, run_status=state.run_status, control_mode=state.control_mode,
                                     message=message, lease_id=lease.id if lease else None)

    async def _checkpoint(self, db: AsyncSession, state: SessionRuntimeStateORM, label: str, reason: str, *,
                          actor: Actor = Actor.USER) -> str:
        checkpoint = await create_checkpoint(
            db, session_id=state.session_id, branch_id=state.branch_id, label=label, reason=reason,
            step_index=int((state.blackboard_json or {}).get('last_step_index', 0)),
            snapshot_json=await snapshot_from_runtime_state(db, state.session_id, state.branch_id),
            created_by_actor=actor.value,
        )
        return checkpoint.id

    async def takeover(self, ctx: SessionContext, client: str) -> ControlActionResponse:
        async with await self.lock(ctx):
            control = self.control(ctx)
            async with self.db() as db:
                state = await self.state(db, ctx)
                if await self.reconcile_effect(db, state):
                    await db.commit()
                if not self.has_manual_control(state):
                    control.lease = None
                existing = self.current_lease(ctx)
                if existing:
                    if existing.client != client:
                        raise ConflictError('Another workspace tab holds browser control')
                restore = self.restore_state(state)
            await self.ensure(ctx, restore)
            if existing:
                existing.last_seen = monotonic()
                return self.response(state, 'You control the browser', existing)
            live = await self.browser.get_state(ctx)
            lease = Lease(str(uuid4()), client)
            async with self.db() as db:
                state = await self.state(db, ctx)
                await self.invalidate(db, ctx, state, 'Manual control replaced the pending browser action')
                self.browser_state(state, live)
                checkpoint = await self._checkpoint(db, state, 'Before manual control', 'manual_before_takeover')
                self.update(state, control_mode=ControlMode.MANUAL, control_holder=ControlHolder.USER,
                            run_status=RunStatus.MANUAL, pause_requested=True)
                await self.event(db, state, 'takeover', 'User took exclusive browser control', actor=Actor.USER,
                                 payload={'checkpoint_id': checkpoint})
                await db.commit()
            self.committed_control_change(ctx, state)
            control.lease = lease
            return self.response(state, 'You control the browser', lease)

    @staticmethod
    def has_manual_control(state: SessionRuntimeStateORM) -> bool:
        return state.control_mode == ControlMode.MANUAL and state.control_holder == ControlHolder.USER

    def require_lease(self, ctx: SessionContext, state: SessionRuntimeStateORM,
                      client: str | None, lease_id: str | None) -> Lease:
        if not self.has_manual_control(state):
            self.control(ctx).lease = None
            raise ConflictError('Browser control changed. Acquire a new manual lease.')
        lease = self.current_lease(ctx)
        if not lease or lease.client != client or lease.id != lease_id:
            raise ConflictError('Browser control changed. Reconnect to the current manual controller.')
        lease.last_seen = monotonic()
        return lease

    async def release(self, ctx: SessionContext, client: str | None, lease_id: str | None,
                      note: str | None, resume: bool) -> ControlActionResponse:
        async with await self.lock(ctx):
            async with self.db() as db:
                self.require_lease(ctx, await self.state(db, ctx), client, lease_id)
            live = await self.browser.get_state(ctx)
            async with self.db() as db:
                state = await self.state(db, ctx)
                await self.reconcile_effect(db, state)
                self.browser_state(state, live)
                board = self.remember(state, f'Human returned control at {live.url or "a closed browser"}. Observe the current page. {note or ""}')
                can_run = bool(state.pending_user_instructions_json or
                               ((state.task_plan_json or {}).get('goal') and not board.get('live_done')))
                uncertain = self.needs_inspection(state)
                schedule = resume and can_run and not uncertain
                if schedule:
                    board['operator_paused'] = False
                self.update(state, control_mode=ControlMode.AGENT, control_holder=ControlHolder.NONE,
                            run_status=RunStatus.INTERRUPTED if uncertain else (
                                (RunStatus.QUEUED if schedule else RunStatus.PAUSED) if can_run else
                                board.get('live_outcome') or RunStatus.IDLE),
                            pause_requested=not schedule, blackboard_json=board)
                checkpoint = await self._checkpoint(db, state, 'After manual control', 'manual_release')
                await self.event(db, state, 'release', 'User returned control. The next step will observe the browser again.',
                                 actor=Actor.USER, payload={'checkpoint_id': checkpoint, 'resume': schedule})
                await db.commit()
            self.committed_control_change(ctx, state)
            self.control(ctx).lease = None
            if schedule:
                await self.service.execution_manager.schedule(ctx.session_id, ctx.branch_id)
            message = 'Inspect the browser before resuming' if uncertain else (
                'Control returned' if schedule else 'Control returned with the agent paused')
            return self.response(state, message)

    async def close_browser(self, ctx: SessionContext, client: str | None, lease_id: str | None) -> ControlActionResponse:
        async with await self.lock(ctx):
            async with self.db() as db:
                state = await self.state(db, ctx)
                if state.control_mode == ControlMode.MANUAL and self.current_lease(ctx):
                    self.require_lease(ctx, state, client, lease_id)
                if state.run_status == RunStatus.NAVIGATING:
                    raise ConflictError('Pause the agent before closing the browser')
            live = await self.browser.get_state(ctx)
            await self.browser.close(ctx)
            async with self.db() as db:
                state = await self.state(db, ctx)
                await self.reconcile_effect(db, state)
                if live.revision:
                    self.browser_state(state, live)
                await self.invalidate(db, ctx, state, 'The browser closed before the pending action')
                outcome = (state.blackboard_json or {}).get('live_outcome')
                self.update(state, control_mode=ControlMode.AGENT, control_holder=ControlHolder.NONE,
                            run_status=outcome or RunStatus.PAUSED, pause_requested=True)
                await self.event(db, state, 'browser_closed', 'Closed the browser and kept the branch history', actor=Actor.USER)
                await db.commit()
            self.committed_control_change(ctx, state)
            self.control(ctx).lease = None
            return self.response(state, 'Browser closed. Take control or resume to reopen it.')

    async def change_run(self, ctx: SessionContext, operation: str) -> ControlActionResponse:
        async with await self.lock(ctx), self.db() as db:
            state = await self.state(db, ctx)
            if await self.reconcile_effect(db, state):
                await db.commit()
                if operation in {'resume', 'replan'}:
                    raise ConflictError('A prior browser command may have taken effect. Inspect the page before resuming.',
                                        error_code='browser_outcome_unknown')
            schedule = False
            if operation == 'pause':
                if state.control_mode == ControlMode.MANUAL or state.run_status in {RunStatus.CANCELLED, RunStatus.COMPLETED, RunStatus.IDLE}:
                    raise ConflictError('There is no running agent to pause')
                self.update(state, pause_requested=True, control_holder=ControlHolder.NONE,
                            blackboard_json={**(state.blackboard_json or {}), 'operator_paused': True},
                            run_status=RunStatus.AWAITING_APPROVAL if state.approval_required else RunStatus.PAUSED)
                message = 'Agent paused at a browser command boundary'
            elif operation == 'stop':
                await self.invalidate(db, ctx, state, 'The user stopped this run')
                board = {**(state.blackboard_json or {}), 'live_done': True, 'live_outcome': 'cancelled'}
                self.update(state, run_status=RunStatus.CANCELLED, pause_requested=True, blackboard_json=board,
                            pending_user_instructions_json=[],
                            control_holder=ControlHolder.USER if state.control_mode == ControlMode.MANUAL else ControlHolder.NONE)
                message = 'Run stopped. Send a new instruction to start another run.'
            elif operation in {'resume', 'replan'}:
                if state.control_mode == ControlMode.MANUAL:
                    raise ConflictError('Return browser control before resuming or replanning')
                if state.run_status == RunStatus.CANCELLED:
                    raise ConflictError('This run was stopped. Send a new instruction.')
                if operation == 'resume' and state.approval_required:
                    raise ConflictError('Decide the pending action before resuming')
                if operation == 'resume' and state.run_status in {RunStatus.NAVIGATING, RunStatus.QUEUED} and not state.pause_requested:
                    if state.run_status == RunStatus.QUEUED:
                        await self.service.execution_manager.schedule(ctx.session_id, ctx.branch_id)
                    return self.response(state, 'Agent is already active')
                if operation == 'replan':
                    await self.invalidate(db, ctx, state, 'The user requested a fresh plan')
                    self.update(state, retry_count=0, blackboard_json={
                        **self.remember(state, 'The user asked for a fresh plan from current browser state.'),
                        'agent_steps': 0, 'live_done': False,
                    })
                if not (state.pending_user_instructions_json or
                        ((state.task_plan_json or {}).get('goal') and not (state.blackboard_json or {}).get('live_done'))):
                    raise ConflictError('Start a task by sending a browser instruction')
                self.update(state, run_status=RunStatus.QUEUED, pause_requested=False, control_holder=ControlHolder.NONE,
                            blackboard_json={**(state.blackboard_json or {}), 'operator_paused': False})
                message = 'Replanning from the current browser' if operation == 'replan' else 'Agent resuming from the current browser'
                schedule = True
            else:
                raise ValidationError('Unknown run operation')
            await self.event(db, state, operation, message, actor=Actor.USER)
            await db.commit()
            self.committed_control_change(ctx, state)
            if schedule:
                await self.service.execution_manager.schedule(ctx.session_id, ctx.branch_id)
            return self.response(state, message)

    async def decide(self, ctx: SessionContext, correlation: str, decision: str, note: str | None) -> ControlActionResponse:
        async with await self.lock(ctx), self.db() as db:
            state = await self.state(db, ctx)
            proposal = self.control(ctx).proposal
            if not (state.approval_required and state.approval_correlation_id == correlation and proposal and proposal.id == correlation):
                raise ConflictError('This action is no longer awaiting a decision')
            schedule = False
            if decision == 'approve':
                live = await self.browser.get_state(ctx)
                if live.revision != proposal.revision:
                    await self.invalidate(db, ctx, state, 'Browser changed before approval')
                    self.update(state, run_status=RunStatus.PAUSED, pause_requested=True)
                    await db.commit()
                    self.committed_control_change(ctx, state)
                    raise ConflictError('The browser changed. Resume to request a fresh action.')
                await self.repos.approvals.decide(db, session_id=ctx.session_id, branch_id=ctx.branch_id,
                                                 correlation_id=correlation, status='approved',
                                                 decided_by=Actor.USER.value, reason=note)
                paused = bool((state.blackboard_json or {}).get('operator_paused'))
                self.update(state, approval_required=False, pause_requested=paused,
                            run_status=RunStatus.PAUSED if paused else RunStatus.QUEUED)
                schedule = not paused
                message = 'Exact browser action approved. Resume when ready.' if paused else 'Exact browser action approved'
            else:
                await self.repos.approvals.decide(db, session_id=ctx.session_id, branch_id=ctx.branch_id,
                                                 correlation_id=correlation, status='rejected',
                                                 decided_by=Actor.USER.value, reason=note or decision)
                self.update(state, approval_required=False, approval_correlation_id=None, approval_pending_action=None,
                            pending_action_summary=None, run_status=RunStatus.PAUSED, pause_requested=True,
                            blackboard_json=self.remember(state, f'User did not authorize {proposal.summary}. {note or ""}'))
                message = 'Action declined. Resume to choose a new step.'
            await self.event(db, state, decision, message, actor=Actor.USER, correlation=correlation)
            await db.commit()
            self.committed_control_change(ctx, state)
            if schedule:
                await self.service.execution_manager.schedule(ctx.session_id, ctx.branch_id)
            return self.response(state, message)

    async def edit_action(self, ctx: SessionContext, correlation: str, command: BrowserCommand) -> ControlActionResponse:
        async with await self.lock(ctx), self.db() as db:
            state = await self.state(db, ctx)
            proposal = self.control(ctx).proposal
            if not (state.approval_required and state.approval_correlation_id == correlation and proposal and proposal.id == correlation):
                raise ConflictError('This action is no longer awaiting a decision')
            original = proposal.command
            if (command.kind, command.target_id, command.tab_id) != (original.kind, original.target_id, original.tab_id):
                raise ValidationError('Keep the action kind and target. Cancel and replan to change the target.')
            live = await self.browser.get_state(ctx)
            if live.revision != proposal.revision:
                raise ConflictError('The browser changed. Cancel this action and resume for a fresh proposal.')
            context = {**proposal.context, 'url': public_url(command.url) if command.url else live.url, 'edited': True}
            updated = Proposal(str(uuid4()), command, proposal.revision, proposal.summary, context)
            await self.invalidate(db, ctx, state, 'User replaced the proposed browser command')
            await self.repos.approvals.create_pending(db, session_id=ctx.session_id, branch_id=ctx.branch_id,
                                                     correlation_id=updated.id, requested_action_json=context)
            self.update(state, approval_required=True, approval_correlation_id=updated.id, approval_pending_action=context,
                        pending_action_summary=updated.summary)
            await self.event(db, state, 'action_edited', 'User edited the pending command. Review the updated proposal.',
                             actor=Actor.USER, correlation=updated.id, sensitive=True)
            await db.commit()
            self.control(ctx).proposal = updated
            return self.response(state, 'Action updated and awaiting approval')

    def describe_browser(self, ctx: SessionContext, live: BrowserLiveState, client: str | None) -> dict:
        lease = self.current_lease(ctx)
        if lease and lease.client == client:
            lease.last_seen = monotonic()
        return {**live.model_dump(mode='json', exclude={'screenshot_path', 'latest_observation'}),
                'ready': live.revision is not None, 'lease_id': lease.id if lease and lease.client == client else None}

    async def manual_command(self, ctx: SessionContext, *, client: str | None, lease_id: str,
                             command_id: str, revision: str, command: BrowserCommand, frame_id: str | None = None) -> dict:
        admitted = asyncio.Event()
        worker = asyncio.create_task(self._manual_command(ctx, client, lease_id, command_id, revision, command, frame_id, admitted))
        self._manual_commands.add(worker)
        worker.add_done_callback(self._manual_finished)
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            if not admitted.is_set():
                worker.cancel()
            raise

    def _manual_finished(self, worker: asyncio.Task[dict]) -> None:
        self._manual_commands.discard(worker)
        if not worker.cancelled():
            worker.exception()

    async def _manual_command(self, ctx: SessionContext, client: str | None, lease_id: str,
                              command_id: str, revision: str, command: BrowserCommand, frame_id: str | None,
                              admitted: asyncio.Event) -> dict:
        async with await self.lock(ctx):
            async with self.db() as db:
                state = await self.state(db, ctx)
                lease = self.require_lease(ctx, state, client, lease_id)
                fingerprint = sha256(f'{revision}:{frame_id}:{command.model_dump_json(exclude_none=True)}'.encode()).hexdigest()
                if command_id in lease.commands:
                    previous, response = lease.commands[command_id]
                    if previous != fingerprint or response is None:
                        raise ConflictError('Command ID was used. Refresh the browser before issuing another command.')
                    return response
                if await self.reconcile_effect(db, state):
                    await db.commit()
                    raise ConflictError('A prior browser command may have taken effect. Inspect the page before continuing.',
                                        error_code='browser_outcome_unknown')
                admitted.set()
                intent = {'kind': command.kind, 'actor': 'user', 'command_id': command_id}
                self.update(state, browser_session_meta_json={**(state.browser_session_meta_json or {}), 'in_flight': intent})
                await db.commit()
            if len(lease.commands) >= 128:
                lease.commands.popitem(last=False)
            lease.commands[command_id] = (fingerprint, None)
            error = None
            try:
                await self.browser.execute(ctx, command, expected_revision=revision, frame_id=frame_id)
            except Exception as exc:
                error = exc
            live = await self.browser.get_state(ctx)
            async with self.db() as db:
                state = await self.state(db, ctx)
                self.browser_state(state, live)
                if error is None:
                    self.update(state, browser_session_meta_json={}, last_error_code=None, last_error_message=None)
                elif isinstance(error, (ConflictError, ValidationError)):
                    meta = dict(state.browser_session_meta_json or {})
                    meta.pop('in_flight', None)
                    self.update(state, browser_session_meta_json=meta)
                else:
                    await self.record_unknown(db, state, intent)
                await self.event(db, state, 'manual_input' if error is None else 'manual_input_failed',
                                 f'User browser command: {command.kind}', actor=Actor.USER,
                                 payload={'kind': command.kind, 'command_id': command_id})
                await db.commit()
            if error is not None:
                if isinstance(error, (ConflictError, ValidationError)):
                    raise error
                raise ConflictError('The browser command may already have taken effect. Inspect the page before continuing.',
                                    error_code=BrowserOutcomeUnknown.error_code) from None
            response = self.describe_browser(ctx, live, client)
            lease.commands[command_id] = (fingerprint, response)
            return response
