from __future__ import annotations

import asyncio
import json
from uuid import uuid4

from app.agents.browser_planner import BrowserPlanner
from tests.browser_harness import fixture_website, live_settings, workspace_app
from tests.test_browser_workspace import action, finish, target, wait_state


PROFILES = {
    'fast': {'provider': 'ollama', 'model': 'qwen3:0.6b', 'label': 'Fast local'},
    'deep': {'provider': 'llama_cpp', 'model': 'granite-local', 'label': 'Deep local',
             'base_url': 'http://127.0.0.1:18177/v1'},
}


def configure_models(settings, *, parallelism: int):
    settings.browser_local_model_profiles = {key: dict(profile) for key, profile in PROFILES.items()}
    settings.browser_model_parallelism = parallelism
    settings.ollama_host = 'http://127.0.0.1:11434'
    settings.openai_api_key = 'test-hosted-key'
    return settings


def local_reply(url, *, decision):
    result = json.dumps(decision)
    if url.endswith('/api/chat'):
        return {'message': {'content': result}, 'prompt_eval_count': 9, 'eval_count': 3}
    return {'choices': [{'message': {'content': result}}],
            'usage': {'prompt_tokens': 9, 'completion_tokens': 3}}


def navigation(url):
    return {'kind': 'command', 'summary': 'Open the requested page', 'command': {'kind': 'open', 'url': url}}


async def test_group_waits_for_both_model_workers_and_keeps_browser_profiles_isolated(tmp_path, monkeypatch):
    both_inference_requests_started = asyncio.Event()
    allow_first_decisions = asyncio.Event()
    worksheet_observed = asyncio.Event()
    started: set[str] = set()

    with fixture_website() as (site, visits):
        worksheet = f'{site}/'
        cookie_page = f'{site}/cookie'

        async def model_response(client, url, body, *, headers=None):
            model = body['model']
            assert model in {'qwen3:0.6b', 'granite-local'}
            assert headers is None or 'authorization' not in {key.lower() for key in headers}
            observed = json.loads(body['messages'][-1]['content'])
            is_worksheet = model == 'qwen3:0.6b'
            destination = worksheet if is_worksheet else cookie_page
            expected_endpoint = ('http://127.0.0.1:11434/api/chat' if is_worksheet else
                                 'http://127.0.0.1:18177/v1/chat/completions')
            assert url == expected_endpoint
            assert destination in observed['task']
            page = observed['untrusted_page']
            if model not in started:
                assert page['url'] == 'about:blank'
                started.add(model)
                if len(started) == 2:
                    both_inference_requests_started.set()
                await allow_first_decisions.wait()
                if not is_worksheet:
                    await worksheet_observed.wait()
                return local_reply(url, decision=navigation(destination))

            assert page['url'] == destination
            if is_worksheet:
                assert 'Review worksheet' in page['ax_tree']
                worksheet_observed.set()
                answer = 'Worksheet task finished.'
            else:
                assert 'Cookie absent' in page['ax_tree']
                answer = 'Cookie isolation verified.'
            return local_reply(url, decision={'kind': 'finish', 'summary': answer})

        monkeypatch.setattr(BrowserPlanner, '_request', staticmethod(model_response))
        settings = configure_models(live_settings(tmp_path, site), parallelism=2)
        async with workspace_app(settings) as (_, client):
            models = (await client.get('/workspace/models')).raise_for_status().json()
            assert models['inference_parallelism'] == 2
            assert {model['id'] for model in models['profiles']} == {'fast', 'deep'}
            assert 'test-hosted-key' not in json.dumps(models)
            assert ':18177' not in json.dumps(models)

            response = await client.post('/task-groups', json={
                'title': 'Independent pages',
                'tasks': [
                    {'title': 'Worksheet', 'goal': f'Inspect {worksheet}', 'model_profile': 'fast'},
                    {'title': 'Cookie check', 'goal': f'Inspect {cookie_page}', 'model_profile': 'deep'},
                ],
            })
            group = response.raise_for_status().json()
            assert len({(task['session_id'], task['branch_id']) for task in group['tasks']}) == 2
            path = f"/task-groups/{group['group_id']}"
            joined = asyncio.create_task(client.get(path, params={'wait_for': 'settled', 'wait_seconds': 12}))
            try:
                async with asyncio.timeout(15):
                    await both_inference_requests_started.wait()
                assert not joined.done(), 'The settled join returned while both tasks were still in inference'
                allow_first_decisions.set()
                settled = (await joined).raise_for_status().json()
            finally:
                if not joined.done():
                    joined.cancel()
                    await asyncio.gather(joined, return_exceptions=True)
            assert not settled['timed_out']
            assert settled['counts']['terminal'] == 2
            assert settled['counts']['running'] == 0
            tasks = {task['title']: task for task in settled['tasks']}
            assert tasks['Worksheet']['model_profile'] == 'fast'
            assert tasks['Worksheet']['model_name'] == 'qwen3:0.6b'
            assert tasks['Worksheet']['run_status'] == 'completed'
            assert tasks['Worksheet']['answer'] == 'Worksheet task finished.'
            assert tasks['Cookie check']['model_profile'] == 'deep'
            assert tasks['Cookie check']['model_name'] == 'granite-local'
            assert tasks['Cookie check']['run_status'] == 'completed'
            assert tasks['Cookie check']['answer'] == 'Cookie isolation verified.'
            assert visits['GET /cookie'] == 1


async def test_group_create_retry_preserves_task_ids_and_completed_answers_after_restart(tmp_path):
    class Planner:
        async def decide(self, *, goal, observation, history):
            return finish(f'Finished {goal}')

    with fixture_website() as (site, _):
        settings = live_settings(tmp_path, site)
        request = {'title': 'Retry safely', 'request_id': str(uuid4()),
                   'tasks': [{'goal': 'first'}, {'goal': 'second'}]}
        async with workspace_app(settings, planner=Planner()) as (_, client):
            replies = await asyncio.gather(*(client.post('/task-groups', json=request) for _ in range(2)))
            original, retry = [response.raise_for_status().json() for response in replies]
            assert original['group_id'] == retry['group_id']
            assert [task['session_id'] for task in original['tasks']] == [task['session_id'] for task in retry['tasks']]
            conflict = await client.post('/task-groups', json={**request, 'title': 'Different request'})
            assert conflict.status_code == 409
            path = f"/task-groups/{original['group_id']}"
            completed = (await client.post('/task-groups', params={'wait_for': 'settled', 'wait_seconds': 15},
                                           json=request)).raise_for_status().json()
            assert completed['group_id'] == original['group_id']
            assert completed['counts']['terminal'] == 2
            assert not completed['timed_out']

        async with workspace_app(settings) as (_, client):
            recovered = (await client.get(path)).raise_for_status().json()
            assert [task['session_id'] for task in recovered['tasks']] == [task['session_id'] for task in original['tasks']]
            assert [task['answer'] for task in recovered['tasks']] == ['Finished first', 'Finished second']
            assert all(task['run_status'] == 'completed' for task in recovered['tasks'])


async def test_session_model_choice_is_validated_and_isolated_while_other_session_pauses(tmp_path, monkeypatch):
    inference_in_progress = asyncio.Event()
    allow_first_decision = asyncio.Event()
    model_requests = []

    async def model_response(client, url, body, *, headers=None):
        model = body['model']
        model_requests.append(model)
        if model == 'qwen3:0.6b':
            inference_in_progress.set()
            await allow_first_decision.wait()
        return local_reply(url, decision={'kind': 'finish', 'summary': f'Processed by {model}.'})

    monkeypatch.setattr(BrowserPlanner, '_request', staticmethod(model_response))
    with fixture_website() as (site, _):
        settings = configure_models(live_settings(tmp_path, site), parallelism=1)
        async with workspace_app(settings) as (_, client):
            rejected = await client.post('/sessions', json={'title': 'Unknown model', 'model_profile': 'unauthorized-model'})
            assert rejected.status_code in {400, 404, 422}
            rejected = await client.post('/task-groups', json={'tasks': [
                {'goal': 'Valid task', 'model_profile': 'fast'},
                {'goal': 'Invalid task', 'model_profile': 'unauthorized-model'},
            ]})
            assert rejected.status_code in {400, 404, 422}
            assert (await client.get('/sessions')).raise_for_status().json() == []

            first = (await client.post('/sessions', json={'title': 'First', 'initial_task': 'Work one',
                                                           'model_profile': 'fast'})).raise_for_status().json()
            first_path = f"/sessions/{first['session_id']}"
            async with asyncio.timeout(12):
                await inference_in_progress.wait()
            second = (await client.post('/sessions', json={'title': 'Second', 'initial_task': 'Work two',
                                                            'model_profile': 'deep'})).raise_for_status().json()
            second_path = f"/sessions/{second['session_id']}"
            (await client.post(f'{second_path}/pause')).raise_for_status()
            allow_first_decision.set()
            await wait_state(client, first_path, 'completed')
            await wait_state(client, second_path, 'paused')
            assert model_requests == ['qwen3:0.6b']

            (await client.post(f'{second_path}/resume')).raise_for_status()
            await wait_state(client, second_path, 'completed')
            assert model_requests == ['qwen3:0.6b', 'granite-local']
            messages = (await client.get(f'{second_path}/messages')).raise_for_status().json()
            assert any(message['content'] == 'Processed by granite-local.' for message in messages)


async def test_saved_model_selection_does_not_fall_back_after_profile_changes(tmp_path, monkeypatch):
    requests = []

    async def model_response(client, url, body, *, headers=None):
        requests.append(body['model'])
        return local_reply(url, decision={'kind': 'finish', 'summary': 'Unexpected replacement model.'})

    monkeypatch.setattr(BrowserPlanner, '_request', staticmethod(model_response))
    with fixture_website() as (site, _):
        settings = configure_models(live_settings(tmp_path, site), parallelism=1)
        async with workspace_app(settings) as (_, client):
            created = (await client.post('/sessions', json={'model_profile': 'fast'})).raise_for_status().json()
        session = f"/sessions/{created['session_id']}"

        settings.browser_local_model_profiles['fast']['model'] = 'different-model'
        async with workspace_app(settings) as (app, client):
            (await client.post(f'{session}/messages', json={'content': 'Use the selected model'})).raise_for_status()
            await app.state.execution_manager.wait_for_idle(timeout=12)
            state = (await client.get(session)).raise_for_status().json()['state']
            assert state['run_status'] == 'failed'
            assert state['last_error_code'] == 'model_profile_changed'
            assert state['model_name'] == 'qwen3:0.6b'
            assert requests == []


async def test_group_queues_at_browser_capacity_and_streams_progress_until_browser_closes(tmp_path):
    first_work_started = asyncio.Event()
    allow_first_result = asyncio.Event()
    started: list[str] = []

    class Planner:
        async def decide(self, *, goal, observation, history):
            started.append(goal)
            if len(started) == 1:
                first_work_started.set()
                await allow_first_result.wait()
            return finish(f'Finished {goal}')

    with fixture_website() as (site, _):
        settings = live_settings(tmp_path, site)
        settings.browser_max_branches = 1
        async with workspace_app(settings, planner=Planner(), network=True) as (_, client):
            created = (await client.post('/task-groups', json={'tasks': [
                {'goal': 'one browser'}, {'goal': 'another browser'},
            ]})).raise_for_status().json()
            path = f"/task-groups/{created['group_id']}"
            async with asyncio.timeout(12):
                await first_work_started.wait()
                async with client.stream('GET', f'{path}/stream') as stream:
                    stream.raise_for_status()
                    event = None
                    async for line in stream.aiter_lines():
                        if line.startswith('event: '):
                            event = line.removeprefix('event: ')
                        if event == 'group' and line.startswith('data: '):
                            progress = json.loads(line.removeprefix('data: '))
                            if progress['counts']['queued'] == 1:
                                break
                    else:
                        raise AssertionError('Task group stream ended before reporting the queued task')
            assert progress['group_id'] == created['group_id']
            assert progress['counts']['running'] == 1
            waiting = (await client.get(path, params={'wait_for': 'settled', 'wait_seconds': 1})).raise_for_status().json()
            assert waiting['timed_out']
            assert waiting['counts']['terminal'] == 0

            first = next(task for task in created['tasks'] if task['goal'] == started[0])
            first_path = f"/sessions/{first['session_id']}"
            allow_first_result.set()
            await wait_state(client, first_path, 'completed')
            assert len(started) == 1, 'The first browser remains open for inspection'
            (await client.post(f'{first_path}/browser/close', json={})).raise_for_status()
            settled = (await client.get(path, params={'wait_for': 'settled', 'wait_seconds': 10}, timeout=12)).raise_for_status().json()
            assert settled['status'] == 'settled'
            assert settled['counts']['terminal'] == 2
            assert len(started) == 2


async def test_group_attention_wait_preserves_sensitive_approval_boundary(tmp_path):
    with fixture_website() as (site, visits):
        approval_page = f'{site}/approval'

        class Planner:
            async def decide(self, *, goal, observation, history):
                if 'Submit' not in goal or visits['POST /commit-a']:
                    return finish('Task finished.')
                if observation.url != approval_page:
                    return action('open', url=approval_page)
                return action('click', target_id=target(observation, 'Submit first record'))

        async with workspace_app(live_settings(tmp_path, site), planner=Planner()) as (_, client):
            created = (await client.post('/task-groups', json={'tasks': [
                {'goal': 'Finish without navigating'},
                {'goal': f'Submit first record on {approval_page}'},
            ]})).raise_for_status().json()
            path = f"/task-groups/{created['group_id']}"
            waiting = (await client.get(path, params={'wait_for': 'attention', 'wait_seconds': 15})).raise_for_status().json()
            assert waiting['status'] == 'attention'
            assert waiting['counts']['attention'] == 1
            assert not waiting['timed_out']
            assert visits['POST /commit-a'] == 0
            approval = next(task for task in waiting['tasks'] if task['run_status'] == 'awaiting_approval')
            assert approval['attention']
            session = f"/sessions/{approval['session_id']}"
            state = (await client.get(session)).raise_for_status().json()['state']
            (await client.post(f'{session}/approve', json={'correlation_id': state['approval_correlation_id']})).raise_for_status()
            settled = (await client.get(path, params={'wait_for': 'settled', 'wait_seconds': 15})).raise_for_status().json()
            assert settled['status'] == 'settled'
            assert settled['counts']['terminal'] == 2
            assert visits['POST /commit-a'] == 1


async def test_group_cannot_complete_explicit_navigation_task_from_blank_page(tmp_path):
    attempts = []
    unsupported_answer = 'The invoice total is 1234.50.'

    class Planner:
        async def decide(self, *, goal, observation, history):
            if goal.startswith('Open '):
                attempts.append(observation.url)
                return finish(unsupported_answer)
            return finish('Sibling task finished.')

    with fixture_website() as (site, visits):
        settings = live_settings(tmp_path, site)
        settings.max_step_retries = 1
        async with workspace_app(settings, planner=Planner()) as (_, client):
            group = (await client.post('/task-groups', params={'wait_for': 'attention', 'wait_seconds': 15}, json={
                'tasks': [{'goal': f'Open {site}/details and report the invoice total.'}, {'goal': 'Finish the sibling task'}],
            })).raise_for_status().json()
            assert group['status'] == 'attention'
            assert group['counts']['attention'] == 1
            first, sibling = group['tasks']
            assert first['run_status'] == 'awaiting_input'
            assert first['answer'] != unsupported_answer
            assert attempts == ['about:blank', 'about:blank']
            assert visits['GET /details'] == 0
            await wait_state(client, f"/sessions/{sibling['session_id']}", 'completed')


async def test_browser_waiting_task_is_scheduled_after_workspace_restart(tmp_path):
    started = []
    first_decision = asyncio.Event()

    class Planner:
        async def decide(self, *, goal, observation, history):
            started.append(goal)
            first_decision.set()
            return finish(f'Completed {goal}.')

    with fixture_website() as (site, _):
        settings = live_settings(tmp_path, site)
        settings.browser_max_branches = 1
        async with workspace_app(settings, planner=Planner()) as (_, client):
            group = (await client.post('/task-groups', json={
                'tasks': [{'goal': 'First task'}, {'goal': 'Second task'}],
            })).raise_for_status().json()
            await asyncio.wait_for(first_decision.wait(), timeout=12)
            for task in group['tasks']:
                status = 'completed' if task['goal'] == started[0] else 'queued'
                await wait_state(client, f"/sessions/{task['session_id']}", status)
            assert len(started) == 1

        async with workspace_app(settings, planner=Planner()) as (_, client):
            path = f"/task-groups/{group['group_id']}"
            settled = (await client.get(path, params={'wait_for': 'settled', 'wait_seconds': 15}, timeout=16)).raise_for_status().json()
            assert settled['status'] == 'settled'
            assert not settled['timed_out']
            assert sorted(started) == ['First task', 'Second task']
