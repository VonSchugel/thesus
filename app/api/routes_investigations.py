from __future__ import annotations

import asyncio
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response
from starlette.requests import ClientDisconnect

from app.core.errors import AppError
from app.reverse_engineering.contracts import (
    Comparison, ComparisonRequest, Investigation, MAX_ARTIFACT_BYTES, Mode, RequestedTarget,
)
from app.reverse_engineering.service import Investigations


router = APIRouter(prefix='/sessions/{session_id}/investigations', tags=['investigations'])


def _service(request: Request) -> Investigations:
    return request.app.state.investigations


@router.post('', status_code=202, response_model=Investigation)
async def submit_investigation(
    request: Request, session_id: UUID, target: RequestedTarget = 'auto', mode: Mode = 'inspect',
    review: bool = False, authorization: Annotated[str | None, Header(alias='X-Analysis-Authorization')] = None,
    run_id: Annotated[UUID | None, Header(alias='X-Investigation-Id')] = None,
) -> Investigation:
    if authorization not in ('owned', 'authorized'):
        raise AppError('Attest that you own this artifact or have authorization to analyze it.',
                       status_code=403, error_code='analysis_authorization_required')
    if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/octet-stream':
        raise AppError('Upload raw artifact bytes as application/octet-stream.', status_code=415,
                       error_code='artifact_media_type')
    service = _service(request)
    try:
        async with asyncio.timeout(15), service.upload_slots:
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > MAX_ARTIFACT_BYTES:
                    raise AppError('The artifact exceeds 4 MiB.', status_code=413, error_code='input_limit')
                data.extend(chunk)
    except TimeoutError as exc:
        raise AppError('The upload exceeded its time limit.', status_code=408, error_code='upload_timeout') from exc
    except ClientDisconnect as exc:
        raise AppError('The upload ended before the artifact was received.', status_code=400,
                       error_code='upload_interrupted') from exc
    if not data:
        raise AppError('The artifact is empty.', status_code=422, error_code='empty_artifact')
    return await service.submit(session_id, bytes(data), target, mode, authorization, review, run_id)


@router.get('', response_model=list[Investigation])
async def list_investigations(request: Request, session_id: UUID) -> list[Investigation]:
    return await _service(request).list(session_id)


@router.get('/{run_id}', response_model=Investigation)
async def get_investigation(request: Request, session_id: UUID, run_id: UUID) -> Investigation:
    return await _service(request).get(session_id, run_id)


@router.post('/{run_id}/control/{action}', response_model=Investigation)
async def control_investigation(
    request: Request, session_id: UUID, run_id: UUID, action: Literal['resume', 'cancel'],
) -> Investigation:
    return await _service(request).control(session_id, run_id, action)


@router.post('/{run_id}/compare', response_model=Comparison)
async def compare_investigations(
    request: Request, session_id: UUID, run_id: UUID, payload: ComparisonRequest,
) -> Comparison:
    return await _service(request).compare(session_id, run_id, payload.other_id)


@router.delete('/{run_id}', status_code=204)
async def remove_investigation(request: Request, session_id: UUID, run_id: UUID) -> Response:
    await _service(request).remove(session_id, run_id)
    return Response(status_code=204)
