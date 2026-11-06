from __future__ import annotations

from fastapi import Request

from app.core.orchestrator import OrchestratorService


def get_services(request: Request) -> OrchestratorService:
    return request.app.state.services

