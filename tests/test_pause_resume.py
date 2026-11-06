import asyncio

import pytest

from tests.conftest import create_session, wait_idle


@pytest.mark.asyncio
async def test_pause_resume_flow(app_factory):
    async with app_factory(step_delay_s=0.12, action_rate_limit_ms=0) as (app, client, _, _):
        session = await create_session(client)
        sid = session['session_id']
        bid = session['selected_branch_id']
        await client.post(f'/sessions/{sid}/messages', json={'content': 'Find 3 laptop deals under $700 and compare them.', 'branch_id': bid, 'trigger_agents': True})
        await asyncio.sleep(0.03)
        pause = await client.post(f'/sessions/{sid}/pause', params={'branch_id': bid})
        assert pause.status_code == 200
        await wait_idle(app)
        state = (await client.get(f'/sessions/{sid}', params={'branch_id': bid})).json()['state']
        assert state['run_status'] == 'paused'
        resume = await client.post(f'/sessions/{sid}/resume', params={'branch_id': bid})
        assert resume.status_code == 200
        await wait_idle(app)
        state2 = (await client.get(f'/sessions/{sid}', params={'branch_id': bid})).json()['state']
        assert state2['run_status'] in {'completed', 'paused'}

