import pytest

from tests.conftest import create_session, wait_idle


@pytest.mark.asyncio
async def test_branching_from_checkpoint_produces_independent_continuation(app_factory):
    async with app_factory(step_delay_s=0.01) as (app, client, _, _):
        session = await create_session(client)
        sid = session['session_id']
        source_bid = session['selected_branch_id']
        await client.post(f'/sessions/{sid}/messages', json={'content': 'Find 3 laptop deals under $700 and compare them.', 'branch_id': source_bid, 'trigger_agents': True})
        await wait_idle(app)
        checkpoints = (await client.get(f'/sessions/{sid}/checkpoints', params={'branch_id': source_bid})).json()
        assert checkpoints
        checkpoint_id = checkpoints[-1]['checkpoint_id']
        source_messages_before = (await client.get(f'/sessions/{sid}/messages', params={'branch_id': source_bid})).json()
        branch = (await client.post(f'/sessions/{sid}/branch', json={'source_branch_id': source_bid, 'checkpoint_id': checkpoint_id, 'name': 'alt'})).json()
        child_bid = branch['branch_id']
        await client.post(f'/sessions/{sid}/messages', json={'content': 'Use gaming laptops only', 'branch_id': child_bid, 'trigger_agents': False})
        child_messages = (await client.get(f'/sessions/{sid}/messages', params={'branch_id': child_bid})).json()
        source_messages_after = (await client.get(f'/sessions/{sid}/messages', params={'branch_id': source_bid})).json()
        assert any(m['content'] == 'Use gaming laptops only' for m in child_messages)
        assert not any(m['content'] == 'Use gaming laptops only' for m in source_messages_after)
        assert len(source_messages_after) == len(source_messages_before)

