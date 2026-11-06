from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import get_services
from app.api.snapshots import stream_snapshots
from app.core.errors import ConflictError
from app.core.orchestrator import OrchestratorService
from app.models.task_groups import CreateTaskGroupRequest, TaskGroupResponse, WaitFor

router = APIRouter(tags=['task groups'])


def groups(services: OrchestratorService):
    if services.task_groups is None:
        raise ConflictError('Task groups require the live browser workspace')
    return services.task_groups


@router.get('/workspace/models')
async def model_catalog(services: OrchestratorService = Depends(get_services)):
    groups(services)
    return services.live.models.catalog()


@router.post('/task-groups', response_model=TaskGroupResponse)
async def create_task_group(payload: CreateTaskGroupRequest, wait_for: WaitFor = 'settled',
                            wait_seconds: float = Query(default=0, ge=0, le=30),
                            services: OrchestratorService = Depends(get_services)):
    task_groups = groups(services)
    group = await task_groups.create(payload)
    return await task_groups.wait(group.group_id, wait_for, wait_seconds) if wait_seconds else group


@router.get('/task-groups', response_model=list[TaskGroupResponse])
async def list_task_groups(services: OrchestratorService = Depends(get_services)):
    return await groups(services).list()


@router.get('/task-groups/{group_id}', response_model=TaskGroupResponse)
async def get_task_group(group_id: str, wait_for: WaitFor = 'settled',
                         wait_seconds: float = Query(default=0, ge=0, le=30),
                         services: OrchestratorService = Depends(get_services)):
    task_groups = groups(services)
    return await task_groups.wait(group_id, wait_for, wait_seconds) if wait_seconds else await task_groups.get(group_id)


@router.get('/task-groups/{group_id}/stream')
async def task_group_stream(group_id: str, request: Request, services: OrchestratorService = Depends(get_services)):
    task_groups = groups(services)
    await task_groups.get(group_id)

    async def snapshot() -> str:
        return (await task_groups.get(group_id)).model_dump_json()

    return stream_snapshots(request, 'group', snapshot)
