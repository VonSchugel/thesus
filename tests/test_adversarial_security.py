from __future__ import annotations

import asyncio
import base64
from http.server import BaseHTTPRequestHandler
from threading import Event
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.agents.browser_planner import AgentDecision
from app.integrations.browser.egress_proxy import BrowserEgressProxy
from app.models.domain import SessionContext
from tests.browser_harness import fixture_website, live_settings, serve_website, workspace_app
from tests.conftest import create_session
from tests.test_browser_workspace import action, browser_state, client_headers, finish, manual, run_task, target, take_control, wait_state


async def test_connect_tunnel_closes_silent_upstream_after_client_disconnect():
    upstream_eof = asyncio.Event()

    async def upstream(reader, writer):
        try:
            await reader.read()
            upstream_eof.set()
        finally:
            writer.close()

    server = await asyncio.start_server(upstream, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]

    async def resolve(url: str):
        assert url == f'https://127.0.0.1:{port}/'
        return ['127.0.0.1'], port

    proxy = BrowserEgressProxy(resolve)
    try:
        access = await proxy.start()
        address = urlsplit(access['server'])
        reader, writer = await asyncio.open_connection(address.hostname, address.port)
        auth = base64.b64encode(f"{access['username']}:{access['password']}".encode()).decode()
        target = f'127.0.0.1:{port}'
        writer.write(f'CONNECT {target} HTTP/1.1\r\nHost: {target}\r\nProxy-Authorization: Basic {auth}\r\n\r\n'.encode())
        try:
            await writer.drain()
            assert (await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout=2)).startswith(b'HTTP/1.1 200 ')
        finally:
            writer.close()
        await asyncio.wait_for(upstream_eof.wait(), timeout=1)
    finally:
        await proxy.close()
        server.close()
        await asyncio.wait_for(server.wait_closed(), timeout=2)


async def test_untrusted_tcp_peer_cannot_use_loopback_host_without_token(tmp_path):
    with fixture_website() as (site, _):
        settings = live_settings(tmp_path, site)
        async with workspace_app(settings) as (app, loopback):
            assert (await loopback.get('/sessions')).status_code == 200
            transport = httpx.ASGITransport(app=app, client=('192.0.2.41', 51425))
            async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1') as remote:
                response = await remote.post('/sessions', json={'title': 'remote attempt'})
                assert response.status_code in {401, 403}
            assert (await loopback.get('/sessions')).json() == []

        settings.workspace_access_token = 'synthetic-workspace-token'
        async with workspace_app(settings) as (app, _):
            transport = httpx.ASGITransport(app=app, client=('192.0.2.41', 51425))
            async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1') as remote:
                assert (await remote.get('/sessions')).status_code == 401
                authorized = await remote.post('/sessions', json={'title': 'authorized remote'},
                                               headers={'Authorization': 'Bearer synthetic-workspace-token'})
                assert authorized.status_code == 200


@pytest.mark.parametrize(('method', 'mutation'), [('post', 'pending'), ('post', 'dispatch'), ('post', 'none'),
                                                ('get', 'dispatch'), ('get', 'none')])
async def test_approved_form_dispatch_matches_observed_hidden_fields(tmp_path, method, mutation):
    change_amount = Event()
    submitted: list[bytes] = []

    class Page(BaseHTTPRequestHandler):
        def reply(self, content: bytes) -> None:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            if self.path == '/form':
                script = {
                    'pending': b'''<script>fetch('/switch').then(response => {
                        if (!response.ok) return;
                        document.querySelector('input').value = '9000';
                        document.title = 'Payment changed';
                    });</script>''',
                    'dispatch': b'''<script>document.querySelector('button').addEventListener('click', () => {
                        document.querySelector('input').value = '9000';
                    });</script>''',
                    'none': b'',
                }[mutation]
                self.reply(b'''<!doctype html><title>Payment prepared</title>
                  <form action="/submit" method="FORM_METHOD">
                    <input name="amount" type="hidden" value="10">
                    <textarea name="note">hello ~*
next</textarea>
                    <button type="submit">Send payment</button>
                  </form>'''.replace(b'FORM_METHOD', method.encode()) + script)
            elif self.path == '/switch' and change_amount.wait(timeout=12):
                self.reply(b'ready')
            elif urlsplit(self.path).path == '/submit':
                submitted.append(urlsplit(self.path).query.encode())
                self.reply(b'<!doctype html><title>Payment submitted</title>')
            else:
                self.send_error(404)

        def do_POST(self):
            submitted.append(self.rfile.read(int(self.headers['Content-Length'])))
            self.reply(b'<!doctype html><title>Payment submitted</title>')

        def log_message(self, *_):
            pass

    with serve_website(Page) as site:
        form = f'{site}/form'

        class Planner:
            async def decide(self, *, goal, observation, history):
                if observation.url.startswith(f'{site}/submit'):
                    return finish('Observed the submitted payment.')
                if observation.url != form:
                    return action('open', url=form)
                return action('click', target_id=target(observation, 'Send payment'))

        async with workspace_app(live_settings(tmp_path, site), planner=Planner()) as (app, client):
            session = f"/sessions/{(await create_session(client))['session_id']}"
            try:
                await run_task(client, session, f'Open and send the payment at {form}')
                pending = await wait_state(client, session, 'awaiting_approval')
                if mutation == 'pending':
                    change_amount.set()
                    async with asyncio.timeout(8):
                        while (await client.get(f'{session}/browser')).json()['title'] != 'Payment changed':
                            await asyncio.sleep(0.03)
                decision = await client.post(f'{session}/approve', json={'correlation_id': pending['approval_correlation_id']})
                assert decision.status_code in {200, 409}
                await app.state.execution_manager.wait_for_idle(timeout=10)
                assert submitted == ([b'amount=10&note=hello+%7E*%0D%0Anext'] if mutation == 'none' else [])
                assert 'amount=9000' not in (await client.get(f'{session}/events')).text
            finally:
                change_amount.set()


async def test_manual_synthetic_login_resumes_the_same_authenticated_browser(tmp_path):
    password = 'synthetic-audit-password-4c9176'
    submitted = []

    class LoginPage(BaseHTTPRequestHandler):
        def reply(self, content: bytes) -> None:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            if self.path == '/login':
                self.reply(b'''<!doctype html><title>Sign in</title><h1>Sign in</h1>
                  <form action="/login" method="post">
                    <input type="password" name="password" aria-label="Test password"
                      style="position:absolute;top:80px;left:30px;width:220px;height:30px">
                    <button type="submit" style="position:absolute;top:125px;left:30px">Sign in</button>
                  </form>''')
            elif self.path == '/private' and 'audit_login=yes' in self.headers.get('Cookie', ''):
                self.reply(b'<!doctype html><title>Private account</title><h1>Private invoice total: 46</h1>')
            else:
                self.send_error(403)

        def do_POST(self):
            fields = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
            if self.path != '/login' or fields.get('password') != [password]:
                self.send_error(403)
                return
            submitted.append('one synthetic login')
            self.send_response(303)
            self.send_header('Location', '/private')
            self.send_header('Set-Cookie', 'audit_login=yes; HttpOnly; SameSite=Strict; Path=/')
            self.send_header('Content-Length', '0')
            self.end_headers()

        def log_message(self, *_):
            pass

    with serve_website(LoginPage) as site:
        login = f'{site}/login'

        class Planner:
            async def decide(self, *, goal, observation, history):
                assert password not in observation.model_dump_json()
                if observation.url == f'{site}/private':
                    assert 'Private invoice total: 46' in observation.ax_tree
                    return finish('The authenticated page reports an invoice total of 46.')
                if observation.url == login:
                    return AgentDecision(kind='clarify', summary='Sign in to the test account manually.')
                return action('open', url=login)

        async with workspace_app(live_settings(tmp_path, site), planner=Planner()) as (app, client):
            created = await create_session(client)
            session = f"/sessions/{created['session_id']}"
            await run_task(client, session, f'Open {login} and read the private test invoice.')
            await wait_state(client, session, 'awaiting_input')
            headers = client_headers()
            lease = await take_control(client, session, headers)
            state = await browser_state(client, session, headers)
            tab = state['active_tab_id']
            focused = await manual(client, session, headers, lease, state['revision'], 'click', x=70, y=100)
            typed = await manual(client, session, headers, lease, focused.raise_for_status().json()['revision'], 'type', text=password)
            ctx = SessionContext(session_id=created['session_id'], branch_id=created['default_branch_id'])
            observed = await app.state.browser_adapter.observe(ctx)
            assert password not in observed.model_dump_json()
            sign_in = await manual(client, session, headers, lease, typed.raise_for_status().json()['revision'], 'key', key='Enter')
            sign_in.raise_for_status()
            async with asyncio.timeout(8):
                while (state := await browser_state(client, session, headers))['url'] != f'{site}/private':
                    await asyncio.sleep(0.03)
            assert state['active_tab_id'] == tab
            (await client.post(f'{session}/release', headers=headers, json={'lease_id': lease, 'resume': True})).raise_for_status()
            await wait_state(client, session, 'completed')
            messages = (await client.get(f'{session}/messages')).raise_for_status().text
            assert 'invoice total of 46' in messages
            assert password not in messages
            assert password not in (await client.get(f'{session}/events')).raise_for_status().text
            assert submitted == ['one synthetic login']
