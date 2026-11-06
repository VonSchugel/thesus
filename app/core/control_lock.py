from __future__ import annotations

import asyncio
from collections import defaultdict


class ControlLockRegistry:
    def __init__(self) -> None:
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def get(self, session_id: str, branch_id: str) -> asyncio.Lock:
        key = (session_id, branch_id)
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

