from __future__ import annotations

from app.models.enums import ControlHolder, ControlMode, RunStatus


RUNNABLE_STATUSES = {RunStatus.IDLE.value, RunStatus.PAUSED.value, RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.INTERRUPTED.value}
IN_PROGRESS_STATUSES = {RunStatus.PLANNING.value, RunStatus.NAVIGATING.value, RunStatus.EXTRACTING.value}


def can_agent_act(state: object) -> bool:
    return (
        getattr(state, 'control_mode', None) == ControlMode.AGENT.value
        and getattr(state, 'control_holder', None) == ControlHolder.AGENT.value
        and not getattr(state, 'pause_requested', False)
        and not getattr(state, 'approval_required', False)
    )

