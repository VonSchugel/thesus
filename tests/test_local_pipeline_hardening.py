import asyncio
import json
import sqlite3
import time

import pytest

from app.db.models import EventORM, MessageORM
from app.integrations.local_llm import LocalLLMProvider
from tests.conftest import create_session, wait_idle


class ControlledProvider:
    model = 'fixture-local-model'

    def __init__(self, *, error=False):
        self.calls = []
        self.error = error

    async def enrich(self, *, items, request):
        release = asyncio.get_running_loop().create_future()
        self.calls.append({'request': request, 'items': items, 'release': release})
        await release
        if self.error:
            raise RuntimeError('fixture service failure, never canonical output')
        return {'status': 'generated', 'classification': 'GENERATED_ADVISORY_UNVERIFIED',
                'model': self.model, 'summary': 'advisory for ' + request, 'source_ids': [1]}

    def release(self, index):
        future = self.calls[index]['release']
        if not future.done():
            future.set_result(None)


async def wait_calls(provider, count):
    for _ in range(200):
        if len(provider.calls) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError('Optional enrichment was not scheduled')


async def submit(app, client, session, content):
    response = await client.post(f"/sessions/{session['session_id']}/messages", json={
        'branch_id': session['selected_branch_id'], 'content': content, 'trigger_agents': True})
    response.raise_for_status()
    await wait_idle(app)


async def messages(client, session):
    return (await client.get(f"/sessions/{session['session_id']}/messages",
            params={'branch_id': session['selected_branch_id']})).json()


def install(app, settings, provider):
    settings.local_llm_enrichment_enabled = True
    app.state.services.local_llm = provider


@pytest.mark.asyncio
async def test_canonical_commits_and_other_pipeline_writes_while_inference_exceeds_db_timeout(app_factory):
    async with app_factory(step_delay_s=0) as (app, client, _, settings):
        provider = ControlledProvider()
        install(app, settings, provider)
        session = await create_session(client)
        await submit(app, client, session, 'Find laptop deals and compare')
        await wait_calls(provider, 1)
        before = [item for item in await messages(client, session) if item['role'] == 'assistant'][-1]
        assert before['content_json']['local_llm']['status'] == 'pending'
        assert (await client.get(f"/sessions/{session['session_id']}")).json()['state']['run_status'] == 'completed'
        started = time.perf_counter()
        await create_session(client, 'Independent pipeline')
        assert time.perf_counter() - started < 1
        # SQLite normally gives competing writers five seconds. Keep inference
        # pending past that boundary and confirm no connection retains its lock.
        await asyncio.sleep(5.1)
        started = time.perf_counter()
        await create_session(client, 'Another independent pipeline')
        assert time.perf_counter() - started < 1
        assert not provider.calls[0]['release'].done()
        provider.release(0)
        await app.state.services.wait_for_enrichment(timeout=2)
        after = [item for item in await messages(client, session) if item['id'] == before['id']][0]
        assert after['content'] == before['content']
        assert after['content_json']['items'] == before['content_json']['items']
        assert after['content_json']['local_llm']['status'] == 'generated'


@pytest.mark.asyncio
async def test_late_advice_updates_only_original_turn_and_event(app_factory):
    async with app_factory(step_delay_s=0) as (app, client, _, settings):
        provider = ControlledProvider()
        install(app, settings, provider)
        session = await create_session(client)
        await submit(app, client, session, 'First fixture request')
        await wait_calls(provider, 1)
        first = [item for item in await messages(client, session) if item['role'] == 'assistant'][-1]
        await submit(app, client, session, 'Second fixture request')
        await wait_calls(provider, 2)
        second = [item for item in await messages(client, session) if item['role'] == 'assistant'][-1]
        assert first['id'] != second['id']
        provider.release(0)
        for _ in range(100):
            rows = await messages(client, session)
            original = next(item for item in rows if item['id'] == first['id'])
            if original['content_json']['local_llm']['status'] == 'generated':
                break
            await asyncio.sleep(0.01)
        latest = next(item for item in rows if item['id'] == second['id'])
        assert original['content_json']['local_llm']['summary'] == 'advisory for First fixture request'
        assert latest['content_json']['local_llm']['status'] == 'pending'
        assert latest['content'] == second['content']
        provider.release(1)
        await app.state.services.wait_for_enrichment(timeout=2)
        rows = await messages(client, session)
        latest = next(item for item in rows if item['id'] == second['id'])
        assert latest['content_json']['local_llm']['summary'] == 'advisory for Second fixture request'
        events = (await client.get(f"/sessions/{session['session_id']}/events")).json()
        outcomes = [item['action_payload']['local_llm'] for item in events if item['actor'] == 'extractor_agent']
        assert {item['summary'] for item in outcomes} == {'advisory for First fixture request', 'advisory for Second fixture request'}


@pytest.mark.asyncio
async def test_unexpected_provider_failure_cannot_abort_completed_canonical_result(app_factory):
    async with app_factory(step_delay_s=0) as (app, client, _, settings):
        provider = ControlledProvider(error=True)
        install(app, settings, provider)
        session = await create_session(client)
        await submit(app, client, session, 'Find fixture sources')
        await wait_calls(provider, 1)
        before = [item for item in await messages(client, session) if item['role'] == 'assistant'][-1]
        provider.release(0)
        await app.state.services.wait_for_enrichment(timeout=2)
        after = [item for item in await messages(client, session) if item['id'] == before['id']][0]
        assert after['content'] == before['content']
        assert after['content_json']['items'] == before['content_json']['items']
        assert after['content_json']['local_llm']['reason'] == 'unexpected_provider_error'
        assert (await client.get(f"/sessions/{session['session_id']}")).json()['state']['run_status'] == 'completed'


@pytest.mark.asyncio
async def test_task_budget_skips_extra_advice_without_blocking_canonical_pipeline(app_factory):
    async with app_factory(step_delay_s=0) as (app, client, _, settings):
        provider = ControlledProvider()
        install(app, settings, provider)
        sessions = []
        for index in range(5):
            session = await create_session(client)
            sessions.append(session)
            await submit(app, client, session, 'Fixture request ' + str(index))
        assert len(app.state.services._enrichment_tasks) <= 4
        assert len(provider.calls) == 4
        last = [item for item in await messages(client, sessions[-1]) if item['role'] == 'assistant'][-1]
        assert last['content_json']['local_llm']['status'] == 'skipped'
        assert last['content_json']['local_llm']['reason'] == 'task_budget_exhausted'
        for index in range(4):
            provider.release(index)
        await app.state.services.wait_for_enrichment(timeout=2)


@pytest.mark.asyncio
async def test_shutdown_records_cancelled_advice_and_preserves_committed_answer(app_factory):
    async with app_factory(step_delay_s=0) as (app, client, _, settings):
        provider = ControlledProvider()
        install(app, settings, provider)
        session = await create_session(client)
        await submit(app, client, session, 'Fixture request')
        await wait_calls(provider, 1)
        before = [item for item in await messages(client, session) if item['role'] == 'assistant'][-1]
        database = settings.data_dir.parent / 'test.db'
    with sqlite3.connect(database) as connection:
        text, payload = connection.execute('SELECT content,content_json FROM messages WHERE id=?', (before['id'],)).fetchone()
    assert text == before['content']
    assert json.loads(payload)['local_llm']['reason'] == 'enrichment_cancelled'


def canonical_database_snapshot(database):
    with sqlite3.connect(database) as connection:
        messages = connection.execute('SELECT id,content,content_json FROM messages ORDER BY id').fetchall()
        events = connection.execute('SELECT id,action_payload FROM events ORDER BY id').fetchall()
        states = connection.execute('SELECT * FROM session_runtime_state ORDER BY session_id,branch_id').fetchall()
    def canonical(encoded):
        payload = json.loads(encoded) if encoded else None
        if isinstance(payload, dict):
            payload.pop('local_llm', None)
        return payload
    return {'messages': [(row[0], row[1], canonical(row[2])) for row in messages],
            'events': [(row[0], canonical(row[1])) for row in events], 'states': states}


@pytest.mark.asyncio
async def test_startup_reconciles_interrupted_advice_preserving_sources_finished_results_and_new_turns(app_factory, monkeypatch):
    async def forbidden_inference(*args, **kwargs):
        raise AssertionError('Startup recovery must not replay inference')
    monkeypatch.setattr(LocalLLMProvider, 'enrich', forbidden_inference)
    async with app_factory(step_delay_s=0) as (app, client, _, settings):
        session = await create_session(client)
        await submit(app, client, session, 'Original interrupted fixture request')
        first_rows = await messages(client, session)
        pending_ids = [item['id'] for item in first_rows if item['role'] in ('assistant', 'extractor_agent')]
        source_id = next(item['id'] for item in first_rows if item['role'] == 'user')
        first_events = (await client.get(f"/sessions/{session['session_id']}/events")).json()
        pending_event_id = next(item['event_id'] for item in first_events if item['actor'] == 'extractor_agent')
        await submit(app, client, session, 'Already finished fixture request')
        all_rows = await messages(client, session)
        finished_ids = [item['id'] for item in all_rows if item['role'] in ('assistant', 'extractor_agent') and item['id'] not in pending_ids]
        all_events = (await client.get(f"/sessions/{session['session_id']}/events")).json()
        finished_event_id = next(item['event_id'] for item in reversed(all_events) if item['actor'] == 'extractor_agent')
        pending = {'status': 'pending', 'model': 'original-fixture-model', 'source_user_message_id': source_id}
        finished = {'status': 'generated', 'model': 'finished-fixture-model', 'summary': 'Finished advisory must remain unchanged',
                    'classification': 'GENERATED_ADVISORY_UNVERIFIED', 'source_user_message_id': 'finished-source-fixture'}
        async with app.state.session_factory() as db:
            for model, ids, field, metadata in ((MessageORM, pending_ids, 'content_json', pending),
                    (MessageORM, finished_ids, 'content_json', finished),
                    (EventORM, [pending_event_id], 'action_payload', pending),
                    (EventORM, [finished_event_id], 'action_payload', finished)):
                for row_id in ids:
                    row = await db.get(model, row_id)
                    payload = dict(getattr(row, field))
                    payload['local_llm'] = metadata
                    setattr(row, field, payload)
            await db.commit()
        database = settings.data_dir.parent / 'test.db'
        before = canonical_database_snapshot(database)
    # A new app instance reconciles persisted metadata before yielding lifespan.
    async with app_factory(step_delay_s=0) as (app, client, _, settings):
        assert canonical_database_snapshot(database) == before
        rows = await messages(client, session)
        for row_id in pending_ids:
            outcome = next(item for item in rows if item['id'] == row_id)['content_json']['local_llm']
            assert outcome == {**pending, 'status': 'fallback', 'reason': 'enrichment_interrupted'}
            assert 'summary' not in outcome
        for row_id in finished_ids:
            assert next(item for item in rows if item['id'] == row_id)['content_json']['local_llm'] == finished
        events = (await client.get(f"/sessions/{session['session_id']}/events")).json()
        assert next(item for item in events if item['event_id'] == pending_event_id)['action_payload']['local_llm'] == {**pending, 'status': 'fallback', 'reason': 'enrichment_interrupted'}
        assert next(item for item in events if item['event_id'] == finished_event_id)['action_payload']['local_llm'] == finished
        provider = ControlledProvider()
        install(app, settings, provider)
        await submit(app, client, session, 'New request after reconciliation')
        await wait_calls(provider, 1)
        provider.release(0)
        await app.state.services.wait_for_enrichment(timeout=2)
        rows = await messages(client, session)
        newest = [item for item in rows if item['role'] == 'assistant'][-1]
        assert newest['content_json']['local_llm']['status'] == 'generated'
        assert newest['content_json']['local_llm']['summary'] == 'advisory for New request after reconciliation'
        assert next(item for item in rows if item['id'] == pending_ids[0])['content_json']['local_llm']['reason'] == 'enrichment_interrupted'


@pytest.mark.asyncio
async def test_advisory_persistence_failure_is_bounded_logged_and_reconciled_next_start(app_factory, monkeypatch):
    async with app_factory(step_delay_s=0) as (app, client, _, settings):
        provider = ControlledProvider()
        install(app, settings, provider)
        session = await create_session(client)
        await submit(app, client, session, 'Fixture with unavailable advisory persistence')
        await wait_calls(provider, 1)
        before = [item for item in await messages(client, session) if item['role'] == 'assistant'][-1]
        original_factory = app.state.services.session_factory
        logged = []
        class UnavailableDatabase:
            async def __aenter__(self):
                raise OSError('Fixture database unavailable')
            async def __aexit__(self, *args):
                pass
        monkeypatch.setattr(app.state.services, 'session_factory', lambda: UnavailableDatabase())
        monkeypatch.setattr(app.state.services.log, 'exception', lambda message, *args, **kwargs: logged.append(message))
        started = time.perf_counter()
        provider.release(0)
        await app.state.services.wait_for_enrichment(timeout=1)
        assert time.perf_counter() - started < 1
        monkeypatch.setattr(app.state.services, 'session_factory', original_factory)
        after = next(item for item in await messages(client, session) if item['id'] == before['id'])
        assert after['content'] == before['content'] and after['content_json'] == before['content_json']
        assert after['content_json']['local_llm']['status'] == 'pending'
        assert logged == ['Unable to persist optional enrichment']
        assert (await client.get(f"/sessions/{session['session_id']}")).json()['state']['run_status'] == 'completed'
    async with app_factory(step_delay_s=0) as (_, client, _, _):
        restored = next(item for item in await messages(client, session) if item['id'] == before['id'])
        assert restored['content'] == before['content']
        assert restored['content_json']['items'] == before['content_json']['items']
        assert restored['content_json']['local_llm']['reason'] == 'enrichment_interrupted'
