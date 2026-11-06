import pytest

from tests.conftest import create_session, wait_idle


@pytest.mark.asyncio
async def test_persistence_of_event_log_across_restart(app_factory):
    async with app_factory(step_delay_s=0.01) as (app, client, _, _):
        session = await create_session(client)
        sid = session['session_id']
        bid = session['selected_branch_id']
        r = await client.post(f'/sessions/{sid}/messages', json={'content': 'Find 3 laptop deals under $700 and compare them.', 'branch_id': bid, 'trigger_agents': True})
        assert r.status_code == 200
        await wait_idle(app)
        r_events = await client.get(f'/sessions/{sid}/events', params={'branch_id': bid})
        assert r_events.status_code == 200
        assert len(r_events.json()) > 0

    async with app_factory(step_delay_s=0.01) as (_, client2, _, _):
        r2 = await client2.get(f'/sessions/{sid}/events', params={'branch_id': bid})
        events = r2.json()
        assert any(e['actor'] == 'navigator_agent' for e in events)

