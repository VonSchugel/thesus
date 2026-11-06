from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query

from app.api.deps import get_services
from app.core.orchestrator import OrchestratorService
from app.core.errors import ValidationError
from app.models.api import ApproveRequest, BranchRequest, RejectRequest, SnapshotRequest

router = APIRouter(tags=['control'])


@router.post('/sessions/{session_id}/pause')
async def pause_session(
    session_id: str,
    branch_id: str | None = Query(default=None),
    services: OrchestratorService = Depends(get_services),
):
    return await services.pause(session_id, branch_id=branch_id)


@router.post('/sessions/{session_id}/resume')
async def resume_session(
    session_id: str,
    branch_id: str | None = Query(default=None),
    services: OrchestratorService = Depends(get_services),
):
    return await services.resume(session_id, branch_id=branch_id)


@router.post('/sessions/{session_id}/takeover')
async def takeover_session(
    session_id: str,
    branch_id: str | None = Query(default=None),
    x_workspace_client: UUID | None = Header(default=None),
    services: OrchestratorService = Depends(get_services),
):
    if services.live:
        if not x_workspace_client:
            raise ValidationError('Workspace client ID is required for browser control')
        return await services.live.takeover(await services.live.context(session_id, branch_id), str(x_workspace_client))
    return await services.takeover(session_id, branch_id=branch_id)


@router.post('/sessions/{session_id}/release')
async def release_session(
    session_id: str,
    payload: SnapshotRequest,
    x_workspace_client: UUID | None = Header(default=None),
    services: OrchestratorService = Depends(get_services),
):
    if services.live:
        return await services.live.release(await services.live.context(session_id, payload.branch_id),
                                           str(x_workspace_client) if x_workspace_client else None,
                                           payload.lease_id, payload.note, payload.resume)
    return await services.release(session_id, branch_id=payload.branch_id, note=payload.note)


@router.post('/sessions/{session_id}/approve')
async def approve_session(session_id: str, payload: ApproveRequest, services: OrchestratorService = Depends(get_services)):
    if services.live:
        return await services.live.decide(await services.live.context(session_id, payload.branch_id),
                                          payload.correlation_id, 'approve', payload.note)
    return await services.approve(session_id, branch_id=payload.branch_id, correlation_id=payload.correlation_id, note=payload.note)


@router.post('/sessions/{session_id}/reject')
async def reject_session(session_id: str, payload: RejectRequest, services: OrchestratorService = Depends(get_services)):
    if services.live:
        return await services.live.decide(await services.live.context(session_id, payload.branch_id),
                                          payload.correlation_id, 'reject', payload.reason)
    return await services.reject(session_id, branch_id=payload.branch_id, correlation_id=payload.correlation_id, reason=payload.reason)


@router.post('/sessions/{session_id}/branch')
async def branch_session(session_id: str, payload: BranchRequest, services: OrchestratorService = Depends(get_services)):
    return await services.create_branch_from_checkpoint(
        session_id,
        source_branch_id=payload.source_branch_id,
        checkpoint_id=payload.checkpoint_id,
        name=payload.name,
    )


@router.post('/sessions/{session_id}/snapshot')
async def snapshot_session(session_id: str, payload: SnapshotRequest, services: OrchestratorService = Depends(get_services)):
    return await services.snapshot(session_id, branch_id=payload.branch_id, note=payload.note)

