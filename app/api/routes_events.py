from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_services
from app.core.orchestrator import OrchestratorService
from app.models.api import EventResponse

router = APIRouter(tags=['events'])


@router.get('/sessions/{session_id}/events', response_model=list[EventResponse])
async def get_events(
    session_id: str,
    branch_id: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    action_type: str | None = Query(default=None),
    limit: int = Query(default=500, ge=0, le=5000),
    after_event_id: str | None = Query(default=None),
    services: OrchestratorService = Depends(get_services),
):
    return await services.list_events(
        session_id,
        branch_id=branch_id,
        actor=actor,
        action_type=action_type,
        limit=limit,
        after_event_id=after_event_id,
    )

