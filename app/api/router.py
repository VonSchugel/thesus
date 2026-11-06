from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.api import routes_control, routes_events, routes_messages, routes_replay, routes_sessions, routes_task_groups, routes_workspace

router = APIRouter()
router.include_router(routes_sessions.router)
router.include_router(routes_messages.router)
router.include_router(routes_events.router)
router.include_router(routes_control.router)
router.include_router(routes_replay.router)
router.include_router(routes_workspace.router)
router.include_router(routes_task_groups.router)

_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / 'ui' / 'templates'))


@router.get('/', response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request):
    return _templates.TemplateResponse(request=request, name='index.html')

