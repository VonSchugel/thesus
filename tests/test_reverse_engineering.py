from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import update

from app.app import create_app
from app.core.config import Settings
from app.reverse_engineering import service
from app.reverse_engineering.contracts import EngineReport, Locator, Observation
from tests.browser_harness import workspace_app


UPLOAD = {'X-Analysis-Authorization': 'owned', 'Content-Type': 'application/octet-stream'}


def _settings(tmp_path: Path, *, enabled: bool = True, token: str | None = None) -> Settings:
    return Settings(
        app_db_url=f'sqlite+aiosqlite:///{(tmp_path / "workspace.db").as_posix()}',
        data_dir=tmp_path / 'data', profile_dir=tmp_path / 'profiles', screenshot_dir=tmp_path / 'screenshots',
        browser_backend='fake', reverse_engineering_enabled=enabled, workspace_access_token=token,
        local_llm_enrichment_enabled=False,
    )


@asynccontextmanager
async def _workspace(settings: Settings):
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            yield app, client


async def _session(client: httpx.AsyncClient) -> str:
    return (await client.post('/sessions', json={'title': 'Investigation'})).raise_for_status().json()['session_id']


def _capture(status: int = 200, *, path: str = '/sample', invalid: bool = False) -> bytes:
    entries = [{
        'request': {'method': 'GET', 'url': f'https://fixture.invalid{path}?key=secret',
                    'headers': [{'name': 'Authorization', 'value': 'secret'}]},
        'response': {'status': status, 'content': {'text': 'secret'}},
    }]
    if invalid:
        entries.append({'request': {}})
    return json.dumps({'log': {'version': '1.2', 'entries': entries}}).encode()


async def test_investigation_api_is_opt_in_and_uses_workspace_access(tmp_path):
    async with _workspace(_settings(tmp_path, enabled=False)) as (_, client):
        session_id = await _session(client)
        path = f'/sessions/{session_id}/investigations'
        assert (await client.post(path, content=_capture(), headers=UPLOAD)).status_code == 404
    async with _workspace(_settings(tmp_path, token='local-test')) as (_, client):
        assert (await client.post(path, content=_capture(), headers=UPLOAD)).status_code == 401
        client.headers['Authorization'] = 'Bearer local-test'
        assert (await client.post(path, content=_capture())).status_code == 403
        assert (await client.post(path, content=_capture(), headers={**UPLOAD, 'Origin': 'https://other.invalid'})).status_code == 403
        assert (await client.post(path, content=_capture(), headers={
            **UPLOAD, 'Content-Type': 'text/plain',
        })).status_code == 415
        assert (await client.post(path, content=b'', headers=UPLOAD)).status_code == 422
        assert (await client.post(path, content=b'a' * (4 * 1024 * 1024 + 1), headers=UPLOAD)).status_code == 413
        assert (await client.post(path, content=b'not recognized', headers=UPLOAD)).status_code == 422
        assert (await client.post(path, content=b'MZbad', params={'target': 'elf'}, headers=UPLOAD)).status_code == 422


async def test_har_workflow_persists_scoped_evidence_and_compares_captures(tmp_path):
    settings = _settings(tmp_path)
    async with _workspace(settings) as (app, client):
        session_id, other_session = await _session(client), await _session(client)
        base = f'/sessions/{session_id}/investigations'
        request_id = str(uuid4())
        artifact = _capture()
        queued = (await client.post(base, content=artifact, headers={**UPLOAD, 'X-Investigation-Id': request_id})).json()
        assert queued['id'] == request_id and queued['status'] == 'queued'
        assert queued['artifact_sha256'] == hashlib.sha256(artifact).hexdigest()
        retry = await client.post(base, content=artifact, headers={**UPLOAD, 'X-Investigation-Id': request_id})
        assert retry.status_code == 202 and retry.json()['id'] == request_id
        assert (await client.post(
            base, content=_capture(404), headers={**UPLOAD, 'X-Investigation-Id': request_id},
        )).status_code == 409
        assert (await client.get(f'/sessions/{other_session}/investigations/{request_id}')).status_code == 404

        run_ids = []
        for capture in (artifact, _capture(404), _capture(invalid=True)):
            run = (await client.post(base, content=capture, headers=UPLOAD)).raise_for_status().json()
            run_ids.append(run['id'])
        await app.state.investigations.manager.wait_for_idle()
        evidence = (await client.get(f'{base}/{request_id}')).raise_for_status().json()
        assert evidence['status'] == 'succeeded' and evidence['attempt'] == 1
        assert [item['action'] for item in evidence['audit']] == ['admitted', 'plan_checkpoint', 'started', 'succeeded']
        assert 'secret' not in json.dumps(evidence) and 'fixture.invalid' not in json.dumps(evidence)
        observation = next(item for item in evidence['report']['observations'] if item['key'] == 'http_exchange')
        assert observation['locator']['record'] == 0
        assert observation['value'] == {
            'method': 'GET', 'path_sha256': hashlib.sha256(b'/sample').hexdigest(), 'status': 200,
        }
        results = []
        for run_id in run_ids:
            result = (await client.post(f'{base}/{request_id}/compare', json={'other_id': run_id})).raise_for_status().json()
            results.append(result['result'])
        assert results == ['match', 'difference', 'inconclusive']
        assert all(item['report'] is None for item in (await client.get(base)).json())
    async with _workspace(settings) as (_, client):
        assert (await client.get(f'{base}/{request_id}')).raise_for_status().json() == evidence
        assert (await client.delete(f'{base}/{request_id}')).status_code == 204
        assert (await client.get(f'{base}/{request_id}')).status_code == 404


async def test_review_cancel_resume_and_recover_keep_a_single_pinned_plan(tmp_path, monkeypatch):
    began, cancelled = asyncio.Event(), asyncio.Event()
    result = EngineReport(observations=[Observation(key='fixture', value=True, locator=Locator())])
    calls = 0

    async def inspect(plan, artifact):
        nonlocal calls
        calls += 1
        if calls == 1:
            began.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        return result

    monkeypatch.setattr(service, 'inspect_artifact', inspect)
    settings = _settings(tmp_path)
    async with _workspace(settings) as (app, client):
        session_id = await _session(client)
        base = f'/sessions/{session_id}/investigations'
        pending = (await client.post(f'{base}?review=true', content=_capture(), headers=UPLOAD)).raise_for_status().json()
        path = f'{base}/{pending["id"]}'
        assert pending['status'] == 'waiting_for_review' and calls == 0
        assert (await client.post(f'{path}/control/resume')).raise_for_status().json()['status'] == 'queued'
        await asyncio.wait_for(began.wait(), timeout=3)
        assert (await client.post(f'{path}/control/cancel')).raise_for_status().json()['status'] == 'cancelled'
        await asyncio.wait_for(cancelled.wait(), timeout=3)
        await app.state.investigations.manager.wait_for_idle()
        assert (await client.get(path)).raise_for_status().json()['report'] is None
        # Recreate the persisted boundary of a process loss after a claim.
        async with app.state.session_factory() as db:
            await db.execute(update(service.InvestigationRow).where(
                service.InvestigationRow.id == pending['id'],
            ).values(status='running'))
            await db.commit()
    async with _workspace(settings) as (app, client):
        restored = (await client.get(path)).raise_for_status().json()
        assert restored['status'] == 'interrupted' and restored['plan'] == pending['plan']
        assert restored['audit'][-1]['action'] == 'recovered_interruption'
        (await client.post(f'{path}/control/resume')).raise_for_status()
        await app.state.investigations.manager.wait_for_idle()
        done = (await client.get(path)).raise_for_status().json()
        assert done['status'] == 'succeeded' and done['attempt'] == 2 and calls == 2
        assert done['report'] == result.model_dump(mode='json')


async def test_resume_refuses_a_changed_backend(tmp_path, monkeypatch):
    async with _workspace(_settings(tmp_path)) as (app, client):
        session_id = await _session(client)
        base = f'/sessions/{session_id}/investigations'
        run = (await client.post(f'{base}?review=true', content=_capture(), headers=UPLOAD)).raise_for_status().json()
        monkeypatch.setattr(service, '_BACKEND_REVISION', 'incompatible-test')
        path = f'{base}/{run["id"]}'
        (await client.post(f'{path}/control/resume')).raise_for_status()
        await app.state.investigations.manager.wait_for_idle()
        result = (await client.get(path)).raise_for_status().json()
        assert result['status'] == 'failed' and result['error'] == 'backend_changed'
        assert result['plan'] == run['plan'] and result['report'] is None


async def test_worker_timeout_and_output_limits_reap_child(monkeypatch):
    from app.reverse_engineering import execution
    from app.reverse_engineering.contracts import EngineFailure, Plan

    processes = []
    create_process = asyncio.create_subprocess_exec

    async def start(*args, **kwargs):
        child = await create_process(*args, **kwargs)
        processes.append(child)
        return child

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', start)
    plan = Plan(target='har', mode='inspect', backend='test', versions={}, question='test')
    monkeypatch.setattr(execution, '_BOOT', 'import time; time.sleep(60)')
    monkeypatch.setattr(execution, 'TIMEOUT_SECONDS', 0.75)
    with pytest.raises(EngineFailure, match='analysis_timeout'):
        await execution.inspect_artifact(plan, _capture())
    monkeypatch.setattr(execution, '_BOOT',
                        'import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(b"x" * 2000000)')
    monkeypatch.setattr(execution, 'TIMEOUT_SECONDS', 5)
    with pytest.raises(EngineFailure, match='output_limit'):
        await execution.inspect_artifact(plan, _capture())
    assert len(processes) == 2 and all(child.returncode is not None for child in processes)


async def test_mcp_stdio_reads_retained_evidence_through_workspace_auth(tmp_path):
    pytest.importorskip('mcp.server.mcpserver')
    from mcp import StdioServerParameters
    from mcp.client import Client

    settings = _settings(tmp_path, token='test-bridge-token')
    async with workspace_app(settings, network=True) as (app, client):
        client.headers['Authorization'] = 'Bearer test-bridge-token'
        session_id = await _session(client)
        other_session = await _session(client)
        base = f'/sessions/{session_id}/investigations'
        run = (await client.post(base, content=_capture(), headers=UPLOAD)).raise_for_status().json()
        await app.state.investigations.manager.wait_for_idle()
        endpoint = StdioServerParameters(
            command=sys.executable,
            args=['-B', '-m', 'app.reverse_engineering.mcp_bridge', '--port', str(client.base_url.port)],
            cwd=Path(__file__).resolve().parents[1],
            env={'WORKSPACE_ACCESS_TOKEN': 'test-bridge-token'},
        )
        async with Client(endpoint, read_timeout_seconds=10) as bridge:
            tools = (await bridge.list_tools()).tools
            assert [tool.name for tool in tools] == ['get_investigation']
            assert tools[0].annotations.read_only_hint
            result = await bridge.call_tool('get_investigation', {'session_id': session_id, 'investigation_id': run['id']})
            assert result.structured_content['status'] == 'succeeded'
            assert result.structured_content['artifact_sha256'] == run['artifact_sha256']
            wrong = await bridge.call_tool('get_investigation', {'session_id': other_session, 'investigation_id': run['id']})
            assert wrong.is_error
