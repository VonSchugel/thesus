from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import ActionType, Actor


class TabState(BaseModel):
    url: str
    title: str = ''
    tab_id: str | None = None


class BrowserLiveState(BaseModel):
    url: str | None = None
    title: str | None = None
    tabs: list[TabState] = Field(default_factory=list)
    latest_observation: str | None = None
    screenshot_path: str | None = None
    active_tab_id: str | None = None
    revision: str | None = None
    width: int = 1280
    height: int = 800
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


class PlannedAction(BaseModel):
    action_type: ActionType
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    page_url: str | None = None
    sensitive: bool = False
    correlation_id: str | None = None


class NavigationStepResult(BaseModel):
    planned_action: PlannedAction
    executed: bool = True
    observation_summary: str | None = None
    page_url: str | None = None
    screenshot_path: str | None = None
    raw_event: dict[str, Any] | None = None
    completed_subtask: bool = False
    error: str | None = None
    live_state: BrowserLiveState | None = None


class SnapshotResult(BaseModel):
    live_state: BrowserLiveState
    metadata: dict[str, Any] = Field(default_factory=dict)


class CheckpointSpec(BaseModel):
    label: str
    reason: str
    subtask_id: str | None = None


class Subtask(BaseModel):
    id: str
    title: str
    goal: str
    status: str = 'pending'
    notes: list[str] = Field(default_factory=list)


class TaskPlan(BaseModel):
    goal: str
    subtasks: list[Subtask]
    checkpoints: list[CheckpointSpec] = Field(default_factory=list)
    extraction_schema: dict[str, Any] = Field(default_factory=dict)
    stop_criteria: list[str] = Field(default_factory=list)
    created_at: datetime | None = None


class TimelineItem(BaseModel):
    kind: str
    created_at: datetime
    actor: str | None = None
    action_type: str | None = None
    message_role: str | None = None
    content: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    step_index: int | None = None
    source_id: str


class SessionContext(BaseModel):
    session_id: str
    branch_id: str


class AgentMessage(BaseModel):
    actor: Actor
    content: str
    content_json: dict[str, Any] | None = None

