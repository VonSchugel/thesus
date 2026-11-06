import pytest

from app.agents.browser_planner import BrowserPlanner
from app.core.config import Settings
from app.models.browser import BrowserObservation


@pytest.mark.asyncio
async def test_loopback_model_uses_command_schema_without_forwarding_hosted_key(monkeypatch):
    async def model_response(client, url, body, *, headers):
        assert url == 'http://127.0.0.1:18177/v1/chat/completions'
        assert headers is None
        schema = body['response_format']['json_schema']['schema']
        alternatives = schema['$defs']['BrowserCommand']['anyOf']
        open_command = next(item for item in alternatives if item['properties']['kind']['const'] == 'open')
        assert set(open_command['properties']) == {'kind', 'url'}
        assert open_command['required'] == ['kind', 'url']
        assert all(item['properties']['kind']['const'] != 'click' for item in alternatives)
        return {
            'choices': [{'message': {'content': '{"kind":"command","summary":"Open the page",'
                                                '"command":{"kind":"open","url":"https://example.com/"}}'}}],
            'usage': {'prompt_tokens': 13, 'completion_tokens': 11},
        }

    monkeypatch.setattr(BrowserPlanner, '_request', staticmethod(model_response))
    settings = Settings(browser_agent_provider='openai', browser_agent_model='local-model',
                        openai_base_url='http://127.0.0.1:18177/v1', openai_api_key='test-hosted-key')
    observation = BrowserObservation(revision='fixture', tab_id='fixture', url='about:blank',
                                     title='', ax_tree='', elements=[])
    decision = await BrowserPlanner(settings).decide(goal='Open https://example.com/', observation=observation, history=[])
    assert decision.command.url == 'https://example.com/'
    assert decision.usage == {'input_tokens': 13, 'output_tokens': 11}
