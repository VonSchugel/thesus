from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.safety import is_sensitive_action
from app.db.repo_approvals import ApprovalRepository
from app.db.repo_events import EventRepository
from app.db.repo_state import RuntimeStateRepository
from app.integrations.browser.base import BrowserAdapter
from app.models.domain import PlannedAction, SessionContext, TaskPlan
from app.models.enums import ActionType, Actor, RunStatus


class NavigatorAgent:
    def __init__(self, *, browser_adapter: BrowserAdapter, event_repo: EventRepository, state_repo: RuntimeStateRepository, approval_repo: ApprovalRepository, settings: Any) -> None:
        self.browser_adapter = browser_adapter
        self.event_repo = event_repo
        self.state_repo = state_repo
        self.approval_repo = approval_repo
        self.settings = settings

    async def execute_pending_approved_action(self, db: AsyncSession, *, ctx: SessionContext, step_index: int) -> bool:
        state = await self.state_repo.get(db, session_id=ctx.session_id, branch_id=ctx.branch_id)
        if not state or not state.approval_pending_action or not state.approval_correlation_id:
            return False
        decision = await self.approval_repo.latest_by_correlation(db, session_id=ctx.session_id, branch_id=ctx.branch_id, correlation_id=state.approval_correlation_id)
        if not decision or decision.status != 'approved':
            return False
        action = PlannedAction.model_validate(state.approval_pending_action)
        result = await self.browser_adapter.execute_action(session_ctx=ctx, action=action, step_index=step_index)
        await self._persist_navigation_result(db, ctx=ctx, step_index=step_index, result=result)
        state_after = await self.state_repo.get(db, session_id=ctx.session_id, branch_id=ctx.branch_id)
        if state_after and state_after.task_plan_json:
            plan = TaskPlan.model_validate(state_after.task_plan_json)
            for subtask in plan.subtasks:
                if subtask.status != 'done':
                    subtask.status = 'done'
                    break
            await self.state_repo.upsert(
                db,
                session_id=ctx.session_id,
                branch_id=ctx.branch_id,
                task_plan_json=plan.model_dump(mode='json'),
            )
            await self._update_blackboard_from_result(
                db,
                ctx=ctx,
                subtask_title='Approved sensitive action',
                result=result,
            )
        await self.state_repo.upsert(
            db,
            session_id=ctx.session_id,
            branch_id=ctx.branch_id,
            approval_required=False,
            approval_pending_action=None,
            approval_correlation_id=None,
            run_status=RunStatus.NAVIGATING.value,
            pending_action_summary=None,
        )
        return True

    async def navigate_plan(self, db: AsyncSession, *, ctx: SessionContext, plan: TaskPlan) -> None:
        await self.browser_adapter.start(ctx)
        await self.state_repo.upsert(db, session_id=ctx.session_id, branch_id=ctx.branch_id, run_status=RunStatus.NAVIGATING.value, control_holder=Actor.NAVIGATOR_AGENT.value)
        state = await self.state_repo.get(db, session_id=ctx.session_id, branch_id=ctx.branch_id)
        step_index = int(((state.blackboard_json or {}) if state else {}).get('last_step_index', 0))
        for subtask in plan.subtasks:
            latest_state = await self.state_repo.get(db, session_id=ctx.session_id, branch_id=ctx.branch_id)
            if latest_state is None:
                return
            if subtask.status == 'done':
                continue
            if latest_state.control_mode == 'manual':
                await self.state_repo.upsert(db, session_id=ctx.session_id, branch_id=ctx.branch_id, run_status=RunStatus.MANUAL.value)
                return
            if latest_state.pause_requested:
                await self.browser_adapter.pause(ctx)
                await self.state_repo.upsert(db, session_id=ctx.session_id, branch_id=ctx.branch_id, run_status=RunStatus.PAUSED.value)
                return
            if latest_state.approval_required:
                await self.browser_adapter.pause(ctx)
                await self.state_repo.upsert(db, session_id=ctx.session_id, branch_id=ctx.branch_id, run_status=RunStatus.AWAITING_APPROVAL.value)
                return

            step_index += 1

            async def before_action(action: PlannedAction) -> bool:
                if is_sensitive_action(action, getattr(self.settings, 'sensitive_domains', [])):
                    corr = str(uuid4())
                    action.correlation_id = corr
                    await self.approval_repo.create_pending(
                        db,
                        session_id=ctx.session_id,
                        branch_id=ctx.branch_id,
                        correlation_id=corr,
                        requested_action_json=action.model_dump(mode='json'),
                    )
                    await self.event_repo.add_event(
                        db,
                        session_id=ctx.session_id,
                        branch_id=ctx.branch_id,
                        step_index=step_index,
                        actor=Actor.NAVIGATOR_AGENT.value,
                        action_type=ActionType.NOTE.value,
                        action_payload={'planned_action': action.model_dump(mode='json'), 'reason': 'approval_required'},
                        observation_summary='Sensitive action requires approval before execution',
                        page_url=action.page_url,
                        correlation_id=corr,
                        is_sensitive=True,
                    )
                    await self.state_repo.upsert(
                        db,
                        session_id=ctx.session_id,
                        branch_id=ctx.branch_id,
                        approval_required=True,
                        approval_pending_action=action.model_dump(mode='json'),
                        approval_correlation_id=corr,
                        run_status=RunStatus.AWAITING_APPROVAL.value,
                        pending_action_summary=action.summary,
                    )
                    await self.browser_adapter.pause(ctx)
                    return False
                await self.state_repo.upsert(db, session_id=ctx.session_id, branch_id=ctx.branch_id, pending_action_summary=action.summary)
                return True

            result = await self.browser_adapter.run_navigation_step(session_ctx=ctx, subtask=subtask, step_index=step_index, before_action=before_action)
            if not result.executed:
                return
            await self._persist_navigation_result(db, ctx=ctx, step_index=step_index, result=result)
            subtask.status = 'done' if result.completed_subtask else subtask.status
            await self._update_blackboard_from_result(db, ctx=ctx, subtask_title=subtask.title, result=result)
            await self.state_repo.upsert(db, session_id=ctx.session_id, branch_id=ctx.branch_id, task_plan_json=plan.model_dump(mode='json'), pending_action_summary=None)
            await asyncio.sleep(max(0, getattr(self.settings, 'action_rate_limit_ms', 0)) / 1000.0)

    async def _persist_navigation_result(self, db: AsyncSession, *, ctx: SessionContext, step_index: int, result: Any) -> None:
        action = result.planned_action
        await self.event_repo.add_event(
            db,
            session_id=ctx.session_id,
            branch_id=ctx.branch_id,
            step_index=step_index,
            actor=Actor.NAVIGATOR_AGENT.value,
            action_type=action.action_type.value,
            action_payload=action.model_dump(mode='json'),
            observation_summary=result.observation_summary,
            page_url=result.page_url,
            screenshot_path=result.screenshot_path,
            raw_event_json=result.raw_event,
            correlation_id=action.correlation_id,
        )
        if result.live_state is not None:
            await self.state_repo.upsert(
                db,
                session_id=ctx.session_id,
                branch_id=ctx.branch_id,
                last_known_url=result.live_state.url,
                last_known_title=result.live_state.title,
                tabs_json=[t.model_dump(mode='json') if hasattr(t, 'model_dump') else t for t in result.live_state.tabs],
                latest_observation=result.observation_summary,
            )

    async def _update_blackboard_from_result(self, db: AsyncSession, *, ctx: SessionContext, subtask_title: str, result: Any) -> None:
        state = await self.state_repo.get(db, session_id=ctx.session_id, branch_id=ctx.branch_id)
        if state is None:
            return
        blackboard = dict(state.blackboard_json or {})
        findings = list(blackboard.get('findings', []))
        findings.append({'subtask': subtask_title, 'observation': result.observation_summary, 'url': result.page_url, 'title': subtask_title})
        blackboard['findings'] = findings
        blackboard['last_step_index'] = int(blackboard.get('last_step_index', 0)) + 1
        await self.state_repo.upsert(db, session_id=ctx.session_id, branch_id=ctx.branch_id, blackboard_json=blackboard)

