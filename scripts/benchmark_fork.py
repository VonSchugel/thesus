"""Measure a fork from an early checkpoint after seeding a later history tail."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch

from sqlalchemy import insert

from app.db.models import EventORM, MessageORM
from tests.conftest import create_session, run_test_app


def materialize_full_history(repository):
    ordered = repository.list_all_ordered

    async def read(db, *, session_id, branch_id, prefix_limit=None):
        rows = await ordered(db, session_id=session_id, branch_id=branch_id)
        return rows[:prefix_limit]

    return patch.object(repository, 'list_all_ordered', read)


async def benchmark(tail_rows: int, repeats: int, *, full_scan: bool) -> dict:
    durations = []
    with TemporaryDirectory(prefix='fork-benchmark-') as directory:
        async with run_test_app(Path(directory)) as (app, client, _, _):
            session = await create_session(client)
            session_id, branch_id = session['session_id'], session['selected_branch_id']
            url = f'/sessions/{session_id}'
            (await client.post(f'{url}/messages', json={'content': 'First instruction', 'trigger_agents': False})).raise_for_status()
            (await client.post(f'{url}/snapshot', json={})).raise_for_status()
            checkpoint = (await client.get(f'{url}/checkpoints')).raise_for_status().json()[0]
            # Give all synthetic rows a later timestamp so they lie beyond the checkpoint.
            tail_start = datetime.now(timezone.utc) + timedelta(seconds=1)
            async with app.state.session_factory() as db:
                for start in range(0, tail_rows, 1000):
                    rows = range(start, min(start + 1000, tail_rows))
                    await db.execute(insert(MessageORM), [
                        {'id': f'benchmark-message-{index}', 'session_id': session_id, 'branch_id': branch_id,
                         'role': 'user', 'actor': 'user', 'content': 'Synthetic tail row',
                         'created_at': tail_start + timedelta(microseconds=index)} for index in rows
                    ])
                    await db.execute(insert(EventORM), [
                        {'id': f'benchmark-event-{index}', 'session_id': session_id, 'branch_id': branch_id,
                         'step_index': index + 1, 'actor': 'system', 'action_type': 'note',
                         'action_payload': {'index': index},
                         'created_at': tail_start + timedelta(microseconds=index)} for index in rows
                    ])
                await db.commit()
            with ExitStack() as stack:
                if full_scan:
                    repositories = app.state.services.repos
                    stack.enter_context(materialize_full_history(repositories.messages))
                    stack.enter_context(materialize_full_history(repositories.events))
                for trial in range(repeats):
                    started = perf_counter()
                    result = (await client.post(f'{url}/branch', json={
                        'source_branch_id': branch_id, 'checkpoint_id': checkpoint['checkpoint_id'],
                    })).raise_for_status().json()
                    durations.append((perf_counter() - started) * 1000)
                    messages = (await client.get(f'{url}/messages', params={
                        'branch_id': result['branch_id'],
                    })).raise_for_status().json()
                    assert [message['content'] for message in messages] == ['First instruction'], trial
    return {'mode': 'full_scan' if full_scan else 'sql_prefix', 'tail_rows_per_table': tail_rows, 'repeats': repeats,
            'duration_ms': [round(duration, 2) for duration in durations], 'median_ms': round(median(durations), 2)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tail-rows', type=int, default=20_000)
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--full-scan', action='store_true', help='Recreate the prior read-all-then-slice path')
    options = parser.parse_args()
    if options.tail_rows < 0 or options.repeats < 1:
        parser.error('Expected a nonnegative tail size and at least one repeat')
    print(json.dumps(asyncio.run(benchmark(options.tail_rows, options.repeats, full_scan=options.full_scan))))
