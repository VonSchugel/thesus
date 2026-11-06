from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_services
from app.core.orchestrator import OrchestratorService
from app.models.api import MessageResponse, PostMessageRequest

router = APIRouter(tags=['messages'])


@router.get('/sessions/{session_id}/messages', response_model=list[MessageResponse])
async def get_messages(
    session_id: str,
    branch_id: str | None = Query(default=None),
    limit: int = Query(default=200, ge=0, le=5000),
    before: str | None = Query(default=None),
    services: OrchestratorService = Depends(get_services),
):
    return await services.list_messages(session_id, branch_id=branch_id, limit=limit, before=before)


@router.post('/sessions/{session_id}/messages')
async def post_message(session_id: str, payload: PostMessageRequest, services: OrchestratorService = Depends(get_services)):
    return await services.post_user_message(
        session_id,
        branch_id=payload.branch_id,
        content=payload.content,
        trigger_agents=payload.trigger_agents,
    )

