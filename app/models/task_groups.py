from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class GroupTaskRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    title: str | None = Field(default=None, max_length=160)
    goal: str = Field(min_length=1, max_length=12000)
    model_profile: str | None = Field(default=None, max_length=48)


class CreateTaskGroupRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    title: str | None = Field(default=None, max_length=160)
    request_id: UUID | None = None
    tasks: list[GroupTaskRequest] = Field(min_length=2, max_length=24)


class GroupTaskResponse(BaseModel):
    session_id: str
    branch_id: str
    title: str | None
    goal: str
    model_profile: str
    model_name: str | None
    model_status: str | None = None
    run_status: str
    answer: str | None = None
    error: str | None = None
    activity: str | None = None
    attention: bool


class GroupCounts(BaseModel):
    total: int
    queued: int
    running: int
    attention: int
    terminal: int


class TaskGroupResponse(BaseModel):
    group_id: str
    title: str | None
    status: Literal['active', 'attention', 'settled']
    created_at: datetime
    tasks: list[GroupTaskResponse]
    counts: GroupCounts
    timed_out: bool = False


WaitFor = Literal['attention', 'settled']
