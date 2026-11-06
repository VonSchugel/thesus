import pytest

from tests.conftest import create_session


@pytest.mark.asyncio
async def test_persistence_of_chat_history_across_restart(app_factory, tmp_path):
    async with app_factory() as (_, client, _, _):
        session = await create_session(client)
        sid = session['session_id']
        bid = session['selected_branch_id']
        r = await client.post(f'/sessions/{sid}/messages', json={'content': 'hello', 'branch_id': bid, 'trigger_agents': False})
        assert r.status_code == 200

    async with app_factory() as (_, client2, _, _):
        r2 = await client2.get(f'/sessions/{sid}/messages', params={'branch_id': bid})
        assert r2.status_code == 200
        msgs = r2.json()
        assert any(m['content'] == 'hello' and m['role'] == 'user' for m in msgs)

