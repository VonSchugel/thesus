import pytest

from tests.conftest import create_session, wait_idle


@pytest.mark.asyncio
async def test_approval_gating_for_sensitive_action(app_factory):
    async with app_factory(step_delay_s=0.01, action_rate_limit_ms=0) as (app, client, fake, _):
        session = await create_session(client)
        sid = session['session_id']
        bid = session['selected_branch_id']
        await client.post(f'/sessions/{sid}/messages', json={'content': 'Buy a laptop and purchase it for me', 'branch_id': bid, 'trigger_agents': True})
        await wait_idle(app)
        s = (await client.get(f'/sessions/{sid}', params={'branch_id': bid})).json()
        assert s['state']['run_status'] == 'awaiting_approval'
        corr = s['state']['approval_correlation_id']
        assert corr
        executed_before = fake.get_executed_actions(sid, bid)
        assert all(a['action']['payload'].get('target') != 'submit' for a in executed_before)

        ap = await client.post(f'/sessions/{sid}/approve', json={'branch_id': bid, 'correlation_id': corr, 'note': 'ok'})
        assert ap.status_code == 200
        rs = await client.post(f'/sessions/{sid}/resume', params={'branch_id': bid})
        assert rs.status_code == 200
        await wait_idle(app)

        events = (await client.get(f'/sessions/{sid}/events', params={'branch_id': bid})).json()
        assert any(e['action_type'] == 'click' for e in events)
        assert any(e['action_type'] == 'approve' for e in events)

