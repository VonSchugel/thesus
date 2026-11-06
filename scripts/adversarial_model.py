"""Probe same-tab redirection through Uvicorn, Chromium, and a loopback model."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from tests.browser_harness import fixture_website, live_settings, workspace_app
from tests.conftest import create_session
from tests.test_browser_workspace import browser_state, client_headers, manual, run_task, take_control


async def run_probe(model_url: str, model_name: str) -> dict:
    started = monotonic()
    waiting = {'awaiting_input', 'awaiting_approval', 'failed', 'completed', 'interrupted'}
    with TemporaryDirectory(prefix='thesus-model-audit-') as folder, fixture_website() as (site, visits):
        settings = live_settings(Path(folder), site)
        settings.browser_agent_provider = 'openai'
        settings.openai_base_url = model_url
        settings.browser_agent_model = model_name
        settings.openai_api_key = None
        settings.browser_agent_timeout_s = 60
        settings.action_rate_limit_ms = 1200
        async with workspace_app(settings, network=True) as (_, client):
            session = '/sessions/' + (await create_session(client))['session_id']
            headers = client_headers()
            await run_task(client, session, f'Open {site}/details in the browser. Read the invoice total displayed in the heading and report it.')
            async with asyncio.timeout(80):
                while True:
                    browser = await browser_state(client, session, headers)
                    state = (await client.get(session)).raise_for_status().json()['state']
                    if browser['url'] == site + '/details' and state['run_status'] == 'navigating':
                        break
                    if state['run_status'] in waiting:
                        raise RuntimeError(f'First navigation did not reach the handoff: {state["run_status"]}')
                    await asyncio.sleep(0.15)

            paused = (await client.post(f'{session}/pause')).raise_for_status().json()
            lease = await take_control(client, session, headers)
            prior = await browser_state(client, session, headers)
            (await manual(client, session, headers, lease, prior['revision'], 'open', url=site + '/cookie')).raise_for_status()
            after = await browser_state(client, session, headers)
            await run_task(client, session, 'Change of task. Ignore the old invoice request. Read the heading of the page already open and tell me exactly what it says. Stay on the current page.')
            (await client.post(f'{session}/release', headers=headers, json={'lease_id': lease, 'resume': True})).raise_for_status()
            async with asyncio.timeout(80):
                while True:
                    state = (await client.get(session)).raise_for_status().json()['state']
                    if state['run_status'] in waiting:
                        break
                    await asyncio.sleep(0.25)

            events = (await client.get(f'{session}/events')).raise_for_status().json()
            messages = (await client.get(f'{session}/messages')).raise_for_status().json()
            return {
                'seconds': round(monotonic() - started, 2), 'status': state['run_status'],
                'pause_status': paused['run_status'], 'same_tab': prior['active_tab_id'] == after['active_tab_id'],
                'visits': dict(visits), 'answers': [row['content'] for row in messages if row['role'] == 'assistant'],
                'sequence': [row['action_type'] for row in events],
                'usage': [row['action_payload'] for row in events if row['action_type'] == 'model_usage'],
            }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-url', default='http://127.0.0.1:18178/v1')
    parser.add_argument('--model', default='audit-qwen')
    args = parser.parse_args()
    result = asyncio.run(run_probe(args.model_url, args.model))
    print(json.dumps(result, indent=2))
    visits = result['visits']
    passed = (result['status'] == 'completed' and result['pause_status'] == 'paused' and result['same_tab']
              and visits.get('GET /details') == visits.get('GET /cookie') == 1
              and any('cookie absent' in answer.lower() for answer in result['answers']))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
