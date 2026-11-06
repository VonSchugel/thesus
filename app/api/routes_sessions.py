from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_services
from app.core.orchestrator import OrchestratorService
from app.models.api import CreateSessionRequest, SessionResponse

router = APIRouter(tags=['sessions'])


@router.get('/sessions')
async def list_sessions(services: OrchestratorService = Depends(get_services)):
    return await services.list_sessions()


@router.post('/sessions', response_model=SessionResponse)
async def create_session(payload: CreateSessionRequest, services: OrchestratorService = Depends(get_services)):
    return await services.create_session(title=payload.title, root_task=payload.initial_task,
                                         model_profile=payload.model_profile, start_task=True)


@router.get('/sessions/{session_id}', response_model=SessionResponse)
async def get_session(
    session_id: str,
    branch_id: str | None = Query(default=None),
    services: OrchestratorService = Depends(get_services),
):
    return await services.get_session(session_id, branch_id=branch_id)


@router.get('/sessions/{session_id}/checkpoints')
async def list_checkpoints(
    session_id: str,
    branch_id: str | None = Query(default=None),
    services: OrchestratorService = Depends(get_services),
):
    return await services.list_checkpoints(session_id, branch_id=branch_id)

