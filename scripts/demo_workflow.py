from __future__ import annotations

import asyncio
import json

import httpx

BASE_URL = 'http://127.0.0.1:8000'


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        session = (await client.post('/sessions', json={'title': 'Laptop deals demo'})).json()
        session_id = session['session_id']
        branch_id = session['selected_branch_id']
        print('1) Created session', session_id, 'branch', branch_id)

        msg = {
            'content': 'Find 3 laptop deals under $700 and compare them.',
            'branch_id': branch_id,
            'trigger_agents': True,
        }
        print('2) Posting user message...')
        print((await client.post(f'/sessions/{session_id}/messages', json=msg)).json())

        await asyncio.sleep(1.0)
        print('3) Pause')
        print((await client.post(f'/sessions/{session_id}/pause', params={'branch_id': branch_id})).json())

        print('4) Inject correction while paused')
        correction = {'content': 'Prefer refurbished options if warranty is included.', 'branch_id': branch_id, 'trigger_agents': False}
        print((await client.post(f'/sessions/{session_id}/messages', json=correction)).json())

        print('5) Resume')
        print((await client.post(f'/sessions/{session_id}/resume', params={'branch_id': branch_id})).json())

        await asyncio.sleep(1.5)
        print('6) Read session state')
        state = (await client.get(f'/sessions/{session_id}', params={'branch_id': branch_id})).json()
        print(json.dumps(state, indent=2)[:2000])

        print('7) Fetch replay')
        replay = (await client.get(f'/sessions/{session_id}/replay', params={'branch_id': branch_id})).json()
        print('Timeline items:', len(replay.get('timeline', [])))


if __name__ == '__main__':
    asyncio.run(main())
