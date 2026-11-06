"""Measure the real loopback browser collaboration path using a deterministic planner."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from tests.browser_harness import fixture_website, live_settings, workspace_app
from tests.conftest import create_session
from tests.test_browser_workspace import action, browser_state, client_headers, finish, manual, run_task, take_control, wait_state


def process_metrics():
    try:
        import psutil
    except ImportError:
        return None
    root = psutil.Process()
    totals = [0.0, 0]
    for process in [root, *root.children(recursive=True)]:
        try:
            usage = process.cpu_times()
            totals[0] += usage.user + usage.system
            totals[1] += process.memory_info().rss
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return totals


def summary(samples: list[float]) -> dict:
    if not samples:
        return {'n': 0}
    ordered = sorted(samples)
    return {'n': len(samples), 'median': round(statistics.median(samples), 2), 'max': round(ordered[-1], 2)}


async def measure(rounds: int, frames: int):
    results: dict[str, list[float]] = {key: [] for key in (
        'start_to_agent_observation_ms', 'takeover_ms', 'click_command_ms',
        'click_to_changed_frame_http_ms', 'typing_command_ms', 'release_to_completed_ms',
        'jpeg_http_ms', 'jpeg_bytes', 'serial_jpeg_requests_per_second',
        'sampled_process_tree_working_set_mib', 'process_tree_cpu_seconds_per_round',
    )}
    failures = []
    inputs_attempted = 0
    inputs_failed = 0
    handoffs = 0
    resumes = 0
    successes = 0

    with fixture_website() as (site, visits), TemporaryDirectory(prefix='thesus-bench-') as folder:
        for index in range(rounds):
            planner_observed = asyncio.Event()
            suspend = asyncio.Event()

            class Planner:
                async def decide(self, *, goal, observation, history):
                    if observation.url != site + '/':
                        return action('open', url=site + '/')
                    if f'REVIEWED-{index}' in observation.ax_tree:
                        assert 'Review count 1' in observation.ax_tree
                        return finish('Observed human update.')
                    planner_observed.set()
                    await suspend.wait()
                    return action('open', url=site + '/obsolete')

            settings = live_settings(Path(folder) / str(index), site)
            before_resources = process_metrics()
            async with workspace_app(settings, planner=Planner(), network=True) as (_, client):
                headers = client_headers()
                session = f"/sessions/{(await create_session(client))['session_id']}"
                frame_url = f'{session}/browser/frame'
                try:
                    started = perf_counter()
                    await run_task(client, session, f'Review the worksheet at {site}/')
                    await asyncio.wait_for(planner_observed.wait(), timeout=18)
                    results['start_to_agent_observation_ms'].append((perf_counter() - started) * 1_000)
                    started = perf_counter()
                    lease = await take_control(client, session, headers)
                    results['takeover_ms'].append((perf_counter() - started) * 1_000)
                    handoffs += 1

                    before_frame = (await client.get(frame_url, headers=headers)).raise_for_status()
                    before_revision = before_frame.headers['x-browser-revision']
                    started = perf_counter()
                    inputs_attempted += 1
                    click = await manual(client, session, headers, lease, before_revision, 'click', x=70, y=100)
                    inputs_failed += int(click.is_error)
                    click.raise_for_status()
                    results['click_command_ms'].append((perf_counter() - started) * 1_000)
                    changed = (await client.get(frame_url, headers=headers)).raise_for_status()
                    if changed.content == before_frame.content:
                        raise RuntimeError('Click did not produce a visibly changed JPEG')
                    results['click_to_changed_frame_http_ms'].append((perf_counter() - started) * 1_000)

                    state = await browser_state(client, session, headers)
                    started = perf_counter()
                    inputs_attempted += 1
                    typed = await manual(client, session, headers, lease, state['revision'], 'type', text=f'REVIEWED-{index}')
                    inputs_failed += int(typed.is_error)
                    typed.raise_for_status()
                    results['typing_command_ms'].append((perf_counter() - started) * 1_000)

                    capture_started = perf_counter()
                    for _ in range(frames):
                        started = perf_counter()
                        frame = (await client.get(frame_url, headers=headers)).raise_for_status()
                        results['jpeg_http_ms'].append((perf_counter() - started) * 1_000)
                        results['jpeg_bytes'].append(len(frame.content))
                    results['serial_jpeg_requests_per_second'].append(frames / (perf_counter() - capture_started))

                    started = perf_counter()
                    (await client.post(f'{session}/release', headers=headers, json={'lease_id': lease, 'resume': True})).raise_for_status()
                    suspend.set()
                    await wait_state(client, session, 'completed')
                    results['release_to_completed_ms'].append((perf_counter() - started) * 1_000)
                    resumes += 1
                    if visits['GET /obsolete']:
                        raise RuntimeError('An obsolete plan navigated after manual intervention')
                    successes += 1
                except Exception as exc:
                    failures.append(f'{type(exc).__name__}: {exc}')
                finally:
                    suspend.set()
                    after_resources = process_metrics()
                    if before_resources and after_resources:
                        results['sampled_process_tree_working_set_mib'].append(after_resources[1] / 1024 ** 2)
                        results['process_tree_cpu_seconds_per_round'].append(after_resources[0] - before_resources[0])

    return {
        'environment': {'python': platform.python_version(), 'os': platform.platform(), 'playwright': version('playwright')},
        'trials': rounds, 'task_successes': successes, 'takeovers': handoffs, 'resumptions': resumes,
        'manual_input_attempts': inputs_attempted, 'manual_input_failures': inputs_failed,
        'model_tokens': 0, 'failures': failures, 'metrics': {key: summary(value) for key, value in results.items()},
    }


if __name__ == '__main__':
    args_parser = argparse.ArgumentParser(description=__doc__)
    args_parser.add_argument('--rounds', type=int, default=5)
    args_parser.add_argument('--frames', type=int, default=10)
    args_parser.add_argument('--uncached', action='store_true', help='Measure the same workflow without frame reuse')
    args = args_parser.parse_args()
    if args.rounds < 1 or args.frames < 1:
        args_parser.error('rounds and frames must both be at least 1')
    if args.uncached:
        from app.integrations.browser import playwright_adapter

        playwright_adapter._FRAME_CACHE_S = 0
    result = asyncio.run(measure(args.rounds, args.frames))
    result['frame_cache_enabled'] = not args.uncached
    print(json.dumps(result, indent=2))
    raise SystemExit(bool(result['failures']))
