from __future__ import annotations

import asyncio
from threading import Event
from uuid import uuid4

import pytest

from tests.browser_harness import fixture_website, live_settings, workspace_app
from tests.conftest import create_session
from tests.test_browser_workspace import action, browser_state, client_headers, finish, run_task, take_control, target, wait_state


async def test_lost_manual_request_cannot_release_control_during_chromium_navigation(tmp_path):
    arrived, reply = Event(), Event()
    with fixture_website(slow_navigation=(arrived, reply)) as (site, visits):
        async with workspace_app(live_settings(tmp_path, site)) as (_, client):
            session = f"/sessions/{(await create_session(client))['session_id']}"
            headers = client_headers()
            lease = await take_control(client, session, headers)
            state = await browser_state(client, session, headers)
            payload = {'lease_id': lease, 'command_id': str(uuid4()), 'expected_revision': state['revision'],
                       'command': {'kind': 'open', 'url': site + '/slow'}}
            pending = asyncio.create_task(client.post(f'{session}/browser/command', headers=headers, json=payload))
            release = None
            try:
                assert await asyncio.to_thread(arrived.wait, 5)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                release = asyncio.create_task(client.post(f'{session}/release', headers=headers,
                                                          json={'lease_id': lease, 'resume': False}))
                await asyncio.sleep(0.25)
                assert not release.done(), 'Ownership changed while the admitted command was still in flight'
            finally:
                reply.set()
                if release is not None:
                    (await release).raise_for_status()
            assert visits['GET /slow'] == 1
            events = (await client.get(f'{session}/events')).raise_for_status().json()
            assert [event['action_type'] for event in events][-2:] == ['manual_input', 'release']


async def test_navigation_with_unknown_effect_is_not_repeated_automatically(tmp_path):
    arrived, reply = Event(), Event()
    with fixture_website(slow_navigation=(arrived, reply)) as (site, visits):
        class Planner:
            async def decide(self, *, goal, observation, history):
                return action('open', url=site + '/slow')

        settings = live_settings(tmp_path, site)
        settings.browser_timeout_ms = 500
        async with workspace_app(settings, planner=Planner()) as (app, client):
            session = f"/sessions/{(await create_session(client))['session_id']}"
            try:
                await run_task(client, session, f'Open {site}/slow')
                assert await asyncio.to_thread(arrived.wait, 6)
                await app.state.execution_manager.wait_for_idle(timeout=12)
                status = (await client.get(session)).raise_for_status().json()['state']
                assert visits['GET /slow'] == 1, 'The original request was accepted before the browser timed out'
                assert status['run_status'] == 'interrupted'
                assert status['last_error_code'] == 'browser_outcome_unknown'
                reply.set()
                (await client.post(f'{session}/resume')).raise_for_status()
                await wait_state(client, session, 'awaiting_approval')
                assert visits['GET /slow'] == 1, 'Resume must not silently repeat the unconfirmed navigation'
            finally:
                reply.set()


async def test_explicit_pause_survives_approval_until_resume(tmp_path):
    with fixture_website() as (site, visits):
        url = site + '/approval'

        class Planner:
            async def decide(self, *, goal, observation, history):
                if visits['POST /commit-a']:
                    return finish('Fixture submission observed')
                if observation.url != url:
                    return action('open', url=url)
                return action('click', target_id=target(observation, 'Submit first record'))

        async with workspace_app(live_settings(tmp_path, site), planner=Planner()) as (app, client):
            session = f"/sessions/{(await create_session(client))['session_id']}"
            await run_task(client, session, f'Submit the first record at {url}')
            state = await wait_state(client, session, 'awaiting_approval')
            (await client.post(f'{session}/pause')).raise_for_status()
            (await client.post(f'{session}/approve', json={'correlation_id': state['approval_correlation_id']})).raise_for_status()
            await app.state.execution_manager.wait_for_idle()
            assert visits['POST /commit-a'] == 0, 'Approval silently lifted the explicit pause'
            await wait_state(client, session, 'paused')
            (await client.post(f'{session}/resume')).raise_for_status()
            await wait_state(client, session, 'completed')
            assert visits['POST /commit-a'] == 1


async def test_duplicate_resume_does_not_cancel_a_current_decision(tmp_path):
    deciding, finish_decision = asyncio.Event(), asyncio.Event()
    attempts = []

    class Planner:
        async def decide(self, *, goal, observation, history):
            attempts.append(goal)
            deciding.set()
            await finish_decision.wait()
            return finish('One decision completed')

    with fixture_website() as (site, _):
        async with workspace_app(live_settings(tmp_path, site), planner=Planner()) as (_, client):
            session = f"/sessions/{(await create_session(client))['session_id']}"
            await run_task(client, session, 'Wait for the controlled fixture decision')
            await asyncio.wait_for(deciding.wait(), timeout=10)
            replies = await asyncio.gather(client.post(f'{session}/resume'), client.post(f'{session}/resume'))
            assert all(reply.is_success for reply in replies)
            finish_decision.set()
            await wait_state(client, session, 'completed')
            assert len(attempts) == 1


async def test_expired_lease_cannot_use_current_revision_or_block_new_controller(tmp_path):
    with fixture_website() as (site, visits):
        settings = live_settings(tmp_path, site)
        settings.workspace_lease_timeout_s = 1
        async with workspace_app(settings) as (_, client):
            session = f"/sessions/{(await create_session(client))['session_id']}"
            old_client, new_client = client_headers(), client_headers()
            old_lease = await take_control(client, session, old_client)
            await asyncio.sleep(1.1)
            new_lease = await take_control(client, session, new_client)
            state = await browser_state(client, session, new_client)
            assert new_lease != old_lease and state['lease_id'] == new_lease
            denied = await client.post(f'{session}/browser/command', headers=old_client, json={
                'lease_id': old_lease, 'command_id': str(uuid4()), 'expected_revision': state['revision'],
                'command': {'kind': 'open', 'url': site + '/details'},
            })
            assert denied.status_code == 409
            assert visits['GET /details'] == 0


async def test_model_deadline_can_resume_without_dispatching_old_output(tmp_path):
    attempts = []

    class Planner:
        async def decide(self, *, goal, observation, history):
            attempts.append(goal)
            if len(attempts) == 1:
                await asyncio.Event().wait()
            return finish('Fresh decision after timeout')

    with fixture_website() as (site, visits):
        settings = live_settings(tmp_path, site)
        settings.browser_agent_timeout_s = 0.2
        async with workspace_app(settings, planner=Planner()) as (_, client):
            session = f"/sessions/{(await create_session(client))['session_id']}"
            await run_task(client, session, 'Recover from a timed-out model decision')
            await wait_state(client, session, 'failed')
            assert sum(visits.values()) == 0
            (await client.post(f'{session}/resume')).raise_for_status()
            await wait_state(client, session, 'completed')
            assert len(attempts) == 2
