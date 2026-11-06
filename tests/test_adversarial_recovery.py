from __future__ import annotations

import asyncio
from datetime import datetime
import sqlite3
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.execution_manager import ExecutionManager
from app.db.models import EventORM, MessageORM
from app.models.domain import SessionContext
from app.models.task_groups import CreateTaskGroupRequest
from tests.browser_harness import fixture_website, live_settings, workspace_app
from tests.conftest import create_session
from tests.test_browser_workspace import action, browser_state, client_headers, finish, manual, run_task, take_control, target, wait_state


async def test_nested_fork_keeps_each_inherited_checkpoint_history_boundary(tmp_path):
    with fixture_website() as (site, _):
        async with workspace_app(live_settings(tmp_path, site)) as (_, client):
            created = await create_session(client)
            session = f"/sessions/{created['session_id']}"
            original = created['selected_branch_id']

            for content in ('Before first checkpoint', 'Only after first checkpoint'):
                (await client.post(f'{session}/messages', json={
                    'content': content, 'trigger_agents': False,
                })).raise_for_status()
                (await client.post(f'{session}/snapshot', json={'note': content})).raise_for_status()

            originals = (await client.get(f'{session}/checkpoints')).raise_for_status().json()
            early, late = originals
            assert (early['message_cursor'], late['message_cursor']) == (1, 2)
            child = (await client.post(f'{session}/branch', json={
                'source_branch_id': original, 'checkpoint_id': late['checkpoint_id'],
            })).raise_for_status().json()['branch_id']
            inherited = (await client.get(f'{session}/checkpoints', params={'branch_id': child})).raise_for_status().json()
            assert [(row['message_cursor'], row['event_cursor']) for row in inherited] == [
                (row['message_cursor'], row['event_cursor']) for row in originals
            ]
            grandchild = (await client.post(f'{session}/branch', json={
                'source_branch_id': child, 'checkpoint_id': inherited[0]['checkpoint_id'],
            })).raise_for_status().json()['branch_id']
            messages = (await client.get(f'{session}/messages', params={'branch_id': grandchild})).raise_for_status().json()
            assert [row['content'] for row in messages] == ['Before first checkpoint']


@pytest.mark.parametrize('tied_timestamps', [False, True])
async def test_fork_replay_preserves_source_timestamps_and_cross_kind_order(tmp_path, tied_timestamps):
    with fixture_website() as (site, _):
        async with workspace_app(live_settings(tmp_path, site)) as (app, client):
            created = await create_session(client)
            session = f"/sessions/{created['session_id']}"
            for content in ('First user instruction', 'Second user instruction'):
                (await client.post(f'{session}/messages', json={
                    'content': content, 'trigger_agents': False,
                })).raise_for_status()
                (await client.post(f'{session}/snapshot', json={})).raise_for_status()

            checkpoints = (await client.get(f'{session}/checkpoints')).raise_for_status().json()
            cutoff = next(row['checkpoint_id'] for row in checkpoints if row['message_cursor'] == 2)
            original = (await client.get(f'{session}/replay')).raise_for_status().json()['timeline']

            def is_after_cutoff(row):
                return row['action_type'] == 'snapshot' and row['payload']['checkpoint_id'] == cutoff

            excluded = next(row['source_id'] for row in original if is_after_cutoff(row))
            if tied_timestamps:
                fixed_time = datetime(2020, 1, 1)
                async with app.state.session_factory() as db:
                    for kind, model in (('message', MessageORM), ('event', EventORM)):
                        ids = [row['source_id'] for row in original if row['kind'] == kind]
                        await db.execute(update(model).where(model.id.in_(ids)).values(created_at=fixed_time))
                    # A later row can sort first by UUID when the wall clock ties.
                    await db.execute(update(EventORM).where(EventORM.id == excluded)
                                     .values(id='00000000-0000-4000-8000-000000000000'))
                    await db.commit()
                original = (await client.get(f'{session}/replay')).raise_for_status().json()['timeline']
                appended = (await client.get(f'{session}/events')).raise_for_status().json()
                assert appended[-1]['event_id'] == '00000000-0000-4000-8000-000000000000'
                catchup = (await client.get(f'{session}/events', params={
                    'after_event_id': appended[-2]['event_id'],
                })).raise_for_status().json()
                assert catchup == appended[-1:]
                messages = (await client.get(f'{session}/messages')).raise_for_status().json()
                earlier = (await client.get(f'{session}/messages', params={
                    'before': messages[-1]['id'],
                })).raise_for_status().json()
                assert [row['content'] for row in earlier] == ['First user instruction']
            fork = (await client.post(f'{session}/branch', json={'checkpoint_id': cutoff})).raise_for_status().json()['branch_id']
            replay = (await client.get(f'{session}/replay', params={'branch_id': fork})).raise_for_status().json()['timeline']

            def history(timeline):
                return [(row['kind'], row['created_at'], row['content'], row['action_type']) for row in timeline]

            # The final snapshot event was written after the selected checkpoint.
            assert history(replay) == history(row for row in original if not is_after_cutoff(row))
            assert {row['source_id'] for row in replay}.isdisjoint(row['source_id'] for row in original)
            assert all(UUID(row['source_id']).version == 4 for row in replay)


async def test_manual_snapshot_fork_restores_page_after_unsolicited_navigation(tmp_path):
    with fixture_website() as (site, _):
        async with workspace_app(live_settings(tmp_path, site)) as (app, client):
            created = await create_session(client)
            session = f"/sessions/{created['session_id']}"
            owner = client_headers()
            lease = await take_control(client, session, owner)
            state = await browser_state(client, session, owner)
            (await manual(client, session, owner, lease, state['revision'], 'open', url=f'{site}/')).raise_for_status()

            ctx = SessionContext(session_id=created['session_id'], branch_id=created['selected_branch_id'])
            _, page = app.state.browser_adapter._active(ctx)
            await page.evaluate('(url) => location.assign(url)', f'{site}/details')
            await page.wait_for_url(f'{site}/details')
            prior = (await client.get(session)).raise_for_status().json()['state']
            assert prior['last_known_url'] == f'{site}/'

            (await client.post(f'{session}/snapshot', json={'note': 'The page redirected before this checkpoint'})).raise_for_status()
            checkpoints = (await client.get(f'{session}/checkpoints')).raise_for_status().json()
            fork = (await client.post(f'{session}/branch', json={
                'checkpoint_id': checkpoints[-1]['checkpoint_id'],
            })).raise_for_status().json()['branch_id']
            params = {'branch_id': fork}
            forked = (await client.get(session, params=params)).raise_for_status().json()['state']
            assert forked['last_known_url'] == f'{site}/details'
            (await client.post(f'{session}/takeover', params=params, headers=owner)).raise_for_status()
            restored = (await client.get(f'{session}/browser', params=params, headers=owner)).raise_for_status().json()
            assert restored['url'] == f'{site}/details'


async def test_fork_resumes_an_instruction_queued_before_its_checkpoint(tmp_path):
    class Planner:
        async def decide(self, *, goal, observation, history):
            assert goal == 'Summarize the queued task'
            return finish('The queued task was preserved.')

    with fixture_website() as (site, _):
        async with workspace_app(live_settings(tmp_path, site), planner=Planner()) as (_, client):
            created = await create_session(client)
            session = f"/sessions/{created['session_id']}"
            (await client.post(f'{session}/messages', json={
                'content': 'Summarize the queued task', 'trigger_agents': False,
            })).raise_for_status()
            (await client.post(f'{session}/snapshot', json={})).raise_for_status()
            branch = (await client.post(f'{session}/branch', json={})).raise_for_status().json()['branch_id']
            params = {'branch_id': branch}
            (await client.post(f'{session}/resume', params=params)).raise_for_status()
            await wait_state(client, f'{session}?branch_id={branch}', 'completed')
            history = (await client.get(f'{session}/messages', params=params)).raise_for_status().json()
            assert any(row['role'] == 'assistant' and row['content'] == 'The queued task was preserved.' for row in history)
            events = (await client.get(f'{session}/events', params=params)).raise_for_status().json()
            user_message = next(row for row in history if row['role'] == 'user')
            plan_event = next(row for row in events if row['action_type'] == 'plan')
            assert plan_event['action_payload']['source_message_id'] == user_message['id']


async def test_interrupted_model_run_clears_obsolete_owner_and_emits_recovery_event(tmp_path):
    deciding = asyncio.Event()

    class WaitingPlanner:
        async def decide(self, *, goal, observation, history):
            deciding.set()
            await asyncio.Event().wait()

    with fixture_website() as (site, _):
        settings = live_settings(tmp_path, site)
        async with workspace_app(settings, planner=WaitingPlanner()) as (_, client):
            created = await create_session(client)
            session = f"/sessions/{created['session_id']}"
            await run_task(client, session, 'Wait during model inference')
            await asyncio.wait_for(deciding.wait(), timeout=12)
            state = (await client.get(session)).raise_for_status().json()['state']
            assert (state['run_status'], state['control_holder']) == ('navigating', 'agent')

        async with workspace_app(settings) as (_, client):
            state = (await client.get(session)).raise_for_status().json()['state']
            assert (state['run_status'], state['control_holder'], state['pause_requested']) == ('interrupted', 'none', True)
            events = (await client.get(f'{session}/events')).raise_for_status().json()
            assert any(event['action_type'] == 'process_recovery' and
                       event['action_payload']['previous_status'] == 'navigating' for event in events)


def storage_error() -> OperationalError:
    return OperationalError('fault-injected audit write', None, sqlite3.OperationalError('database is locked'))


async def test_group_dispatch_does_not_depend_on_post_commit_snapshot_read(tmp_path, monkeypatch):
    class Planner:
        async def decide(self, *, goal, observation, history):
            return finish(f'Completed {goal}')

    with fixture_website() as (site, _):
        async with workspace_app(live_settings(tmp_path, site), planner=Planner()) as (app, client):
            task_groups = app.state.services.task_groups
            group_id = uuid4()
            payload = CreateTaskGroupRequest(request_id=group_id, tasks=[{'goal': 'One task'}, {'goal': 'Two tasks'}])

            async def fail_response_read(*args, **kwargs):
                raise storage_error()

            with monkeypatch.context() as patch:
                patch.setattr(task_groups, 'get', fail_response_read)
                with pytest.raises(OperationalError):
                    await task_groups.create(payload)

            group = (await client.get(f'/task-groups/{group_id}', params={
                'wait_for': 'settled', 'wait_seconds': 12,
            })).raise_for_status().json()
            assert group['status'] == 'settled'
            assert [item['answer'] for item in group['tasks']] == ['Completed One task', 'Completed Two tasks']


class SubmitPlanner:
    def __init__(self, site):
        self.destination = f'{site}/approval'

    async def decide(self, *, goal, observation, history):
        if observation.title == 'Record committed':
            return finish('The record was submitted.')
        if observation.url != self.destination:
            return action('open', url=self.destination)
        return action('click', target_id=target(observation, 'Submit first record'))


async def test_failed_rejection_write_does_not_destroy_pending_approval(tmp_path, monkeypatch):
    with fixture_website() as (site, visits):
        async with workspace_app(live_settings(tmp_path, site), planner=SubmitPlanner(site)) as (app, client):
            created = await create_session(client)
            session = f"/sessions/{created['session_id']}"
            await run_task(client, session, f'Submit the record at {site}/approval')
            state = await wait_state(client, session, 'awaiting_approval')
            correlation = state['approval_correlation_id']
            service = app.state.services.live
            ctx = await service.context(created['session_id'], None)
            add_event = service.repos.events.add_event

            async def fail_rejection(db, **fields):
                if fields['action_type'] == 'reject':
                    raise storage_error()
                return await add_event(db, **fields)

            with monkeypatch.context() as patch:
                patch.setattr(service.repos.events, 'add_event', fail_rejection)
                with pytest.raises(OperationalError):
                    await service.decide(ctx, correlation, 'reject', None)

            pending = (await client.get(session)).raise_for_status().json()['state']
            assert pending['approval_correlation_id'] == correlation
            assert pending['pending_approval']['command']['kind'] == 'click'
            (await client.post(f'{session}/reject', json={'correlation_id': correlation})).raise_for_status()
            assert visits['POST /commit-a'] == 0


async def test_post_effect_storage_failure_is_reported_and_does_not_replay_after_restart(tmp_path, monkeypatch):
    with fixture_website() as (site, visits):
        settings = live_settings(tmp_path, site)
        async with workspace_app(settings, planner=SubmitPlanner(site)) as (app, client):
            created = await create_session(client)
            session = f"/sessions/{created['session_id']}"
            await run_task(client, session, f'Submit the record at {site}/approval')
            state = await wait_state(client, session, 'awaiting_approval')
            correlation = state['approval_correlation_id']
            repository = app.state.services.live.repos.events
            add_event = repository.add_event

            async def fail_after_effect(db, **fields):
                if fields['action_type'] == 'click' and fields['correlation_id'] == correlation:
                    assert visits['POST /commit-a'] == 1
                    raise storage_error()
                return await add_event(db, **fields)

            with monkeypatch.context() as patch:
                patch.setattr(repository, 'add_event', fail_after_effect)
                (await client.post(f'{session}/approve', json={'correlation_id': correlation})).raise_for_status()
                await app.state.execution_manager.wait_for_idle(timeout=15)
                interrupted = (await client.get(session)).raise_for_status().json()['state']
                assert interrupted['run_status'] == 'interrupted'
                assert interrupted['last_error_code'] == 'browser_outcome_unknown'
                assert visits['POST /commit-a'] == 1

            (await client.post(f'{session}/snapshot', json={})).raise_for_status()
            fork = (await client.post(f'{session}/branch', json={})).raise_for_status().json()['branch_id']
            params = {'branch_id': fork}
            forked = (await client.get(session, params=params)).raise_for_status().json()['state']
            assert forked['last_error_code'] == 'browser_outcome_unknown'
            owner = client_headers()
            (await client.post(f'{session}/takeover', params=params, headers=owner)).raise_for_status()
            browser = (await client.get(f'{session}/browser', params=params, headers=owner)).raise_for_status().json()
            assert browser['url'] == 'about:blank'
            assert visits['GET /commit-a'] == 0

        async with workspace_app(settings) as (_, client):
            state = await wait_state(client, session, 'interrupted')
            assert state['last_error_code'] == 'browser_outcome_unknown'
            assert state['pause_requested']
            stale = await client.post(f'{session}/approve', json={'correlation_id': correlation})
            assert stale.status_code == 409
            assert visits['POST /commit-a'] == 1


async def test_shutdown_rejects_new_workers_while_existing_worker_drains():
    running = asyncio.Event()
    cleanup = asyncio.Event()
    finish_cleanup = asyncio.Event()
    started = []

    async def worker(session_id, branch_id):
        started.append(branch_id)
        if branch_id == 'existing':
            running.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.set()
                await finish_cleanup.wait()

    manager = ExecutionManager(worker)
    assert await manager.schedule('session', 'existing')
    await asyncio.wait_for(running.wait(), timeout=2)
    shutdown = asyncio.create_task(manager.shutdown())
    try:
        await asyncio.wait_for(cleanup.wait(), timeout=2)
        assert not await manager.schedule('session', 'late')
        await manager.schedule_many([('session', 'late batch')])
        assert started == ['existing']
        assert not shutdown.done()
    finally:
        finish_cleanup.set()
        await asyncio.wait_for(shutdown, timeout=2)


async def test_cancelled_release_cannot_leave_a_live_lease_after_commit(tmp_path, monkeypatch):
    committed = asyncio.Event()
    commit = AsyncSession.commit

    async def block_after_commit(db):
        await commit(db)
        if asyncio.current_task().get_name() == 'release-at-commit-boundary':
            committed.set()
            await asyncio.Event().wait()

    with fixture_website() as (site, visits):
        async with workspace_app(live_settings(tmp_path, site)) as (app, client):
            created = await create_session(client)
            session = f"/sessions/{created['session_id']}"
            headers = client_headers()
            lease = await take_control(client, session, headers)
            state = await browser_state(client, session, headers)
            service = app.state.services.live
            ctx = await service.context(created['session_id'], None)
            with monkeypatch.context() as patch:
                patch.setattr(AsyncSession, 'commit', block_after_commit)
                release = asyncio.create_task(service.release(
                    ctx, headers['X-Workspace-Client'], lease, note=None, resume=False,
                ), name='release-at-commit-boundary')
                try:
                    await asyncio.wait_for(committed.wait(), timeout=10)
                finally:
                    release.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await release

            durable = (await client.get(session)).raise_for_status().json()['state']
            assert durable['control_mode'] == 'agent'
            stale = await manual(client, session, headers, lease, state['revision'], 'open', url=f'{site}/details')
            assert stale.status_code == 409
            assert visits['GET /details'] == 0
            new_lease = await take_control(client, session, headers)
            assert new_lease != lease
