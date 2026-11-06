from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any

from app.integrations.browser.base import BeforeActionHook, BrowserAdapter
from app.models.domain import (
    BrowserLiveState,
    NavigationStepResult,
    PlannedAction,
    SessionContext,
    SnapshotResult,
    Subtask,
    TabState,
)
from app.models.enums import ActionType


@dataclass
class _BranchRuntime:
    paused: bool = False
    block_current_step: bool = False
    before_action_hook: BeforeActionHook | None = None
    pending_action: PlannedAction | None = None
    blocked_subtask_goal: str | None = None


class BrowserUseAdapter(BrowserAdapter):
    """Lazy-import browser-use adapter for real browsing.

    Supports `ollama` and `openai` via env-backed settings.
    """

    def __init__(self, *, headless: bool = False, profile_path_getter: Any | None = None, settings: Any | None = None) -> None:
        self.headless = headless
        self.profile_path_getter = profile_path_getter
        self.settings = settings
        self._sessions: dict[tuple[str, str], Any] = {}
        self._agents: dict[tuple[str, str], Any] = {}
        self._runtime: dict[tuple[str, str], _BranchRuntime] = {}

    def _key(self, ctx: SessionContext) -> tuple[str, str]:
        return (ctx.session_id, ctx.branch_id)

    def _branch_runtime(self, ctx: SessionContext) -> _BranchRuntime:
        key = self._key(ctx)
        runtime = self._runtime.get(key)
        if runtime is None:
            runtime = _BranchRuntime()
            self._runtime[key] = runtime
        return runtime

    async def _imports(self) -> tuple[Any, Any, Any]:
        try:
            from browser_use.agent.service import Agent
            from browser_use.browser.session import BrowserSession
            from browser_use.browser.profile import BrowserProfile
        except Exception as exc:
            raise RuntimeError('browser-use import failed. Install browser-use and optional dependencies.') from exc
        return Agent, BrowserSession, BrowserProfile

    def _make_llm(self) -> Any:
        provider = (getattr(self.settings, 'browser_use_llm_provider', 'ollama') or 'ollama').lower()
        if provider == 'openai':
            try:
                from browser_use.llm.openai.chat import ChatOpenAI
            except Exception as exc:
                raise RuntimeError('OpenAI browser-use LLM backend unavailable. Install browser-use openai deps.') from exc
            api_key = getattr(self.settings, 'openai_api_key', None)
            if not api_key:
                raise RuntimeError('OPENAI_API_KEY is required when BROWSER_USE_LLM_PROVIDER=openai')
            return ChatOpenAI(
                model=getattr(self.settings, 'openai_model_navigator', 'gpt-4o-mini'),
                api_key=api_key,
                base_url=getattr(self.settings, 'openai_base_url', None),
            )

        if provider != 'ollama':
            raise RuntimeError(f'Unsupported BROWSER_USE_LLM_PROVIDER={provider!r}. Use \"ollama\" or \"openai\".')

        try:
            from browser_use.llm.ollama.chat import ChatOllama
        except Exception as exc:
            raise RuntimeError('Ollama browser-use LLM backend unavailable. Install browser-use ollama deps.') from exc
        return ChatOllama(
            model=getattr(self.settings, 'ollama_model_navigator', 'qwen2.5:7b'),
            host=getattr(self.settings, 'ollama_host', None),
        )

    async def start(self, session_ctx: SessionContext) -> None:
        await self.ensure_browser(session_ctx)

    async def ensure_browser(self, session_ctx: SessionContext) -> None:
        key = self._key(session_ctx)
        if key in self._sessions:
            return

        _, BrowserSession, BrowserProfile = await self._imports()
        self._branch_runtime(session_ctx)

        profile_path = self.profile_path_getter(session_ctx) if self.profile_path_getter else None
        profile = BrowserProfile(
            user_data_dir=profile_path,
            headless=self.headless,
            keep_alive=True,
            allowed_domains=getattr(self.settings, 'browser_allowed_domains', None) or None,
            prohibited_domains=getattr(self.settings, 'browser_prohibited_domains', None) or None,
        )
        browser_session = BrowserSession(browser_profile=profile)
        await browser_session.start()

        self._sessions[key] = browser_session
        self._agents.pop(key, None)

    async def run_navigation_step(
        self,
        *,
        session_ctx: SessionContext,
        subtask: Subtask,
        step_index: int,
        before_action: BeforeActionHook | None = None,
    ) -> NavigationStepResult:
        await self.ensure_browser(session_ctx)
        key = self._key(session_ctx)
        runtime = self._branch_runtime(session_ctx)

        runtime.before_action_hook = before_action
        runtime.block_current_step = False
        runtime.pending_action = None
        runtime.blocked_subtask_goal = None

        if runtime.paused:
            return NavigationStepResult(
                planned_action=PlannedAction(action_type=ActionType.NOTE, summary='Browser adapter paused'),
                executed=False,
                observation_summary='Browser adapter paused',
            )

        Agent, _, _ = await self._imports()
        llm = self._make_llm()
        browser_session = self._sessions[key]

        async def on_new_step(browser_state_summary: Any, agent_output: Any, step_number: int) -> None:
            runtime.block_current_step = False
            planned = self._extract_planned_action(browser_state_summary, agent_output)
            runtime.pending_action = planned
            if runtime.before_action_hook is None:
                return
            allow = await runtime.before_action_hook(planned)
            if not allow:
                runtime.block_current_step = True
                runtime.blocked_subtask_goal = subtask.goal
                runtime.paused = True
                active_agent = self._agents.get(key)
                if active_agent is not None and hasattr(active_agent, 'pause'):
                    active_agent.pause()

        async def should_stop() -> bool:
            return runtime.paused or runtime.block_current_step

        step_agent = Agent(
            task=subtask.goal,
            llm=llm,
            browser_session=browser_session,
            register_new_step_callback=on_new_step,
            register_should_stop_callback=should_stop,
        )
        self._agents[key] = step_agent
        try:
            if hasattr(step_agent, 'run'):
                await step_agent.run(max_steps=1)
            elif hasattr(step_agent, 'take_step'):
                await step_agent.take_step()
        finally:
            runtime.before_action_hook = None
            # keep latest agent reference for pause/resume only if still needed; otherwise close and remove
            agent_to_close = self._agents.pop(key, None)
            if agent_to_close is not None and hasattr(agent_to_close, 'close'):
                close_result = agent_to_close.close()
                if inspect.isawaitable(close_result):
                    await close_result

        if runtime.block_current_step:
            action = runtime.pending_action or PlannedAction(
                action_type=ActionType.NOTE,
                summary=f'Blocked browser-use action for {subtask.goal}',
            )
            live = await self.get_state(session_ctx)
            return NavigationStepResult(
                planned_action=action,
                executed=False,
                observation_summary='Paused before browser-use action execution',
                page_url=live.url,
                completed_subtask=False,
                live_state=live,
            )

        live = await self.get_state(session_ctx)
        action = runtime.pending_action or PlannedAction(
            action_type=ActionType.NOTE,
            summary=f'browser-use step for: {subtask.goal}',
            payload={'subtask_id': subtask.id},
            page_url=live.url,
        )
        return NavigationStepResult(
            planned_action=action,
            executed=True,
            observation_summary=live.latest_observation or 'browser-use step executed',
            page_url=live.url,
            completed_subtask=True,
            live_state=live,
        )

    async def execute_action(self, *, session_ctx: SessionContext, action: PlannedAction, step_index: int) -> NavigationStepResult:
        # Resume via a bounded step for the blocked subtask rather than exact low-level replay.
        runtime = self._branch_runtime(session_ctx)
        goal = runtime.blocked_subtask_goal
        if goal:
            runtime.paused = False
            result = await self.run_navigation_step(
                session_ctx=session_ctx,
                subtask=Subtask(id="approved-resume", title="Approved resume", goal=goal, status="pending"),
                step_index=step_index,
                before_action=None,
            )
            runtime.blocked_subtask_goal = None
            return result
        await asyncio.sleep(0)
        live = await self.get_state(session_ctx)
        return NavigationStepResult(
            planned_action=action,
            executed=True,
            observation_summary='Approved action resumed via browser-use continuation',
            page_url=live.url,
            completed_subtask=True,
            live_state=live,
        )

    async def pause(self, session_ctx: SessionContext) -> None:
        key = self._key(session_ctx)
        runtime = self._branch_runtime(session_ctx)
        runtime.paused = True
        agent = self._agents.get(key)
        if agent is not None and hasattr(agent, 'pause'):
            agent.pause()

    async def resume(self, session_ctx: SessionContext) -> None:
        key = self._key(session_ctx)
        runtime = self._branch_runtime(session_ctx)
        runtime.paused = False
        runtime.block_current_step = False
        agent = self._agents.get(key)
        if agent is not None and hasattr(agent, 'resume'):
            agent.resume()

    async def get_state(self, session_ctx: SessionContext) -> BrowserLiveState:
        key = self._key(session_ctx)
        browser_session = self._sessions.get(key)
        if browser_session is None:
            return BrowserLiveState(url=None, title=None, tabs=[])
        try:
            summary = await browser_session.get_browser_state_summary(include_screenshot=False)
            tabs = [
                TabState(
                    url=getattr(t, 'url', ''),
                    title=getattr(t, 'title', ''),
                    tab_id=str(getattr(t, 'target_id', '')),
                )
                for t in (getattr(summary, 'tabs', []) or [])
            ]
            return BrowserLiveState(
                url=getattr(summary, 'url', None),
                title=getattr(summary, 'title', None),
                tabs=tabs,
                latest_observation=f"{getattr(summary, 'title', '')} @ {getattr(summary, 'url', '')}",
            )
        except Exception:
            return BrowserLiveState(url=None, title=None, tabs=[], latest_observation='Unable to read browser state')

    async def capture_snapshot(self, session_ctx: SessionContext) -> SnapshotResult:
        return SnapshotResult(live_state=await self.get_state(session_ctx), metadata={'adapter': 'browser_use'})

    async def close(self, session_ctx: SessionContext) -> None:
        key = self._key(session_ctx)
        agent = self._agents.pop(key, None)
        browser = self._sessions.pop(key, None)
        self._runtime.pop(key, None)
        if agent is not None and hasattr(agent, 'close'):
            close_result = agent.close()
            if inspect.isawaitable(close_result):
                await close_result
        elif browser is not None and hasattr(browser, 'stop'):
            stop_result = browser.stop()
            if inspect.isawaitable(stop_result):
                await stop_result

    def _extract_planned_action(self, browser_state_summary: Any, agent_output: Any) -> PlannedAction:
        page_url = getattr(browser_state_summary, 'url', None)
        actions = getattr(agent_output, 'action', None) or []
        for action_model in actions:
            try:
                dumped = action_model.model_dump(exclude_none=True)
            except Exception:
                dumped = {}
            if not isinstance(dumped, dict):
                continue
            non_empty = [(k, v) for k, v in dumped.items() if v not in ({}, None, [], '')]
            if not non_empty:
                continue
            action_name, payload = non_empty[0]
            return PlannedAction(
                action_type=self._map_action_name(action_name),
                summary=f'{action_name}: {payload}',
                payload={'browser_use_action': action_name, 'params': payload},
                page_url=page_url,
            )
        return PlannedAction(
            action_type=ActionType.NOTE,
            summary='browser-use planned step (action details unavailable)',
            payload={},
            page_url=page_url,
        )

    def _map_action_name(self, name: str) -> ActionType:
        key = name.lower()
        if any(token in key for token in ('open', 'url', 'navigate', 'goto', 'go_to')):
            return ActionType.OPEN
        if 'click' in key:
            return ActionType.CLICK
        if any(token in key for token in ('type', 'input', 'fill')):
            return ActionType.TYPE
        if any(token in key for token in ('extract', 'read', 'scrape')):
            return ActionType.EXTRACT
        return ActionType.NOTE
