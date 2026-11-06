from __future__ import annotations

import asyncio
import json
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import async_playwright, expect
from sqlalchemy import text

from tests.browser_harness import fixture_website, live_settings, serve_website, workspace_app
from tests.conftest import create_session
from tests.test_browser_workspace import action, browser_state, client_headers, finish, manual, take_control, target, wait_state


TEST_PASSWORD = 'workspace-fixture-password'


@contextmanager
def auth_site():
    visits = Counter()

    class Handler(BaseHTTPRequestHandler):
        def html(self, body):
            encoded = body.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            visits[f'GET {self.path}'] += 1
            if self.path == '/sign-in':
                self.html('''<!doctype html><title>Synthetic sign-in</title><h1>Sign in</h1>
                    <form method="post" action="/sign-in">
                      <input aria-label="Fixture password" type="password" name="password"
                             style="position:absolute;left:30px;top:72px;width:220px;height:26px" required>
                      <button style="position:absolute;left:30px;top:115px;width:120px;height:26px">Sign in</button>
                    </form>''')
            elif self.path == '/help':
                self.html('<!doctype html><title>Audit help</title><h1>Read-only audit help</h1>')
            elif self.path == '/dashboard' and 'fixture-auth=valid' in self.headers.get('Cookie', ''):
                self.html('''<!doctype html><title>Authenticated dashboard</title><h1>Authenticated dashboard</h1>
                    <input aria-label="Reviewer note" style="position:absolute;left:30px;top:75px;width:220px;height:26px"
                           oninput="document.getElementById('verified').textContent = 'Review evidence: ' + this.value">
                    <p id="verified" style="margin-top:115px">Review evidence pending</p>''')
            else:
                self.send_error(401 if self.path == '/dashboard' else 404)

        def do_POST(self):
            visits[f'POST {self.path}'] += 1
            data = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
            if self.path != '/sign-in' or data.get('password') != [TEST_PASSWORD]:
                self.send_error(401)
                return
            self.send_response(303)
            self.send_header('Set-Cookie', 'fixture-auth=valid; HttpOnly; SameSite=Strict; Path=/')
            self.send_header('Location', '/dashboard')
            self.send_header('Content-Length', '0')
            self.end_headers()

        def log_message(self, *_):
            pass

    with serve_website(Handler) as site:
        yield site, visits


def is_manual_response(response):
    return urlsplit(response.url).path.endswith('/browser/command')


async def click_remote(page, x, y):
    frame = page.locator('#browser-frame')
    await expect(frame).to_be_visible()
    await page.wait_for_function('() => renderedFrame?.revision === active?.browser?.revision')
    bounds = await frame.bounding_box()
    width, height = await frame.evaluate('image => [image.naturalWidth, image.naturalHeight]')
    async with page.expect_response(is_manual_response) as result:
        await page.mouse.click(bounds['x'] + x * bounds['width'] / width,
                               bounds['y'] + y * bounds['height'] / height)
    response = await result.value
    assert response.request.post_data_json['frame_id']
    assert response.ok


async def type_remote(page, value):
    await page.locator('#text-btn').click()
    await page.locator('#remote-text').fill(value)
    async with page.expect_response(is_manual_response) as result:
        await page.locator('#text-form button[type=submit]').click()
    assert (await result.value).ok


async def test_real_ui_pauses_authenticates_switches_tabs_and_resumes_after_reconnect(tmp_path):
    login_observed = asyncio.Event()
    paused_model = asyncio.Event()
    resumed_model = asyncio.Event()
    allow_answer = asyncio.Event()
    stop_model = asyncio.Event()
    stopped_model = asyncio.Event()
    observations = []

    with auth_site() as (site, visits):
        login = site + '/sign-in'
        dashboard = site + '/dashboard'

        class Planner:
            async def decide(self, *, goal, observation, history):
                observations.append((goal, observation.url, observation.ax_tree))
                if goal == 'Hold this task until I stop it':
                    stop_model.set()
                    try:
                        await asyncio.Future()
                    finally:
                        stopped_model.set()
                if observation.url == 'about:blank':
                    return action('open', url=login)
                if observation.url == login:
                    login_observed.set()
                    try:
                        await asyncio.Future()
                    finally:
                        paused_model.set()
                assert observation.url == dashboard
                assert 'Review evidence: checked-by-auditor' in observation.ax_tree
                resumed_model.set()
                await allow_answer.wait()
                return finish('I verified checked-by-auditor on the authenticated dashboard.')

        settings = live_settings(tmp_path, site)
        async with workspace_app(settings, planner=Planner(), network=True) as (_, client), async_playwright() as playwright:
            viewer = await playwright.chromium.launch(executable_path=settings.browser_executable_path)
            try:
                page = await viewer.new_page(viewport={'width': 1280, 'height': 800})
                await page.goto(str(client.base_url))
                await page.locator('#new-session-btn').click()
                await page.locator('#chat-input').fill(f'Open {login} and verify the manual review evidence after sign-in.')
                await page.locator('#send-btn').click()
                await asyncio.wait_for(login_observed.wait(), 12)
                await expect(page.locator('#browser-title')).to_have_text('Synthetic sign-in')
                await expect(page.locator('#browser-frame')).to_be_visible()
                await page.locator('#activity-tab').click()
                await expect(page.locator('#event-list')).to_contain_text('Navigated to the requested address')
                await page.locator('#pause-btn').click()
                await asyncio.wait_for(paused_model.wait(), 5)
                await expect(page.locator('#run-status')).to_have_attribute('data-status', 'paused')
                await page.locator('#takeover-btn').click()
                await expect(page.locator('#release-btn')).to_be_visible()
                await expect(page.locator('#owner')).to_have_text('You have control')
                base = '/sessions/' + (await page.locator('#session-select').input_value())

                observer = await viewer.new_page(viewport={'width': 1280, 'height': 800})
                await observer.goto(str(client.base_url))
                await expect(observer.locator('#browser-title')).to_have_text('Synthetic sign-in')
                await expect(observer.locator('#navigate')).to_be_disabled()
                async with observer.expect_response(lambda response: urlsplit(response.url).path == f'{base}/takeover') as denial:
                    await observer.locator('#takeover-btn').click()
                assert (await denial.value).status == 409
                await observer.close()

                await page.locator('#new-tab').click()
                await expect(page.locator('#address')).to_have_value('about:blank')
                await page.locator('#address').fill(site + '/help')
                await page.locator('#navigate').click()
                await expect(page.locator('#browser-title')).to_have_text('Audit help')
                await page.get_by_role('button', name='Close Audit help').click()
                await expect(page.locator('#browser-title')).to_have_text('Synthetic sign-in')

                await click_remote(page, 60, 85)
                await type_remote(page, TEST_PASSWORD)
                await click_remote(page, 60, 127)
                await expect(page.locator('#browser-title')).to_have_text('Authenticated dashboard')
                await click_remote(page, 60, 85)
                await type_remote(page, 'checked-by-auditor')
                await page.locator('#release-btn').click()
                await asyncio.wait_for(resumed_model.wait(), 8)
                await page.close()
                allow_answer.set()
                await wait_state(client, base, 'completed')

                reopened = await viewer.new_page(viewport={'width': 1280, 'height': 800})
                await reopened.goto(str(client.base_url))
                await expect(reopened.locator('#run-status')).to_have_attribute('data-status', 'completed')
                await expect(reopened.locator('#chat-list')).to_contain_text('I verified checked-by-auditor')
                await expect(reopened.locator('#browser-title')).to_have_text('Authenticated dashboard')
                await expect(reopened.locator('#browser-frame')).to_be_visible()
                await reopened.locator('#chat-input').fill('Hold this task until I stop it')
                await reopened.locator('#send-btn').click()
                await asyncio.wait_for(stop_model.wait(), 8)
                await reopened.locator('#stop-btn').click()
                await asyncio.wait_for(stopped_model.wait(), 5)
                await expect(reopened.locator('#run-status')).to_have_attribute('data-status', 'cancelled')

                assert visits['GET /help'] == 1
                assert visits['POST /sign-in'] == 1
                assert visits['GET /dashboard'] == 1
                assert TEST_PASSWORD not in str(observations)
                for suffix in ('messages', 'events'):
                    response = (await client.get(f'{base}/{suffix}')).raise_for_status()
                    assert TEST_PASSWORD not in response.text
            finally:
                allow_answer.set()
                await viewer.close()


async def test_ui_can_pause_before_approving_and_waits_for_resume(tmp_path):
    with fixture_website() as (site, visits):
        destination = f'{site}/approval'

        class Planner:
            async def decide(self, *, goal, observation, history):
                if observation.url == 'about:blank':
                    return action('open', url=destination)
                if observation.url == destination:
                    return action('click', target_id=target(observation, 'Submit first record'))
                assert 'Record committed' in observation.ax_tree
                return finish('Verified the submitted record.')

        settings = live_settings(tmp_path, site)
        async with workspace_app(settings, planner=Planner(), network=True) as (_, client), async_playwright() as playwright:
            viewer = await playwright.chromium.launch(executable_path=settings.browser_executable_path)
            try:
                page = await viewer.new_page(viewport={'width': 1280, 'height': 800})
                await page.goto(str(client.base_url))
                await page.locator('#new-session-btn').click()
                await page.locator('#chat-input').fill(f'Open {destination} and submit the first fixture record.')
                await page.locator('#send-btn').click()
                await expect(page.locator('#approval')).to_be_visible(timeout=12_000)
                await page.locator('#pause-btn').click()
                await expect(page.locator('#current-action')).to_contain_text('Approval waits for Resume.')
                await expect(page.locator('#pause-btn')).to_be_disabled()
                await page.locator('#approve-btn').click()
                await expect(page.locator('#run-status')).to_have_attribute('data-status', 'paused')
                await expect(page.locator('#approval')).to_be_hidden()
                assert visits['POST /commit-a'] == 0
                await page.locator('#resume-btn').click()
                await expect(page.locator('#chat-list')).to_contain_text('Verified the submitted record.')
                assert visits['POST /commit-a'] == 1
                assert visits['POST /commit-b'] == 0
            finally:
                await viewer.close()


async def test_delayed_viewport_frame_cannot_restore_a_closed_browser(tmp_path):
    captured = asyncio.Event()
    release_frame = asyncio.Event()
    intercepted_path = None

    with fixture_website() as (site, _):
        settings = live_settings(tmp_path, site)
        async with workspace_app(settings, network=True) as (_, client), async_playwright() as playwright:
            viewer = await playwright.chromium.launch(executable_path=settings.browser_executable_path)
            try:
                session = await create_session(client, 'Delayed screenshot fixture')
                base = f'/sessions/{session["session_id"]}'
                headers = client_headers()
                lease = await take_control(client, base, headers)
                current = await browser_state(client, base, headers)
                (await manual(client, base, headers, lease, current['revision'], 'open', url=site + '/')).raise_for_status()
                (await client.post(f'{base}/release', headers=headers, json={'lease_id': lease, 'resume': False})).raise_for_status()

                page = await viewer.new_page()
                await page.goto(str(client.base_url))
                frame = page.locator('#browser-frame')
                await expect(frame).to_be_visible(timeout=10_000)

                async def delay_response(route):
                    nonlocal intercepted_path
                    response = await route.fetch()
                    if response.ok and not captured.is_set():
                        intercepted_path = urlsplit(route.request.url).path
                        captured.set()
                        await release_frame.wait()
                    await route.fulfill(response=response)

                await page.route('**/browser/frame?*', delay_response)
                await asyncio.wait_for(captured.wait(), 5)
                await page.locator('#close-browser-btn').click()
                await expect(frame).to_be_hidden()
                await page.evaluate('''() => {
                    const frame = document.querySelector('#browser-frame');
                    window.closedBrowserFrameShown = false;
                    new MutationObserver(() => {
                        if (!frame.hidden && frame.hasAttribute('src')) window.closedBrowserFrameShown = true;
                    }).observe(frame, {attributes: true, attributeFilter: ['hidden', 'src']});
                }''')
                async with page.expect_response(lambda response: urlsplit(response.url).path == intercepted_path):
                    release_frame.set()
                # Allow Chromium to decode the released JPEG and commit DOM changes.
                await page.evaluate('''() => new Promise(resolve => setTimeout(resolve, 150))''')
                assert not await page.evaluate('window.closedBrowserFrameShown')
                assert not (await browser_state(client, base, headers))['ready']
            finally:
                release_frame.set()
                await viewer.close()


async def first_workspace_snapshot(client, base, source):
    if source == 'workspace':
        return (await client.get(f'{base}/workspace')).raise_for_status().json()
    async with client.stream('GET', f'{base}/stream') as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith('data: '):
                return json.loads(line[6:])
    raise AssertionError('Workspace stream ended without a snapshot')


@pytest.mark.parametrize('source', ['stream', 'workspace'])
async def test_workspace_snapshot_does_not_mix_commits(tmp_path, monkeypatch, source):
    state_loaded = asyncio.Event()
    finish_snapshot = asyncio.Event()
    instruction = 'Instruction committed during a suspended workspace read'
    with fixture_website() as (site, _):
        async with workspace_app(live_settings(tmp_path, site), network=True) as (app, client):
            session = await create_session(client, 'Snapshot consistency fixture')
            base = f'/sessions/{session["session_id"]}'
            async with app.state.engine.connect() as connection:
                # WAL lets the deliberate concurrent writer commit while the reader stays suspended.
                assert (await connection.execute(text('PRAGMA journal_mode=WAL'))).scalar_one() == 'wal'
            services = app.state.services
            original = services._build_session_response

            async def suspend_after_state(db, session_id, branch_id):
                state = await original(db, session_id, branch_id)
                if not state_loaded.is_set():
                    state_loaded.set()
                    await finish_snapshot.wait()
                return state

            monkeypatch.setattr(services, '_build_session_response', suspend_after_state)
            reading = asyncio.create_task(first_workspace_snapshot(client, base, source))
            try:
                await asyncio.wait_for(state_loaded.wait(), 5)
                (await client.post(f'{base}/messages', json={'content': instruction, 'trigger_agents': False})).raise_for_status()
            finally:
                finish_snapshot.set()
                captured = await asyncio.wait_for(reading, 5)

            assert captured['session']['state']['run_status'] == 'idle'
            assert all(message['content'] != instruction for message in captured['messages'])
            later = await first_workspace_snapshot(client, base, source)
            assert later['session']['state']['run_status'] == 'paused'
            assert any(message['content'] == instruction for message in later['messages'])
