"""Exercise an isolated browser through HTTP and record handoff, recovery and resource evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import perf_counter
from uuid import uuid4

from scripts.benchmark_workspace import process_metrics, summary
from tests.browser_harness import live_settings, serve_website, workspace_app
from tests.conftest import create_session
from tests.test_browser_workspace import action, browser_state, client_headers, finish, manual, run_task, take_control, target, wait_state


@contextmanager
def probe_website(pages):
    visits = Counter()
    shift = Event()

    class Handler(BaseHTTPRequestHandler):
        def send_fixture(self, body):
            encoded = body.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            visits[f'GET {self.path}'] += 1
            if self.path.startswith('/step/'):
                step = int(self.path.rsplit('/', 1)[-1])
                link = f'<a href="/step/{step + 1}">Next record</a>' if step < pages else ''
                self.send_fixture(f'<!doctype html><title>Record {step}</title><h1>Verified record {step}</h1>{link}')
            elif self.path == '/click-cost':
                self.send_fixture('''<!doctype html><title>Click cost fixture</title>
                    <button style="position:absolute;top:60px;left:20px" onclick="document.title += '.'">Record review</button>''')
            elif self.path == '/shift-fixture':
                self.send_fixture('''<!doctype html><title>Before layout shift</title>
                    <form method="post" action="/expected"><button style="position:absolute;top:60px;left:20px">Keep record</button></form>
                    <form method="post" action="/unexpected"><button style="position:absolute;top:150px;left:20px">Replace record</button></form>
                    <script>fetch('/shift').then(() => {
                        const [keep, replace] = document.querySelectorAll('button');
                        keep.style.top = '150px'; replace.style.top = '60px'; document.title = 'After layout shift';
                    });</script>''')
            elif self.path == '/shift' and shift.wait(timeout=10):
                self.send_fixture('OK')
            else:
                self.send_error(404)

        def do_POST(self):
            visits[f'POST {self.path}'] += 1
            self.send_fixture('<!doctype html><title>Submitted</title>Submitted')

        def log_message(self, *_):
            pass

    with serve_website(Handler) as site:
        yield site, visits, shift


async def read_snapshot(client, path, *, previous=None, accept=lambda _: True):
    headers = {'Last-Event-ID': previous} if previous else {}
    async with asyncio.timeout(20):
        async with client.stream('GET', f'{path}/stream', headers=headers) as response:
            response.raise_for_status()
            event_id = None
            async for line in response.aiter_lines():
                if line.startswith('id: '):
                    event_id = line[4:]
                if line.startswith('data: '):
                    snapshot = json.loads(line[6:])
                    if accept(snapshot):
                        return event_id, snapshot, len(line[6:].encode())
    raise AssertionError('Workspace event stream ended unexpectedly')


def rss_mib():
    sample = process_metrics()
    return round(sample[1] / 1024**2, 2) if sample else None


async def compare_poll_paths(client, base):
    async def split():
        urls = (base, f'{base}/messages?limit=120', f'{base}/events?limit=180')
        replies = await asyncio.gather(*(client.get(url) for url in urls))
        return dict(zip(('session', 'messages', 'events'), [reply.raise_for_status().json() for reply in replies]))

    async def unified():
        return (await client.get(f'{base}/workspace')).raise_for_status().json()

    assert await split() == await unified()
    timings = {'split': [], 'unified': []}
    for index in range(10):
        order = [('split', split), ('unified', unified)]
        for name, read in order[index % 2:] + order[:index % 2]:
            started = perf_counter()
            await read()
            timings[name].append((perf_counter() - started) * 1000)
    return {name: summary(samples) for name, samples in timings.items()}


async def long_run(pages):
    points = [pages // 3, 2 * pages // 3]
    observed = {point: asyncio.Event() for point in points}
    seen = []
    context_sizes = []
    history_sizes = []
    delays = []
    memory = {'before': rss_mib()}

    with probe_website(pages) as (site, visits, _), TemporaryDirectory(prefix='thesus-audit-long-') as folder:
        class Planner:
            async def decide(self, *, goal, observation, history):
                if observation.url == 'about:blank':
                    return action('open', url=f'{site}/step/1')
                step = int(observation.url.rsplit('/', 1)[-1])
                assert f'Verified record {step}' in observation.ax_tree
                seen.append(step)
                context_sizes.append(len(observation.ax_tree))
                history_sizes.append(len(history))
                milestone = observed.get(step)
                if milestone and not milestone.is_set():
                    milestone.set()
                    await asyncio.Future()
                if step == pages:
                    return finish(f'Verified all {pages} records.')
                return action('open_link', target_id=target(observation, 'Next record'))

        settings = live_settings(Path(folder), site)
        settings.browser_agent_max_steps = pages + 5
        async with workspace_app(settings, planner=Planner(), network=True) as (_, client):
            session = await create_session(client, 'Long operational probe')
            base = f'/sessions/{session["session_id"]}'
            headers = client_headers()
            initial_id, _, _ = await read_snapshot(client, base)
            started = perf_counter()
            await run_task(client, base, f'Open {site}/step/1 and inspect all {pages} records in order.')
            previous_id = initial_id
            for point in points:
                await asyncio.wait_for(observed[point].wait(), 60)
                delivery = asyncio.create_task(read_snapshot(client, base, previous=previous_id, accept=lambda item: (
                    item['session']['state']['run_status'] == 'paused'
                )))
                try:
                    (await client.post(f'{base}/pause')).raise_for_status()
                    previous_id, snapshot, _ = await delivery
                finally:
                    if not delivery.done():
                        delivery.cancel()
                        await asyncio.gather(delivery, return_exceptions=True)
                paused = next(event for event in reversed(snapshot['events']) if event['action_type'] == 'pause')
                stamp = datetime.fromisoformat(paused['created_at'])
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                delays.append(round((datetime.now(timezone.utc) - stamp).total_seconds() * 1000, 2))
                lease = await take_control(client, base, headers)
                (await client.post(f'{base}/snapshot', headers=headers, json={'note': f'Checkpoint at {point}'})).raise_for_status()
                memory[f'record_{point}'] = rss_mib()
                (await client.post(f'{base}/release', headers=headers, json={'lease_id': lease, 'resume': True})).raise_for_status()

            completed_id, snapshot, payload_bytes = await read_snapshot(client, base, previous=initial_id, accept=lambda item: (
                item['session']['state']['run_status'] == 'completed'
            ))
            elapsed = perf_counter() - started
            assert completed_id != initial_id
            assert snapshot['session']['state']['last_known_url'] == f'{site}/step/{pages}'
            assert all(visits[f'GET /step/{step}'] == 1 for step in range(1, pages + 1))
            assert set(seen) == set(range(1, pages + 1))
            assert len(seen) == pages + len(points)
            assert max(history_sizes) <= 8
            durable = (await client.get(f'{base}/events', params={'limit': 5000})).raise_for_status().json()
            events = [durable[0]]
            cursor = durable[0]['event_id']
            while True:
                batch = (await client.get(f'{base}/events', params={'limit': 35, 'after_event_id': cursor})).raise_for_status().json()
                if not batch:
                    break
                events.extend(batch)
                cursor = batch[-1]['event_id']
            ids = [event['event_id'] for event in events]
            assert len(ids) == len(set(ids))
            assert ids == [event['event_id'] for event in durable]
            assert [event['event_id'] for event in snapshot['events']] == ids[-180:]
            checkpoints = (await client.get(f'{base}/checkpoints')).raise_for_status().json()
            memory['completed'] = rss_mib()
            poll_cost = await compare_poll_paths(client, base)
            (await client.post(f'{base}/browser/close', headers=headers, json={})).raise_for_status()
            await asyncio.sleep(0.25)
            memory['closed'] = rss_mib()
            result = {'records': pages, 'elapsed_s': round(elapsed, 2), 'page_requests': sum(
                visits[f'GET /step/{step}'] for step in range(1, pages + 1)), 'observations': len(seen),
                'retained_events': len(ids), 'reconnected_snapshot_events': len(snapshot['events']),
                'reconnected_snapshot_json_bytes': payload_bytes, 'checkpoints': len(checkpoints),
                'pause_event_created_to_sse_ms': delays, 'max_observed_ax_chars': max(context_sizes),
                'max_recent_history_entries': max(history_sizes), 'process_tree_working_set_mib': memory,
                'workspace_read_ms': poll_cost,
                'model_tokens': 0}
        memory['after_workspace_shutdown'] = rss_mib()
    return result


async def passive_shift():
    with probe_website(3) as (site, visits, shift), TemporaryDirectory(prefix='thesus-audit-layout-') as folder:
        async with workspace_app(live_settings(Path(folder), site), network=True) as (_, client):
            session = await create_session(client, 'Passive layout probe')
            base = f'/sessions/{session["session_id"]}'
            headers = client_headers()
            lease = await take_control(client, base, headers)
            blank = await browser_state(client, base, headers)
            (await manual(client, base, headers, lease, blank['revision'], 'open', url=site + '/shift-fixture')).raise_for_status()
            old = (await client.get(f'{base}/browser/frame', headers=headers)).raise_for_status()
            shift.set()
            async with asyncio.timeout(8):
                while (state := await browser_state(client, base, headers))['title'] != 'After layout shift':
                    await asyncio.sleep(0.025)
            result = await client.post(f'{base}/browser/command', headers=headers, json={
                'lease_id': lease, 'expected_revision': old.headers['X-Browser-Revision'], 'command_id': str(uuid4()),
                'frame_id': old.headers['X-Browser-Frame-Id'], 'command': {'kind': 'click', 'x': 55, 'y': 70},
            })
            assert result.status_code == 409 and not visits['POST /expected'] and not visits['POST /unexpected']
            return {'old_revision_matches_shifted_page': old.headers['X-Browser-Revision'] == state['revision'],
                    'command_http_status': result.status_code,
                    'expected_post': visits['POST /expected'], 'unexpected_post': visits['POST /unexpected']}


async def click_proof_cost():
    samples = {'caller_owned_coordinates': [], 'viewport_frame_proof': []}
    with probe_website(3) as (site, _, _), TemporaryDirectory(prefix='thesus-audit-click-') as folder:
        async with workspace_app(live_settings(Path(folder), site), network=True) as (_, client):
            session = await create_session(client, 'Pointer timing probe')
            base = f'/sessions/{session["session_id"]}'
            headers = client_headers()
            lease = await take_control(client, base, headers)
            blank = await browser_state(client, base, headers)
            (await manual(client, base, headers, lease, blank['revision'], 'open', url=site + '/click-cost')).raise_for_status()
            for index in range(12):
                proof = index % 4 in (1, 2)
                frame = (await client.get(f'{base}/browser/frame', headers=headers)).raise_for_status()
                command = {'lease_id': lease, 'expected_revision': frame.headers['X-Browser-Revision'],
                           'command_id': str(uuid4()), 'command': {'kind': 'click', 'x': 50, 'y': 68}}
                if proof:
                    command['frame_id'] = frame.headers['X-Browser-Frame-Id']
                started = perf_counter()
                response = (await client.post(f'{base}/browser/command', headers=headers, json=command)).raise_for_status()
                assert response.json()['title'].endswith('.' * (index + 1))
                name = 'viewport_frame_proof' if proof else 'caller_owned_coordinates'
                samples[name].append((perf_counter() - started) * 1000)
    return {name: summary(elapsed) for name, elapsed in samples.items()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pages', type=int, default=190)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--passive-shift', action='store_true')
    mode.add_argument('--click-proof-cost', action='store_true')
    args = parser.parse_args()
    if args.pages < 6:
        parser.error('--pages must be at least 6')
    probe = passive_shift() if args.passive_shift else click_proof_cost() if args.click_proof_cost else long_run(args.pages)
    print(json.dumps(asyncio.run(probe), indent=2))
