from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.models.domain import CheckpointSpec, Subtask, TaskPlan


class PlannerAgent:
    async def create_plan(self, *, user_request: str, prior_findings: list[dict] | None = None) -> TaskPlan:
        prior_findings = prior_findings or []
        text = user_request.strip()
        if 'laptop' in text.lower() and 'deal' in text.lower():
            subtasks = [
                Subtask(id=str(uuid4()), title='Search laptop deals', goal='Find candidate laptop deals under budget'),
                Subtask(id=str(uuid4()), title='Open candidate listings', goal='Open pages and collect price/spec data'),
                Subtask(id=str(uuid4()), title='Compare shortlisted options', goal='Compare 3 options and summarize tradeoffs'),
            ]
        else:
            subtasks = [
                Subtask(id=str(uuid4()), title='Understand task', goal=text or 'Interpret the user request'),
                Subtask(id=str(uuid4()), title='Browse for evidence', goal='Find relevant pages and collect evidence'),
                Subtask(id=str(uuid4()), title='Summarize results', goal='Prepare a concise answer'),
            ]
        checkpoints = [CheckpointSpec(label=f'Checkpoint {i+1}: {s.title}', reason='planner_checkpoint', subtask_id=s.id) for i, s in enumerate(subtasks)]
        return TaskPlan(
            goal=text,
            subtasks=subtasks,
            checkpoints=checkpoints,
            extraction_schema={'type': 'comparison', 'fields': ['name', 'price', 'url', 'pros', 'cons']},
            stop_criteria=['At least 3 candidates collected', 'Comparison summary prepared'],
            created_at=datetime.now(timezone.utc),
        )

