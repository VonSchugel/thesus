from __future__ import annotations

from urllib.parse import unquote
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.api.deps import get_services
from app.api.snapshots import stream_snapshots
from app.core.errors import ConflictError, ValidationError
from app.core.orchestrator import OrchestratorService
from app.models.api import RejectRequest, UIStatePollResponse
from app.models.browser import BrowserCommand

router = APIRouter(tags=['workspace'])


class ManualCommandRequest(BaseModel):
    lease_id: UUID
    expected_revision: str = Field(min_length=1, max_length=120)
    command_id: UUID
    command: BrowserCommand
    frame_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')


class EditActionRequest(BaseModel):
    branch_id: str | None = None
    correlation_id: str
    command: BrowserCommand


class CloseBrowserRequest(BaseModel):
    lease_id: UUID | None = None


def live_workspace(services: OrchestratorService):
    if services.live is None:
        raise ConflictError('The live workspace requires BROWSER_BACKEND=playwright')
    return services.live


@router.get('/workspace/capabilities')
async def capabilities(services: OrchestratorService = Depends(get_services)):
    return {'live_browser': services.live is not None, 'demo_url': services.settings.browser_demo_url if services.live else None}


async def read_workspace_snapshot(services: OrchestratorService, session_id: str, branch_id: str | None) -> UIStatePollResponse:
    async with services.session_factory() as db:
        if db.get_bind().dialect.name == 'sqlite':
            # SQLite's legacy driver does not start a read transaction for SELECT.
            await db.execute(text('BEGIN'))
        branch = branch_id or await services._resolve_branch_id(db, session_id, None)
        state = await services._build_session_response(db, session_id, branch)
        messages = await services.repos.messages.list_messages(db, session_id=session_id, branch_id=branch, limit=120)
        events = await services.repos.events.list_events(db, session_id=session_id, branch_id=branch, limit=180)
        return UIStatePollResponse(session=state, messages=[services._message_to_api(item) for item in messages],
                                   events=[services._event_to_api(item) for item in events])


@router.get('/sessions/{session_id}/workspace', response_model=UIStatePollResponse)
async def workspace_snapshot(session_id: str, response: Response, branch_id: str | None = None,
                             services: OrchestratorService = Depends(get_services)):
    response.headers['Cache-Control'] = 'no-store'
    return await read_workspace_snapshot(services, session_id, branch_id)


@router.get('/sessions/{session_id}/stream')
async def stream(session_id: str, request: Request, branch_id: str | None = None,
                 services: OrchestratorService = Depends(get_services)):
    # Resolve before starting the response so invalid scopes return ordinary HTTP errors.
    async with services.session_factory() as db:
        branch = await services._resolve_branch_id(db, session_id, branch_id)

    async def snapshot() -> str:
        return (await read_workspace_snapshot(services, session_id, branch)).model_dump_json()

    return stream_snapshots(request, 'workspace', snapshot)


@router.get('/sessions/{session_id}/browser')
async def browser_state(session_id: str, branch_id: str | None = None, x_workspace_client: UUID | None = Header(default=None),
                        services: OrchestratorService = Depends(get_services)):
    workspace = live_workspace(services)
    ctx = await workspace.context(session_id, branch_id)
    live = await workspace.browser.get_state(ctx)
    return JSONResponse(workspace.describe_browser(ctx, live, str(x_workspace_client) if x_workspace_client else None),
                        headers={'Cache-Control': 'no-store'})


@router.get('/sessions/{session_id}/browser/frame')
async def browser_frame(session_id: str, branch_id: str | None = None,
                        services: OrchestratorService = Depends(get_services)):
    workspace = live_workspace(services)
    ctx = await workspace.context(session_id, branch_id)
    pixels, frame = await workspace.browser.frame(ctx)
    return Response(pixels, media_type='image/jpeg', headers={
        'Cache-Control': 'no-store', 'X-Browser-Revision': frame.revision, 'X-Browser-Tab-Id': frame.tab_id,
        'X-Browser-Frame-Id': frame.frame_id,
        'X-Browser-Width': str(frame.width), 'X-Browser-Height': str(frame.height),
    })


@router.post('/sessions/{session_id}/browser/command')
async def browser_command(session_id: str, payload: ManualCommandRequest, branch_id: str | None = None,
                          x_workspace_client: UUID | None = Header(default=None),
                          services: OrchestratorService = Depends(get_services)):
    workspace = live_workspace(services)
    ctx = await workspace.context(session_id, branch_id)
    return await workspace.manual_command(ctx, client=str(x_workspace_client) if x_workspace_client else None,
                                          lease_id=str(payload.lease_id), command_id=str(payload.command_id),
                                          revision=payload.expected_revision, command=payload.command, frame_id=payload.frame_id)


@router.post('/sessions/{session_id}/browser/close')
async def close_browser(session_id: str, payload: CloseBrowserRequest, branch_id: str | None = None,
                        x_workspace_client: UUID | None = Header(default=None),
                        services: OrchestratorService = Depends(get_services)):
    workspace = live_workspace(services)
    ctx = await workspace.context(session_id, branch_id)
    return await workspace.close_browser(ctx, str(x_workspace_client) if x_workspace_client else None,
                                         str(payload.lease_id) if payload.lease_id else None)


@router.post('/sessions/{session_id}/browser/upload')
async def browser_upload(session_id: str, request: Request, branch_id: str | None = None,
                         x_artifact_name: str = Header(min_length=1, max_length=240),
                         services: OrchestratorService = Depends(get_services)):
    workspace = live_workspace(services)
    ctx = await workspace.context(session_id, branch_id)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > services.settings.browser_artifact_limit_bytes:
            raise ValidationError('File exceeds the workspace artifact size limit')
    async with await workspace.lock(ctx):
        return await workspace.browser.store_upload(ctx, unquote(x_artifact_name), bytes(data))


@router.get('/sessions/{session_id}/browser/artifacts/{artifact_id}')
async def browser_artifact(session_id: str, artifact_id: str, branch_id: str | None = None,
                           services: OrchestratorService = Depends(get_services)):
    workspace = live_workspace(services)
    ctx = await workspace.context(session_id, branch_id)
    path, artifact = workspace.browser.artifact_path(ctx, artifact_id)
    return FileResponse(path, filename=artifact.name, media_type='application/octet-stream', headers={'Cache-Control': 'no-store'})


@router.post('/sessions/{session_id}/stop')
async def stop_run(session_id: str, branch_id: str | None = Query(default=None),
                   services: OrchestratorService = Depends(get_services)):
    workspace = live_workspace(services)
    return await workspace.change_run(await workspace.context(session_id, branch_id), 'stop')


@router.post('/sessions/{session_id}/replan')
async def replan_run(session_id: str, branch_id: str | None = Query(default=None),
                     services: OrchestratorService = Depends(get_services)):
    workspace = live_workspace(services)
    return await workspace.change_run(await workspace.context(session_id, branch_id), 'replan')


@router.post('/sessions/{session_id}/cancel-action')
async def cancel_action(session_id: str, payload: RejectRequest, services: OrchestratorService = Depends(get_services)):
    workspace = live_workspace(services)
    return await workspace.decide(await workspace.context(session_id, payload.branch_id), payload.correlation_id,
                                  'cancel_action', payload.reason)


@router.post('/sessions/{session_id}/edit-action')
async def edit_action(session_id: str, payload: EditActionRequest, services: OrchestratorService = Depends(get_services)):
    workspace = live_workspace(services)
    return await workspace.edit_action(await workspace.context(session_id, payload.branch_id), payload.correlation_id,
                                       payload.command)
