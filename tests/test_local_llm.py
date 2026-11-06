import json
import random
import string
import threading
from types import SimpleNamespace

import pytest

from app.agents.extractor import ExtractorAgent
from app.integrations.local_llm import LocalLLMError, LocalLLMProvider, _INFERENCE_SLOT


def provider(**kwargs):
    return LocalLLMProvider(SimpleNamespace(**kwargs))


def reply(summary='Recorded price is 500.', ids=None, **kwargs):
    return {'done': True, 'message': {'content': json.dumps({'summary': summary, 'source_ids': [1] if ids is None else ids})}, **kwargs}


@pytest.mark.parametrize('host', ['https://127.0.0.1:11434', 'http://example.org', 'http://127.0.0.1:11434/proxy', 'http://user:secret@localhost:11434', 'http://localhost:bad'])
def test_rejects_nonlocal_or_ambiguous_endpoint(host):
    with pytest.raises(LocalLLMError, match='invalid_local_endpoint'):
        provider(local_llm_host=host)._endpoint('/api/chat')


@pytest.mark.asyncio
async def test_inference_is_off_event_loop_bounded_and_source_linked(monkeypatch):
    local = provider()
    main_thread = threading.get_ident()

    def serve(path, body=None):
        assert threading.get_ident() != main_thread
        assert path == '/api/chat'
        assert body['think'] is False and body['stream'] is False
        assert body['options'] == {'num_thread': 4, 'num_ctx': 4096, 'num_predict': 768, 'temperature': 0}
        return reply()

    monkeypatch.setattr(local, '_http_json', serve)
    result = await local.enrich(items=[{'name': 'Example', 'price': '500', 'url': 'https://example.org'}], request='Compare')
    assert result['classification'] == 'GENERATED_ADVISORY_UNVERIFIED'
    assert result['source_ids'] == [1]
    assert result['source_records'][0]['price'] == '500'


@pytest.mark.parametrize('response', [reply(ids=[99]), reply(ids=[True]), reply(ids=[]), reply(ids=[1, 1]), reply(summary=''), reply(done_reason='length'), {'done': False}, {'done': True, 'message': {'content': '{invalid'}}])
@pytest.mark.asyncio
async def test_invalid_or_incomplete_output_falls_back(monkeypatch, response):
    local = provider()
    monkeypatch.setattr(local, '_http_json', lambda *args: response)
    result = await local.enrich(items=[{'name': 'Source'}], request='Task')
    assert result['status'] == 'fallback'
    assert 'summary' not in result


@pytest.mark.asyncio
async def test_busy_inference_does_not_queue_or_call_server(monkeypatch):
    local = provider()
    monkeypatch.setattr(local, '_http_json', lambda *args: pytest.fail('Busy provider must not call server'))
    assert _INFERENCE_SLOT.acquire(blocking=False)
    try:
        result = await local.enrich(items=[{'name': 'Source'}], request='Task')
    finally:
        _INFERENCE_SLOT.release()
    assert result['reason'] == 'provider_busy'


@pytest.mark.asyncio
async def test_readiness_distinguishes_missing_model(monkeypatch):
    local = provider()
    monkeypatch.setattr(local, '_http_json', lambda *args: {'models': [{'name': 'another-model'}]})
    result = await local.status()
    assert result['reachable'] is True and result['model_installed'] is False
    monkeypatch.setattr(local, '_http_json', lambda *args: {'models': [{'name': local.model}]})
    assert (await local.status())['status'] == 'ready'


@pytest.mark.asyncio
async def test_unavailable_service_preserves_canonical_extraction(monkeypatch):
    local = provider()

    def unavailable(*args):
        raise LocalLLMError('service_unavailable')

    monkeypatch.setattr(local, '_http_json', unavailable)
    blackboard = {'findings': [{'title': 'Candidate', 'price': '500', 'url': 'https://example.org'}]}
    baseline, text = await ExtractorAgent().summarize(user_request='Compare', blackboard=blackboard, plan=None)
    result, enriched_text = await ExtractorAgent(local_provider=local).summarize(user_request='Compare', blackboard=blackboard, plan=None)
    assert enriched_text == text
    assert result['items'] == baseline['items']
    assert result['local_llm']['status'] == 'fallback'
    assert result['local_llm']['reason'] == 'service_unavailable'


def test_transport_disables_proxies_and_enforces_response_limit(monkeypatch):
    from app.integrations import local_llm

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            assert size == 262_145
            return b'x' * size

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == 'http://127.0.0.1:11434/api/tags'
            assert timeout <= 5
            return Response()

    def build(*handlers):
        assert handlers[0].proxies == {}
        return Opener()

    monkeypatch.setattr(local_llm, 'build_opener', build)
    with pytest.raises(LocalLLMError, match='response_too_large'):
        provider()._http_json('/api/tags')


@pytest.mark.parametrize('source', ['界' * 1100, ''.join(random.Random(7).choices(string.ascii_letters + string.digits, k=1500))])
@pytest.mark.asyncio
async def test_large_or_high_entropy_source_cannot_silently_overflow_context(monkeypatch, source):
    from app.integrations import local_llm
    monkeypatch.setattr(local_llm, 'build_opener', lambda *args: pytest.fail('Over-budget content must not reach HTTP'))
    result = await provider().enrich(items=[{'name': 'A', 'price': source}, {'name': 'B', 'price': source}], request='Compare')
    assert result['status'] == 'fallback' and result['reason'] == 'input_too_large'


@pytest.mark.asyncio
async def test_status_route_reports_model_presence(app_factory, monkeypatch):
    async with app_factory() as (app, client, _, _):
        monkeypatch.setattr(app.state.services.local_llm, '_http_json', lambda *args: {'models': []})
        response = await client.get('/llm/status')
        assert response.status_code == 200
        assert response.json()['status'] == 'model_missing'
        assert response.json()['enrichment_enabled'] is False


@pytest.mark.parametrize('location', ['http_envelope', 'model_content'])
@pytest.mark.asyncio
async def test_deep_json_is_rejected_without_generated_text(monkeypatch, location):
    from app.integrations import local_llm
    nested = '[' * 10000 + '0' + ']' * 10000
    raw = nested.encode() if location == 'http_envelope' else json.dumps({'done': True, 'message': {'content': nested}}).encode()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            return raw[:size]

    class Opener:
        def open(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(local_llm, 'build_opener', lambda *args: Opener())
    result = await provider().enrich(items=[{'name': 'Canonical source', 'price': '500'}], request='Compare')
    assert result['status'] == 'fallback' and result['reason'] == 'invalid_response'
    assert 'summary' not in result and nested not in json.dumps(result)
