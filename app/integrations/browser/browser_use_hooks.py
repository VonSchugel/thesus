from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class EventLoggerHook:
    on_planned_action: Any | None = None
    on_browser_event: Any | None = None
    on_done: Any | None = None

