import pytest

from app.core.errors import classify_exception
from tests.conftest import create_session


@pytest.mark.asyncio
async def test_api_returns_classified_conflict_error(app_factory):
    async with app_factory() as (_, client, _, _):
        session = await create_session(client)
        sid = session['session_id']
        bid = session['selected_branch_id']
        take = await client.post(f'/sessions/{sid}/takeover', params={'branch_id': bid})
        assert take.status_code == 200
        resume = await client.post(f'/sessions/{sid}/resume', params={'branch_id': bid})
        assert resume.status_code == 409
        payload = resume.json()
        assert payload['error_code'] == 'state_conflict'
        assert payload['error_category'] == 'control_state'
        assert 'recovery_options' in payload


def test_classifier_handles_missing_openai_key_message():
    classified = classify_exception(RuntimeError('OPENAI_API_KEY is required when BROWSER_USE_LLM_PROVIDER=openai'))
    assert classified.code == 'missing_openai_api_key'
    assert classified.category == 'configuration'
    assert classified.retryable is False
