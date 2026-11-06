from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SessionORM(Base):
    __tablename__ = 'sessions'

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    root_task: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default='active', nullable=False)
    default_branch_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    branches = relationship('SessionBranchORM', back_populates='session', cascade='all, delete-orphan')


class SessionBranchORM(Base):
    __tablename__ = 'session_branches'

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey('sessions.id'), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default='main')
    parent_branch_id: Mapped[str | None] = mapped_column(ForeignKey('session_branches.id'), nullable=True)
    source_checkpoint_id: Mapped[str | None] = mapped_column(ForeignKey('checkpoints.id'), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_by_actor: Mapped[str] = mapped_column(String(32), nullable=False, default='system')
    lineage_depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    session = relationship('SessionORM', back_populates='branches')


class MessageORM(Base):
    __tablename__ = 'messages'

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey('sessions.id'), nullable=False, index=True)
    branch_id: Mapped[str] = mapped_column(ForeignKey('session_branches.id'), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    parent_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checkpoint_id: Mapped[str | None] = mapped_column(ForeignKey('checkpoints.id'), nullable=True)


class EventORM(Base):
    __tablename__ = 'events'

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey('sessions.id'), nullable=False, index=True)
    branch_id: Mapped[str] = mapped_column(ForeignKey('session_branches.id'), nullable=False, index=True)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    actor: Mapped[str] = mapped_column(String(32), nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    action_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    observation_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    screenshot_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    checkpoint_id: Mapped[str | None] = mapped_column(ForeignKey('checkpoints.id'), nullable=True)
    browser_event_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_event_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class CheckpointORM(Base):
    __tablename__ = 'checkpoints'

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey('sessions.id'), nullable=False, index=True)
    branch_id: Mapped[str] = mapped_column(ForeignKey('session_branches.id'), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message_cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    event_cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    created_by_actor: Mapped[str] = mapped_column(String(32), nullable=False, default='system')


class SessionRuntimeStateORM(Base):
    __tablename__ = 'session_runtime_state'
    __table_args__ = (
        PrimaryKeyConstraint('session_id', 'branch_id'),
    )

    session_id: Mapped[str] = mapped_column(ForeignKey('sessions.id'), nullable=False)
    branch_id: Mapped[str] = mapped_column(ForeignKey('session_branches.id'), nullable=False)
    run_status: Mapped[str] = mapped_column(String(64), nullable=False, default='idle')
    control_mode: Mapped[str] = mapped_column(String(32), nullable=False, default='agent')
    control_holder: Mapped[str] = mapped_column(String(32), nullable=False, default='none')
    mutex_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pause_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approval_pending_action: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    approval_correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_known_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_known_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    tabs_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    latest_observation: Mapped[str | None] = mapped_column(Text, nullable=True)
    pending_action_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_plan_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    blackboard_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    pending_user_instructions_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    browser_profile_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    browser_session_meta_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    browser_restore_mode: Mapped[str] = mapped_column(String(32), nullable=False, default='none')
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class ApprovalDecisionORM(Base):
    __tablename__ = 'approval_decisions'

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey('sessions.id'), nullable=False, index=True)
    branch_id: Mapped[str] = mapped_column(ForeignKey('session_branches.id'), nullable=False, index=True)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default='pending')
    requested_action_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class TaskGroupORM(Base):
    __tablename__ = 'task_groups'

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str | None] = mapped_column(String(160), nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class TaskGroupMemberORM(Base):
    __tablename__ = 'task_group_members'
    __table_args__ = (PrimaryKeyConstraint('group_id', 'position'),)

    group_id: Mapped[str] = mapped_column(ForeignKey('task_groups.id'), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    session_id: Mapped[str] = mapped_column(ForeignKey('sessions.id'), nullable=False)
    branch_id: Mapped[str] = mapped_column(ForeignKey('session_branches.id'), nullable=False)


Index('ix_events_session_branch_created', EventORM.session_id, EventORM.branch_id, EventORM.created_at)
Index('ix_messages_session_branch_created', MessageORM.session_id, MessageORM.branch_id, MessageORM.created_at)
Index('ix_checkpoint_session_branch_created', CheckpointORM.session_id, CheckpointORM.branch_id, CheckpointORM.created_at)

