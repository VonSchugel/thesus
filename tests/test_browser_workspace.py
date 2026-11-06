from __future__ import annotations

import asyncio
import json
from threading import Event
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from app.agents.browser_planner import AgentDecision
from tests.browser_harness import demo_website, fixture_website, live_settings, workspace_app
from tests.conftest import create_session


def action(kind: str, **params):
    return AgentDecision.model_validate({'kind': 'command', 'summary': f'Fixture: {kind}', 'command': {'kind': kind, **params}})


def finish(summary: str):
    return AgentDecision(kind='finish', summary=summary)


def target(observation, label: str):
    return next(element.target_id for element in observation.elements if element.label == label)


async def run_task(client, session: str, goal: str):
    (await client.post(f'{session}/messages', json={'content': goal})).raise_for_status()


async def wait_state(client, session: str, status: str):
    async with asyncio.timeout(18):
        while True:
            current = (await client.get(session)).raise_for_status().json()['state']
            if current['run_status'] == status:
                return current
            if current['run_status'] == 'failed' and status != 'failed':
                pytest.fail(f'Browser task failed: {current}')
            await asyncio.sleep(0.05)


def client_headers():
    return {'X-Workspace-Client': str(uuid4())}


async def browser_state(client, session: str, headers: dict):
    return (await client.get(f'{session}/browser', headers=headers)).raise_for_status().json()


async def take_control(client, session: str, headers: dict):
    return (await client.post(f'{session}/takeover', headers=headers)).raise_for_status().json()['lease_id']


async def manual(client, session: str, headers: dict, lease: str, revision: str, kind: str, **params):
    return await client.post(
        f'{session}/browser/command', headers=headers,
        json={'lease_id': lease, 'expected_revision': revision, 'command_id': str(uuid4()), 'command': {'kind': kind, **params}},
    )


async def test_paused_worker_reobserves_manual_changes_in_same_chromium_page(tmp_path):
    planning = asyncio.Event()
    permit_obsolete_decision = asyncio.Event()
    observed_after_release = []

    with fixture_website() as (site, visits):
        class Planner:
            async def decide(self, *, goal, observation, history):
                if observation.url != site + '/':
                    return action('open', url=site + '/')
                if 'HUMAN-REVIEWED' in observation.ax_tree:
                    observed_after_release.append(observation.ax_tree)
                    return finish('Saw the human review and verified it on the worksheet.')
                planning.set()
                await permit_obsolete_decision.wait()
                return action('open', url=site + '/obsolete')

        async with workspace_app(live_settings(tmp_path, site), planner=Planner()) as (_, client):
            headers, observer = client_headers(), client_headers()
            session = f"/sessions/{(await create_session(client))['session_id']}"
            await run_task(client, session, f'Review the worksheet at {site}/ with the human')
            await asyncio.wait_for(planning.wait(), timeout=18)
            try:
                (await client.post(f'{session}/pause', headers=headers)).raise_for_status()
                await wait_state(client, session, 'paused')
                lease = await take_control(client, session, headers)
                assert (await client.post(f'{session}/takeover', headers=observer)).status_code == 409

                frame = (await client.get(f'{session}/browser/frame', headers=headers)).raise_for_status()
                assert frame.headers['content-type'].startswith('image/jpeg')
                assert frame.content.startswith(b'\xff\xd8')
                revision = frame.headers['x-browser-revision']
                command = {
                    'lease_id': lease, 'expected_revision': revision, 'command_id': str(uuid4()),
                    'command': {'kind': 'click', 'x': 70, 'y': 100},
                }
                assert (await client.post(f'{session}/browser/command', json=command, headers=observer)).status_code == 409
                (await client.post(f'{session}/browser/command', json=command, headers=headers)).raise_for_status()
                (await client.post(f'{session}/browser/command', json=command, headers=headers)).raise_for_status()

                state = await browser_state(client, session, headers)
                assert state['revision'] != revision
                stale = await manual(client, session, headers, lease, revision, 'type', text='STALE INPUT')
                assert stale.status_code == 409
                (await manual(client, session, headers, lease, state['revision'], 'type', text='HUMAN-REVIEWED')).raise_for_status()
                release = await client.post(f'{session}/release', headers=headers, json={'lease_id': lease, 'resume': True})
                release.raise_for_status()
            finally:
                permit_obsolete_decision.set()

            await wait_state(client, session, 'completed')
            assert len(observed_after_release) == 1
            assert 'Review count 1' in observed_after_release[0]
            assert 'STALE INPUT' not in observed_after_release[0]
            assert visits['GET /obsolete'] == 0
            assert visits['GET /'] == 1
            state = await browser_state(client, session, headers)
            assert (await manual(client, session, headers, lease, state['revision'], 'click', x=70, y=100)).status_code == 409
            events = (await client.get(f'{session}/events')).raise_for_status().json()
            assert {'takeover', 'release', 'pause'}.issubset({event['action_type'] for event in events})


async def test_approval_is_bound_to_one_real_form_and_can_be_rejected(tmp_path):
    with fixture_website() as (site, visits):
        class Planner:
            async def decide(self, *, goal, observation, history):
                if observation.title == 'Record committed':
                    return finish('Verified the server accepted the record.')
                if observation.url != site + '/approval':
                    return action('open', url=site + '/approval')
                label = 'Submit second record' if 'second' in goal else 'Submit first record'
                return action('click', target_id=target(observation, label))

        async with workspace_app(live_settings(tmp_path, site), planner=Planner()) as (_, client):
            headers = client_headers()
            session = f"/sessions/{(await create_session(client))['session_id']}"
            await run_task(client, session, f'Submit the first record at {site}/approval')
            first = (await wait_state(client, session, 'awaiting_approval'))['approval_correlation_id']
            assert first
            assert visits['POST /commit-a'] == 0
            (await client.post(f'{session}/reject', headers=headers, json={'correlation_id': first, 'reason': 'Use the second record'})).raise_for_status()
            await run_task(client, session, f'Submit the second record at {site}/approval')
            state = (await client.get(session)).raise_for_status().json()['state']
            if state['run_status'] == 'paused':
                (await client.post(f'{session}/resume', headers=headers)).raise_for_status()
            second = (await wait_state(client, session, 'awaiting_approval'))['approval_correlation_id']
            assert second and second != first
            stale = await client.post(f'{session}/approve', headers=headers, json={'correlation_id': first})
            assert stale.status_code in {400, 409, 422}
            assert visits['POST /commit-a'] == visits['POST /commit-b'] == 0
            (await client.post(f'{session}/approve', headers=headers, json={'correlation_id': second})).raise_for_status()
            await wait_state(client, session, 'completed')
            assert visits['POST /commit-a'] == 0
            assert visits['POST /commit-b'] == 1


async def test_edited_approval_replaces_the_old_decision_and_keeps_text_out_of_audit(tmp_path):
    original_text = 'ORIGINAL-PRIVATE-FIXTURE'
    revised_text = 'REVISED-PRIVATE-FIXTURE'
    with fixture_website() as (site, _):
        class Planner:
            async def decide(self, *, goal, observation, history):
                if observation.url != site + '/':
                    return action('open', url=site + '/')
                if revised_text in observation.ax_tree:
                    return finish('Verified the revised entry.')
                assert original_text not in observation.ax_tree
                return action('type', target_id=target(observation, 'Review note'), text=original_text)

        async with workspace_app(live_settings(tmp_path, site), planner=Planner()) as (_, client):
            session = f"/sessions/{(await create_session(client))['session_id']}"
            await run_task(client, session, f'Enter a note at {site}/')
            initial = await wait_state(client, session, 'awaiting_approval')
            correlation = initial['approval_correlation_id']
            command = initial['pending_approval']['command']
            assert command['text'] == original_text
            edited = {'correlation_id': correlation, 'command': {**command, 'text': revised_text}}
            wrong_target = {**edited, 'command': {**edited['command'], 'target_id': 'different-target'}}
            assert (await client.post(f'{session}/edit-action', json=wrong_target)).is_client_error
            (await client.post(f'{session}/edit-action', json=edited)).raise_for_status()
            pending = await wait_state(client, session, 'awaiting_approval')
            assert pending['pending_approval']['command']['text'] == revised_text
            replacement = pending['approval_correlation_id']
            assert replacement != correlation
            assert (await client.post(f'{session}/approve', json={'correlation_id': correlation})).status_code == 409
            (await client.post(f'{session}/approve', json={'correlation_id': replacement})).raise_for_status()
            await wait_state(client, session, 'completed')
            events = (await client.get(f'{session}/events')).raise_for_status().text
            assert 'action_edited' in events
            assert original_text not in events and revised_text not in events


async def test_workspace_authentication_and_cross_origin_guards(tmp_path):
    with fixture_website() as (site, _):
        settings = live_settings(tmp_path, site)
        settings.workspace_access_token = 'verification-token'
        async with workspace_app(settings) as (_, client):
            unauthorized = await client.get('/sessions')
            assert unauthorized.status_code == 401
            assert unauthorized.headers['www-authenticate'].startswith('Basic ')
            assert (await client.get('/sessions', auth=('workspace', 'wrong-token'))).status_code == 401
            assert (await client.get('/sessions', auth=('other-user', 'verification-token'))).status_code == 401
            (await client.get('/sessions', auth=('workspace', 'verification-token'))).raise_for_status()

            bearer = {'Authorization': 'Bearer verification-token'}
            (await client.get('/workspace/capabilities', headers=bearer)).raise_for_status()
            assert (await client.get('/sessions', headers={**bearer, 'Host': 'host.invalid'})).status_code == 400
            assert (await client.get('/sessions', headers={**bearer, 'Sec-Fetch-Site': 'cross-site'})).status_code == 403
            rejected = await client.post('/sessions', headers={**bearer, 'Origin': 'http://host.invalid'}, json={'title': 'Denied'})
            assert rejected.status_code == 403
            accepted = await client.post('/sessions', headers={**bearer, 'Origin': 'http://127.0.0.1'}, json={'title': 'Allowed'})
            accepted.raise_for_status()
            sessions = (await client.get('/sessions', headers=bearer)).raise_for_status().json()
            assert [session['title'] for session in sessions] == ['Allowed']


async def test_cancel_replan_and_stop_have_distinct_execution_effects(tmp_path):
    with fixture_website() as (site, visits):
        class Planner:
            async def decide(self, *, goal, observation, history):
                if 'read' in goal:
                    if observation.url == site + '/details':
                        return finish('Read the invoice total.')
                    return action('open', url=site + '/details')
                if observation.url != site + '/approval':
                    return action('open', url=site + '/approval')
                return action('click', target_id=target(observation, 'Submit first record'))

        async with workspace_app(live_settings(tmp_path, site), planner=Planner()) as (_, client):
            session = f"/sessions/{(await create_session(client))['session_id']}"
            await run_task(client, session, f'Submit the first record at {site}/approval')
            first = (await wait_state(client, session, 'awaiting_approval'))['approval_correlation_id']
            (await client.post(f'{session}/cancel-action', json={'correlation_id': first})).raise_for_status()
            assert not (await wait_state(client, session, 'paused'))['approval_required']
            (await client.post(f'{session}/replan')).raise_for_status()
            second = (await wait_state(client, session, 'awaiting_approval'))['approval_correlation_id']
            assert second != first
            (await client.post(f'{session}/stop')).raise_for_status()
            await wait_state(client, session, 'cancelled')
            assert (await client.post(f'{session}/approve', json={'correlation_id': second})).status_code == 409
            assert (await client.post(f'{session}/resume')).status_code == 409
            assert (await client.post(f'{session}/replan')).status_code == 409
            await run_task(client, session, f'Please read {site}/details')
            await wait_state(client, session, 'completed')
            assert visits['POST /commit-a'] == 0
            assert visits['GET /details'] == 1


async def test_approval_rechecks_a_changed_form_destination_before_dispatch(tmp_path):
    change_form = Event()
    with fixture_website(change_form=change_form) as (site, visits):
        class Planner:
            async def decide(self, *, goal, observation, history):
                if observation.url != site + '/changing-form':
                    return action('open', url=site + '/changing-form')
                if observation.title == 'Form changed' and any('did not complete' in item for item in history):
                    return finish('Observed the changed form after resuming.')
                return action('click', target_id=target(observation, 'Submit record'))

        async with workspace_app(live_settings(tmp_path, site), planner=Planner()) as (_, client):
            session = f"/sessions/{(await create_session(client))['session_id']}"
            await run_task(client, session, f'Submit a record at {site}/changing-form')
            try:
                pending = await wait_state(client, session, 'awaiting_approval')
            finally:
                change_form.set()
            headers = client_headers()
            async with asyncio.timeout(8):
                while (await browser_state(client, session, headers))['title'] != 'Form changed':
                    await asyncio.sleep(0.05)
            correlation = pending['approval_correlation_id']
            (await client.post(f'{session}/approve', json={'correlation_id': correlation})).raise_for_status()
            await wait_state(client, session, 'interrupted')
            assert visits['POST /commit-a'] == visits['POST /commit-b'] == 0
            events = (await client.get(f'{session}/events')).raise_for_status().json()
            assert any(event['action_payload'].get('error_type') == 'StaleBrowserState' for event in events)
            (await client.post(f'{session}/resume')).raise_for_status()
            await wait_state(client, session, 'completed')


async def test_takeover_drains_real_in_flight_navigation(tmp_path):
    navigation_started, finish_navigation = Event(), Event()
    with fixture_website(slow_navigation=(navigation_started, finish_navigation)) as (site, visits):
        class Planner:
            async def decide(self, *, goal, observation, history):
                if observation.url == site + '/slow':
                    return finish('Slow navigation completed.')
                return action('open', url=site + '/slow')

        async with workspace_app(live_settings(tmp_path, site), planner=Planner()) as (_, client):
            headers = client_headers()
            session = f"/sessions/{(await create_session(client))['session_id']}"
            await run_task(client, session, f'Open {site}/slow')
            assert await asyncio.to_thread(navigation_started.wait, 12)
            takeover = asyncio.create_task(take_control(client, session, headers))
            try:
                done, _ = await asyncio.wait({takeover}, timeout=0.12)
                assert not done, 'Exclusive browser ownership arrived while Chromium was still navigating'
            finally:
                finish_navigation.set()
            lease = await asyncio.wait_for(takeover, timeout=10)
            state = await browser_state(client, session, headers)
            assert state['url'] == site + '/slow'
            (await manual(client, session, headers, lease, state['revision'], 'open', url=site + '/details')).raise_for_status()
            assert (await browser_state(client, session, headers))['url'] == site + '/details'
            assert visits['GET /slow'] == visits['GET /details'] == 1


async def test_tabs_artifacts_and_profile_are_scoped_to_the_browser_session(tmp_path):
    with fixture_website() as (site, _):
        settings = live_settings(tmp_path, site)
        headers = client_headers()
        async with workspace_app(settings) as (_, client):
            first = f"/sessions/{(await create_session(client))['session_id']}"
            second = f"/sessions/{(await create_session(client))['session_id']}"
            first_lease = await take_control(client, first, headers)
            state = await browser_state(client, first, headers)
            (await manual(client, first, headers, first_lease, state['revision'], 'open', url=site + '/')).raise_for_status()
            state = await browser_state(client, first, headers)
            original = state['active_tab_id']
            (await manual(client, first, headers, first_lease, state['revision'], 'new_tab', url=site + '/cookie')).raise_for_status()
            state = await browser_state(client, first, headers)
            assert len(state['tabs']) == 2 and state['active_tab_id'] != original
            assert state['title'] == 'Cookie present'
            (await manual(client, first, headers, first_lease, state['revision'], 'switch_tab', tab_id=original)).raise_for_status()
            state = await browser_state(client, first, headers)
            assert state['url'] == site + '/'
            (await manual(client, first, headers, first_lease, state['revision'], 'click', x=60, y=258)).raise_for_status()
            async with asyncio.timeout(10):
                while not (state := await browser_state(client, first, headers))['artifacts']:
                    await asyncio.sleep(0.05)
            artifact = next(item for item in state['artifacts'] if item['kind'] == 'download')
            assert artifact['name'] == 'notes.txt'
            route = f"browser/artifacts/{artifact['id']}"
            response = (await client.get(f'{first}/{route}')).raise_for_status()
            assert response.headers['content-disposition'].startswith('attachment;')
            assert response.content == b'worksheet notes\n'
            assert (await client.get(f'{second}/{route}')).status_code == 404

            uploaded = (await client.post(
                f'{first}/browser/upload', headers={**headers, 'X-Artifact-Name': 'upload-notes.txt'},
                content=b'test notes for upload',
            )).raise_for_status().json()
            assert uploaded['kind'] == 'upload'
            state = await browser_state(client, first, headers)
            (await manual(client, first, headers, first_lease, state['revision'], 'upload', artifact_id=uploaded['id'])).raise_for_status()
            assert (await browser_state(client, first, headers))['title'] == 'Attached: upload-notes.txt'

            second_lease = await take_control(client, second, headers)
            state = await browser_state(client, second, headers)
            (await manual(client, second, headers, second_lease, state['revision'], 'open', url=site + '/cookie')).raise_for_status()
            assert (await browser_state(client, second, headers))['title'] == 'Cookie absent'
            await client.post(f'{first}/release', headers=headers, json={'lease_id': first_lease, 'resume': False})

        async with workspace_app(settings) as (_, client):
            restored_lease = await take_control(client, first, headers)
            state = await browser_state(client, first, headers)
            (await manual(client, first, headers, restored_lease, state['revision'], 'open', url=site + '/cookie')).raise_for_status()
            assert (await browser_state(client, first, headers))['title'] == 'Cookie present'
            assert (await client.get(f'{first}/{route}')).raise_for_status().content == b'worksheet notes\n'


async def test_redirect_cannot_escape_private_browser_origin_policy(tmp_path):
    with fixture_website() as (private_site, private_visits), fixture_website(redirect_to=private_site + '/details') as (site, visits):
        async with workspace_app(live_settings(tmp_path, site)) as (_, client):
            headers = client_headers()
            session = f"/sessions/{(await create_session(client))['session_id']}"
            lease = await take_control(client, session, headers)
            state = await browser_state(client, session, headers)
            denied = await manual(client, session, headers, lease, state['revision'], 'open', url=private_site + '/details')
            assert denied.is_client_error
            state = await browser_state(client, session, headers)
            await manual(client, session, headers, lease, state['revision'], 'open', url=site + '/redirect')
            assert visits['GET /redirect'] == 1
            assert private_visits['GET /details'] == 0


async def stream_snapshot(client, session: str):
    async with asyncio.timeout(10):
        async with client.stream('GET', f'{session}/stream') as response:
            response.raise_for_status()
            assert response.headers['content-type'].startswith('text/event-stream')
            event = None
            async for line in response.aiter_lines():
                if line.startswith('event: '):
                    event = line.removeprefix('event: ')
                if event == 'workspace' and line.startswith('data: '):
                    return json.loads(line.removeprefix('data: '))
    pytest.fail('Stream closed without a workspace snapshot')


async def test_workspace_stream_recovers_persisted_history_over_http(tmp_path):
    with fixture_website() as (site, _):
        class Planner:
            async def decide(self, *, goal, observation, history):
                if observation.url != site + '/details':
                    return action('open', url=site + '/details')
                assert 'Invoice total: 26' in observation.ax_tree
                return finish('Invoice total is 26.')

        settings = live_settings(tmp_path, site)
        async with workspace_app(settings, planner=Planner(), network=True) as (_, client):
            created = await create_session(client)
            session = f"/sessions/{created['session_id']}"
            initial = await stream_snapshot(client, session)
            assert initial['session']['state']['run_status'] == 'idle'
            await run_task(client, session, f'Read the invoice total at {site}/details')
            await wait_state(client, session, 'completed')

        async with workspace_app(settings, network=True) as (_, client):
            snapshot = await stream_snapshot(client, session)
            assert snapshot['session']['state']['run_status'] == 'completed'
            assert snapshot['session']['state']['last_known_url'] == site + '/details'
            assert any('26' in message['content'] for message in snapshot['messages'] if message['role'] == 'assistant')
            assert any(event['page_url'] == site + '/details' for event in snapshot['events'])
            another = await create_session(client)
            denied = await client.get(f'{session}/browser', params={'branch_id': another['selected_branch_id']})
            assert denied.status_code == 404


async def test_interrupted_worker_reopens_profile_and_resumes_after_restart(tmp_path):
    observed = asyncio.Event()
    hold = asyncio.Event()
    with fixture_website() as (site, _):
        class InterruptedPlanner:
            async def decide(self, *, goal, observation, history):
                if observation.url != site + '/':
                    return action('open', url=site + '/')
                observed.set()
                await hold.wait()
                return finish('An interrupted planner must not finish.')

        class RestoredPlanner:
            async def decide(self, *, goal, observation, history):
                if observation.url == site + '/cookie':
                    assert observation.title == 'Cookie present'
                    return finish('Found the cookie in the restored browser profile.')
                assert observation.url == site + '/'
                return action('open', url=site + '/cookie')

        settings = live_settings(tmp_path, site)
        async with workspace_app(settings, planner=InterruptedPlanner()) as (_, client):
            session = f"/sessions/{(await create_session(client))['session_id']}"
            await run_task(client, session, f'Open {site}/ and verify the saved cookie at {site}/cookie')
            await asyncio.wait_for(observed.wait(), timeout=15)

        async with workspace_app(settings, planner=RestoredPlanner()) as (_, client):
            await wait_state(client, session, 'interrupted')
            (await client.post(f'{session}/resume', headers=client_headers())).raise_for_status()
            await wait_state(client, session, 'completed')
            messages = (await client.get(f'{session}/messages')).raise_for_status().json()
            assert 'Found the cookie in the restored browser profile.' in [message['content'] for message in messages]


async def test_manual_holder_reconnects_after_a_chromium_renderer_crash(tmp_path):
    with fixture_website() as (site, _):
        async with workspace_app(live_settings(tmp_path, site)) as (app, client):
            headers = client_headers()
            session_id = (await create_session(client))['session_id']
            session = f'/sessions/{session_id}'
            lease = await take_control(client, session, headers)
            state = await browser_state(client, session, headers)
            (await manual(client, session, headers, lease, state['revision'], 'open', url=site + '/')).raise_for_status()
            previous = await browser_state(client, session, headers)

            # Crash only this test's Chromium renderer. The workspace process stays up.
            ctx = await app.state.services.live.context(session_id, None)
            runtime, page = app.state.browser_adapter._active(ctx)
            devtools = await runtime.context.new_cdp_session(page)
            crash = asyncio.create_task(devtools.send('Page.crash'))
            try:
                async with asyncio.timeout(10):
                    while (await browser_state(client, session, headers))['ready']:
                        await asyncio.sleep(0.05)
                assert await take_control(client, session, headers) == lease
                reopened = await browser_state(client, session, headers)
                assert reopened['ready'] and reopened['url'] == site + '/'
                assert reopened['revision'] != previous['revision']
                (await manual(client, session, headers, lease, reopened['revision'], 'open', url=site + '/cookie')).raise_for_status()
                assert (await browser_state(client, session, headers))['title'] == 'Cookie present'
            finally:
                crash.cancel()
                await asyncio.gather(crash, return_exceptions=True)


async def test_workspace_ui_displays_shared_pixels_and_forwards_real_input(tmp_path, record_property):
    from playwright.async_api import async_playwright, expect

    held = asyncio.Event()
    with fixture_website() as (site, _):
        class Planner:
            async def decide(self, *, goal, observation, history):
                if observation.url != site + '/':
                    return action('open', url=site + '/')
                if 'Review count 1 - K' in observation.ax_tree:
                    return finish('Verified the workspace keyboard change.')
                await held.wait()
                return finish('No browser update was observed.')

        settings = live_settings(tmp_path, site)
        async with workspace_app(settings, planner=Planner(), network=True) as (_, client), async_playwright() as driver:
            viewer = await driver.chromium.launch(executable_path=settings.browser_executable_path, headless=True)
            try:
                page = await viewer.new_page(viewport={'width': 1280, 'height': 850})
                await page.goto(str(client.base_url))
                await page.locator('#new-session-btn').click()
                await page.locator('#chat-input').fill(f'Review the worksheet at {site}/ with me')
                await page.locator('#send-btn').click()
                frame = page.locator('#browser-frame')
                await expect(frame).to_be_visible(timeout=20_000)
                await expect(page.locator('#browser-title')).to_have_text('Review worksheet', timeout=15_000)
                await page.evaluate('''() => {
                    const frame = document.querySelector('#browser-frame');
                    const canvas = document.createElement('canvas');
                    canvas.width = canvas.height = 1;
                    const context = canvas.getContext('2d', {willReadFrequently: true});
                    window.frameProbe = () => {
                        if (!frame.complete || !frame.naturalWidth) return [];
                        context.drawImage(frame, 240, 100, 1, 1, 0, 0, 1, 1);
                        return context.getImageData(0, 0, 1, 1).data;
                    };
                    document.querySelector('#viewport').addEventListener('pointerup', () => {
                        window.frameProbeStart = performance.now();
                    }, {once: true, capture: true});
                }''')
                await page.wait_for_function('() => { const [r, g] = window.frameProbe(); return r > g; }')
                await page.locator('#takeover-btn').click()
                await expect(page.locator('#release-btn')).to_be_visible()
                width, height = await frame.evaluate('(image) => [image.naturalWidth, image.naturalHeight]')
                assert width and height
                bounds = await frame.bounding_box()
                assert bounds
                async with page.expect_response(is_command_response) as clicked:
                    await page.mouse.click(bounds['x'] + 70 / width * bounds['width'], bounds['y'] + 100 / height * bounds['height'])
                assert (await clicked.value).ok
                painted = await page.wait_for_function('''() => {
                    const [red, green] = window.frameProbe();
                    return green > red && performance.now() - window.frameProbeStart;
                }''')
                record_property('viewer_click_to_visible_pixel_ms', round(await painted.json_value(), 2))
                async with page.expect_response(is_command_response) as typed:
                    await page.keyboard.press('K')
                assert (await typed.value).ok
                await page.locator('#release-btn').click()
                await expect(page.locator('#run-status')).to_have_attribute('data-status', 'completed', timeout=15_000)
                await expect(page.locator('#chat-list')).to_contain_text('Verified the workspace keyboard change.')
                await page.reload()
                await expect(page.locator('#chat-list')).to_contain_text('Verified the workspace keyboard change.')
                await expect(frame).to_be_visible(timeout=15_000)
            finally:
                held.set()
                await viewer.close()


def is_command_response(response):
    return urlsplit(response.url).path.endswith('/browser/command')


async def test_guided_demo_uses_builtin_planner_and_real_reservation_site(tmp_path):
    from playwright.async_api import async_playwright, expect

    with demo_website() as site:
        settings = live_settings(tmp_path, site)
        settings.browser_demo_url = site + '/'
        async with workspace_app(settings, network=True) as (_, client), async_playwright() as driver:
            viewer = await driver.chromium.launch(executable_path=settings.browser_executable_path, headless=True)
            try:
                page = await viewer.new_page(viewport={'width': 1280, 'height': 850})
                await page.goto(str(client.base_url))
                await page.locator('#new-session-btn').click()
                await page.locator('#demo-btn').click()
                await expect(page.locator('#run-status')).to_have_attribute('data-status', 'awaiting_input', timeout=18_000)
                await expect(page.locator('#browser-title')).to_have_text('Reservation details')
                frame = page.locator('#browser-frame')
                await expect(frame).to_be_visible()
                await page.locator('#takeover-btn').click()
                await expect(page.locator('#release-btn')).to_be_visible()
                width, height = await frame.evaluate('(image) => [image.naturalWidth, image.naturalHeight]')

                # Independently locate the known fixture controls in its native viewport.
                reference = await viewer.new_page(viewport={'width': width, 'height': height})
                await reference.goto(site + '/reservation')
                field = await reference.get_by_label('Sample attendee name').bounding_box()
                submit = await reference.get_by_role('button', name='Confirm reservation').bounding_box()
                await reference.close()
                assert field and submit

                async def click_remote(element):
                    bounds = await frame.bounding_box()
                    assert bounds
                    x = bounds['x'] + (element['x'] + element['width'] / 2) * bounds['width'] / width
                    y = bounds['y'] + (element['y'] + element['height'] / 2) * bounds['height'] / height
                    async with page.expect_response(is_command_response) as pending:
                        await page.mouse.click(x, y)
                    return (await pending.value)

                assert (await click_remote(field)).ok
                async with page.expect_response(is_command_response) as typed:
                    await page.keyboard.press('K')
                typed_response = await typed.value
                assert typed_response.ok
                revision = (await typed_response.json())['revision']
                await page.wait_for_function('(revision) => renderedFrame?.revision === revision', arg=revision)
                assert (await click_remote(submit)).ok
                await expect(page.locator('#browser-title')).to_have_text('Reservation received', timeout=12_000)
                await page.locator('#release-btn').click()
                await expect(page.locator('#run-status')).to_have_attribute('data-status', 'completed', timeout=15_000)
                await expect(page.locator('#chat-list')).to_contain_text('I found the reservation confirmation')
            finally:
                await viewer.close()
