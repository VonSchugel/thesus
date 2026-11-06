from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import ControlMode, RunStatus


class CreateSessionRequest(BaseModel):
    title: str | None = None
    initial_task: str | None = Field(default=None, max_length=12000)
    model_profile: str | None = Field(default=None, max_length=48)


class SessionBranchInfo(BaseModel):
    branch_id: str
    name: str
    parent_branch_id: str | None = None
    source_checkpoint_id: str | None = None
    created_at: datetime


class RuntimeStateResponse(BaseModel):
    run_status: str
    control_mode: str
    control_holder: str
    pause_requested: bool
    operator_paused: bool = False
    approval_required: bool
    approval_correlation_id: str | None = None
    last_known_url: str | None = None
    last_known_title: str | None = None
    tabs_json: list[dict[str, Any]] = Field(default_factory=list)
    latest_observation: str | None = None
    pending_action_summary: str | None = None
    pending_approval: dict[str, Any] | None = None
    browser_profile_path: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    model_profile: str | None = None
    model_name: str | None = None
    model_status: str | None = None
    updated_at: datetime


class SessionResponse(BaseModel):
    session_id: str
    default_branch_id: str
    status: str
    title: str | None = None
    root_task: str | None = None
    created_at: datetime
    updated_at: datetime
    selected_branch_id: str
    state: RuntimeStateResponse
    available_branches: list[SessionBranchInfo] = Field(default_factory=list)


class PostMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=12000)
    branch_id: str | None = None
    trigger_agents: bool = True


class MessageResponse(BaseModel):
    id: str
    session_id: str
    branch_id: str
    role: str
    actor: str
    content: str
    content_json: dict[str, Any] | None = None
    created_at: datetime
    parent_message_id: str | None = None
    checkpoint_id: str | None = None


class EventResponse(BaseModel):
    event_id: str
    session_id: str
    branch_id: str
    step_index: int
    actor: str
    action_type: str
    action_payload: dict[str, Any]
    observation_summary: str | None = None
    page_url: str | None = None
    screenshot_path: str | None = None
    created_at: datetime
    checkpoint_id: str | None = None
    browser_event_name: str | None = None
    correlation_id: str | None = None
    is_sensitive: bool = False


class ControlActionResponse(BaseModel):
    ok: bool
    run_status: str
    control_mode: str
    message: str
    lease_id: str | None = None


class ApproveRequest(BaseModel):
    branch_id: str | None = None
    correlation_id: str
    note: str | None = None


class RejectRequest(BaseModel):
    branch_id: str | None = None
    correlation_id: str
    reason: str | None = None


class BranchRequest(BaseModel):
    source_branch_id: str | None = None
    checkpoint_id: str | None = None
    name: str | None = None


class BranchResponse(BaseModel):
    session_id: str
    branch_id: str
    source_branch_id: str
    checkpoint_id: str
    name: str


class TimelineItemResponse(BaseModel):
    kind: str
    created_at: datetime
    actor: str | None = None
    action_type: str | None = None
    message_role: str | None = None
    content: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    step_index: int | None = None
    source_id: str


class ReplayResponse(BaseModel):
    session_id: str
    branch_id: str
    timeline: list[TimelineItemResponse]


class SnapshotRequest(BaseModel):
    branch_id: str | None = None
    note: str | None = None
    lease_id: str | None = None
    resume: bool = False


class CheckpointResponse(BaseModel):
    checkpoint_id: str
    session_id: str
    branch_id: str
    label: str
    reason: str
    step_index: int
    message_cursor: int
    event_cursor: int
    created_at: datetime


class ControlQuery(BaseModel):
    branch_id: str | None = None


class UIStatePollResponse(BaseModel):
    session: SessionResponse
    messages: list[MessageResponse]
    events: list[EventResponse]

