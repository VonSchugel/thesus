import asyncio

import pytest

from tests.conftest import create_session, wait_idle


@pytest.mark.asyncio
async def test_manual_takeover_lock_prevents_agent_actions(app_factory):
    async with app_factory(step_delay_s=0.01) as (app, client, fake, _):
        session = await create_session(client)
        sid = session['session_id']
        bid = session['selected_branch_id']
        take = await client.post(f'/sessions/{sid}/takeover', params={'branch_id': bid})
        assert take.status_code == 200
        await client.post(f'/sessions/{sid}/messages', json={'content': 'Find laptop deals', 'branch_id': bid, 'trigger_agents': True})
        await asyncio.sleep(0.1)
        await wait_idle(app)
        assert fake.get_executed_actions(sid, bid) == []
        resume = await client.post(f'/sessions/{sid}/resume', params={'branch_id': bid})
        assert resume.status_code == 409

