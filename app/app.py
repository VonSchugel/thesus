from __future__ import annotations

from contextlib import asynccontextmanager
from base64 import b64decode
from binascii import Error as Base64Error
from hmac import compare_digest
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.router import router as api_router
from app.core.config import Settings, load_settings
from app.core.control_lock import ControlLockRegistry
from app.core.errors import AppError, classify_exception
from app.core.execution_manager import ExecutionManager
from app.core.logging import configure_logging
from app.core.orchestrator import OrchestratorService
from app.core.recovery import recover_runtime_states
from app.core.replay import ReplayService
from app.db.engine import make_engine, make_session_factory
from app.db.init_db import init_db
from app.integrations.browser.fake_browser_adapter import FakeBrowserAdapter
from app.integrations.browser.playwright_adapter import PlaywrightAdapter


def create_app(*, settings: Settings | None = None, browser_adapter: Any | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    if browser_adapter is None:
        backend = settings.browser_backend or ('fake' if settings.use_fake_browser_adapter else 'playwright')
        if backend == 'fake':
            browser_adapter = FakeBrowserAdapter()
        elif backend == 'playwright':
            browser_adapter = PlaywrightAdapter(settings)
        else:
            raise ValueError('BROWSER_BACKEND must be playwright or fake')

    lock_registry = ControlLockRegistry()
    replay_service = ReplayService()
    services = OrchestratorService(
        settings=settings,
        session_factory=session_factory,
        browser_adapter=browser_adapter,
        replay_service=replay_service,
        lock_registry=lock_registry,
    )
    if hasattr(browser_adapter, "profile_path_getter"):
        browser_adapter.profile_path_getter = services.profile_path_for
    execution_manager = ExecutionManager(services.process_branch, rerun_when_busy=services.live is not None)
    services.set_execution_manager(execution_manager)
    investigations = None
    if settings.reverse_engineering_enabled:
        from app.api.routes_investigations import router as investigation_router
        from app.reverse_engineering.service import Investigations

        investigations = Investigations(session_factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await init_db(engine)
        async with session_factory() as db:
            await recover_runtime_states(db)
            if services.live:
                await services.live.recover(db)
            await services.reconcile_pending_enrichment(db)
            await db.commit()
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.services = services
        app.state.execution_manager = execution_manager
        app.state.browser_adapter = browser_adapter
        app.state.investigations = investigations
        try:
            await execution_manager.start()
            if investigations:
                await investigations.start()
            if services.live:
                await services.live.schedule_queued()
            yield
        finally:
            if investigations:
                await investigations.manager.shutdown()
            await execution_manager.shutdown()
            await services.shutdown_enrichment()
            try:
                if services.live:
                    await services.live.drain_manual_commands()
                    await services.live.browser.shutdown()
            finally:
                await engine.dispose()

    app = FastAPI(title='Agentic Browser Workspace', lifespan=lifespan)
    app.include_router(api_router)
    if investigations:
        app.include_router(investigation_router)

    if not settings.workspace_access_token:
        @app.middleware('http')
        async def require_loopback_peer(request: Request, call_next):
            # Host and Origin are supplied by the caller. The ASGI peer address
            # is the loopback boundary when no access token is configured.
            try:
                peer = ip_address(request.client.host) if request.client else None
            except ValueError:
                peer = None
            mapped = getattr(peer, 'ipv4_mapped', None)
            if not ((peer and peer.is_loopback) or (mapped and mapped.is_loopback)):
                return JSONResponse({'detail': 'Remote workspace access requires authentication'}, status_code=403)
            return await call_next(request)

    if services.live or settings.workspace_access_token or investigations:
        @app.middleware('http')
        async def workspace_access(request: Request, call_next):
            origin = request.headers.get('origin')
            unsafe = request.method not in {'GET', 'HEAD', 'OPTIONS'}
            host = request.headers.get('host', '').lower()
            if request.headers.get('sec-fetch-site') == 'cross-site' or (unsafe and origin and (
                origin.lower() not in {f'http://{host}', f'https://{host}'}
            )):
                return JSONResponse({'detail': 'Cross-origin workspace access is blocked'}, status_code=403)
            if settings.workspace_access_token:
                authorization = request.headers.get('authorization', '')
                scheme, _, credentials = authorization.partition(' ')
                if scheme.lower() == 'basic':
                    try:
                        username, separator, credentials = b64decode(credentials, validate=True).decode('utf-8').partition(':')
                    except (Base64Error, UnicodeDecodeError):
                        username, separator = '', ''
                    if username != 'workspace' or not separator:
                        credentials = ''
                elif scheme.lower() != 'bearer':
                    credentials = ''
                if not compare_digest(credentials.encode('utf-8'), settings.workspace_access_token.encode('utf-8')):
                    return JSONResponse({'detail': 'Workspace authentication required'}, status_code=401,
                                        headers={'WWW-Authenticate': 'Basic realm="Browser Workspace"'})
            response = await call_next(request)
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['Referrer-Policy'] = 'no-referrer'
            response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' blob:; frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
            return response

        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.workspace_hosts)

        @app.exception_handler(RequestValidationError)
        async def workspace_validation_error(_: Request, exc: RequestValidationError):
            return JSONResponse({'detail': 'Invalid workspace request', 'fields': [
                {'location': error['loc'], 'type': error['type']} for error in exc.errors()
            ]}, status_code=422)

    @app.get('/llm/status')
    async def local_llm_status():
        status = await services.local_llm.status()
        status['enrichment_enabled'] = settings.local_llm_enrichment_enabled
        return status

    static_dir = Path(__file__).resolve().parent / 'ui' / 'static'
    app.mount('/static', StaticFiles(directory=str(static_dir)), name='static')

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError):
        classification = exc.classify()
        return JSONResponse(
            status_code=classification.http_status,
            content=classification.to_response(include_raw_detail=None if services.live or investigations else str(exc), error_type=type(exc).__name__),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_: Request, exc: Exception):
        classification = classify_exception(exc)
        return JSONResponse(
            status_code=classification.http_status,
            content=classification.to_response(include_raw_detail=None if services.live or investigations else str(exc), error_type=type(exc).__name__),
        )

    return app

