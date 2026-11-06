import pytest

from tests.conftest import create_session, wait_idle


@pytest.mark.asyncio
async def test_replay_timeline_reconstructs_messages_and_events(app_factory):
    async with app_factory(step_delay_s=0.01) as (app, client, _, _):
        session = await create_session(client)
        sid = session['session_id']
        bid = session['selected_branch_id']
        await client.post(f'/sessions/{sid}/messages', json={'content': 'Find 3 laptop deals under $700 and compare them.', 'branch_id': bid, 'trigger_agents': True})
        await wait_idle(app)
        replay = (await client.get(f'/sessions/{sid}/replay', params={'branch_id': bid})).json()
        kinds = {item['kind'] for item in replay['timeline']}
        assert 'message' in kinds
        assert 'event' in kinds
        assert 'checkpoint' in kinds

