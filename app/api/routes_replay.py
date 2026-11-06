from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_services
from app.core.orchestrator import OrchestratorService
from app.models.api import ReplayResponse

router = APIRouter(tags=['replay'])


@router.get('/sessions/{session_id}/replay', response_model=ReplayResponse)
async def get_replay(
    session_id: str,
    branch_id: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    action_type: str | None = Query(default=None),
    services: OrchestratorService = Depends(get_services),
):
    return await services.replay(session_id, branch_id=branch_id, actor=actor, action_type=action_type)

