from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from app.app import create_app
from app.core.config import Settings
from app.integrations.browser.fake_browser_adapter import FakeBrowserAdapter


@asynccontextmanager
async def run_test_app(tmp_path: Path, *, step_delay_s: float = 0.05, action_rate_limit_ms: int = 0):
    db_path = tmp_path / 'test.db'
    data_dir = tmp_path / 'data'
    settings = Settings(
        app_db_url=f'sqlite+aiosqlite:///{db_path.as_posix()}',
        data_dir=data_dir,
        screenshot_dir=data_dir / 'screenshots',
        profile_dir=data_dir / 'profiles',
        action_rate_limit_ms=action_rate_limit_ms,
        use_fake_browser_adapter=True,
        local_llm_enrichment_enabled=False,
        log_level='WARNING',
    )
    settings.ensure_dirs()
    fake = FakeBrowserAdapter(step_delay_s=step_delay_s)
    app = create_app(settings=settings, browser_adapter=fake)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as client:
            yield app, client, fake, settings


@pytest.fixture
async def app_factory(tmp_path: Path):
    @asynccontextmanager
    async def factory(*, step_delay_s: float = 0.05, action_rate_limit_ms: int = 0):
        async with run_test_app(tmp_path, step_delay_s=step_delay_s, action_rate_limit_ms=action_rate_limit_ms) as ctx:
            yield ctx

    yield factory


async def create_session(client: httpx.AsyncClient, title: str = 'test') -> dict:
    resp = await client.post('/sessions', json={'title': title})
    resp.raise_for_status()
    return resp.json()


async def wait_idle(app, timeout: float = 5.0) -> None:
    await app.state.execution_manager.wait_for_idle(timeout=timeout)

