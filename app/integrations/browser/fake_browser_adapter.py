from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from app.integrations.browser.base import BeforeActionHook, BrowserAdapter
from app.models.domain import BrowserLiveState, NavigationStepResult, PlannedAction, SessionContext, SnapshotResult, Subtask, TabState
from app.models.enums import ActionType


@dataclass
class _BranchFakeState:
    url: str = 'about:blank'
    title: str = 'Blank'
    tabs: list[TabState] = field(default_factory=lambda: [TabState(url='about:blank', title='Blank', tab_id='tab-1')])
    paused: bool = False
    step_counter: int = 0
    executed_actions: list[dict[str, Any]] = field(default_factory=list)


class FakeBrowserAdapter(BrowserAdapter):
    def __init__(self, step_delay_s: float = 0.05) -> None:
        self._states: dict[tuple[str, str], _BranchFakeState] = {}
        self.step_delay_s = step_delay_s

    def _key(self, ctx: SessionContext) -> tuple[str, str]:
        return (ctx.session_id, ctx.branch_id)

    def _state(self, ctx: SessionContext) -> _BranchFakeState:
        key = self._key(ctx)
        if key not in self._states:
            self._states[key] = _BranchFakeState()
        return self._states[key]

    async def start(self, session_ctx: SessionContext) -> None:
        self._state(session_ctx)

    async def ensure_browser(self, session_ctx: SessionContext) -> None:
        self._state(session_ctx)

    def _plan_for_subtask(self, subtask: Subtask, st: _BranchFakeState) -> PlannedAction:
        text = f"{subtask.title} {subtask.goal}".lower()
        if any(k in text for k in ['purchase', 'buy', 'checkout', 'pay']):
            return PlannedAction(
                action_type=ActionType.CLICK,
                summary='Click submit/purchase button',
                payload={'target': 'submit', 'intent': 'purchase'},
                page_url=st.url,
            )
        if any(k in text for k in ['find', 'search', 'deal', 'laptop']):
            q = quote(subtask.goal)
            return PlannedAction(
                action_type=ActionType.OPEN,
                summary=f'Open search results for {subtask.goal}',
                payload={'url': f'https://example.com/search?q={q}'},
                page_url=st.url,
            )
        if 'compare' in text:
            return PlannedAction(
                action_type=ActionType.EXTRACT,
                summary='Extract comparison details',
                payload={'fields': ['name', 'price', 'specs']},
                page_url=st.url,
            )
        return PlannedAction(
            action_type=ActionType.NOTE,
            summary=f'Note progress on {subtask.title}',
            payload={'note': subtask.goal},
            page_url=st.url,
        )

    async def run_navigation_step(
        self,
        *,
        session_ctx: SessionContext,
        subtask: Subtask,
        step_index: int,
        before_action: BeforeActionHook | None = None,
    ) -> NavigationStepResult:
        st = self._state(session_ctx)
        action = self._plan_for_subtask(subtask, st)
        if before_action is not None:
            allow = await before_action(action)
            if not allow:
                return NavigationStepResult(
                    planned_action=action,
                    executed=False,
                    observation_summary='Execution paused pending user approval or control change',
                    page_url=st.url,
                    completed_subtask=False,
                    live_state=self._to_live_state(st),
                )
        return await self.execute_action(session_ctx=session_ctx, action=action, step_index=step_index)

    async def execute_action(self, *, session_ctx: SessionContext, action: PlannedAction, step_index: int) -> NavigationStepResult:
        st = self._state(session_ctx)
        if self.step_delay_s > 0:
            await asyncio.sleep(self.step_delay_s)
        st.step_counter += 1
        if action.action_type == ActionType.OPEN:
            new_url = str(action.payload.get('url', f'https://example.com/step/{st.step_counter}'))
            st.url = new_url
            st.title = f'Page {st.step_counter}'
            st.tabs[0] = TabState(url=st.url, title=st.title, tab_id='tab-1')
            obs = f'Opened {new_url}'
        elif action.action_type == ActionType.CLICK and action.payload.get('target') == 'submit':
            obs = 'Submit action executed (fake adapter)'
        elif action.action_type == ActionType.EXTRACT:
            obs = 'Extracted structured product details from current page'
        else:
            obs = f'Executed {action.action_type.value}'
        st.executed_actions.append({'step_index': step_index, 'action': action.model_dump(mode='json')})
        return NavigationStepResult(
            planned_action=action,
            executed=True,
            observation_summary=obs,
            page_url=st.url,
            completed_subtask=True,
            live_state=self._to_live_state(st, obs),
            raw_event={'adapter': 'fake', 'step_counter': st.step_counter},
        )

    async def pause(self, session_ctx: SessionContext) -> None:
        self._state(session_ctx).paused = True

    async def resume(self, session_ctx: SessionContext) -> None:
        self._state(session_ctx).paused = False

    async def get_state(self, session_ctx: SessionContext) -> BrowserLiveState:
        return self._to_live_state(self._state(session_ctx))

    async def capture_snapshot(self, session_ctx: SessionContext) -> SnapshotResult:
        st = self._state(session_ctx)
        return SnapshotResult(live_state=self._to_live_state(st), metadata={'adapter': 'fake', 'step_counter': st.step_counter})

    async def close(self, session_ctx: SessionContext) -> None:
        self._states.pop(self._key(session_ctx), None)

    def _to_live_state(self, st: _BranchFakeState, latest_observation: str | None = None) -> BrowserLiveState:
        return BrowserLiveState(url=st.url, title=st.title, tabs=list(st.tabs), latest_observation=latest_observation)

    def get_executed_actions(self, session_id: str, branch_id: str) -> list[dict[str, Any]]:
        state = self._states.get((session_id, branch_id))
        return list(state.executed_actions) if state else []

