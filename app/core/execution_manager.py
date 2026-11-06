from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable


class ExecutionManager:
    def __init__(self, worker: Callable[[str, str], Awaitable[None]], *, rerun_when_busy: bool = False) -> None:
        self._worker = worker
        self._rerun_when_busy = rerun_when_busy
        self._pending: set[tuple[str, str]] = set()
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._log = logging.getLogger(__name__)

    async def start(self) -> None:
        async with self._lock:
            if any(not task.done() for task in self._tasks.values()):
                raise RuntimeError('Cannot start execution while previous workers are draining')
            self._closed = False

    async def schedule(self, session_id: str, branch_id: str) -> bool:
        async with self._lock:
            return self._schedule((session_id, branch_id), rerun=self._rerun_when_busy)

    async def schedule_many(self, branches: Iterable[tuple[str, str]]) -> None:
        async with self._lock:
            for key in branches:
                self._schedule(key, rerun=False)

    def _schedule(self, key: tuple[str, str], *, rerun: bool) -> bool:
        if self._closed:
            return False
        task = self._tasks.get(key)
        if task and not task.done():
            if rerun:
                self._pending.add(key)
            return rerun
        self._tasks[key] = asyncio.create_task(self._run(key), name=f'branch-worker:{key[0]}:{key[1]}')
        return True

    async def _run(self, key: tuple[str, str]) -> None:
        session_id, branch_id = key
        try:
            while True:
                try:
                    await self._worker(session_id, branch_id)
                except Exception:
                    self._log.exception('Branch worker crashed', extra={'extra_data': {'session_id': session_id, 'branch_id': branch_id}})
                async with self._lock:
                    if key not in self._pending:
                        self._tasks.pop(key, None)
                        return
                    self._pending.remove(key)
        except asyncio.CancelledError:
            async with self._lock:
                if self._tasks.get(key) is asyncio.current_task():
                    self._tasks.pop(key, None)
                    self._pending.discard(key)
            raise

    async def shutdown(self) -> None:
        async with self._lock:
            tasks = list(self._tasks.items())
            if not self._closed:
                self._closed = True
                self._pending.clear()
                for _, task in tasks:
                    task.cancel()
        if tasks:
            await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
            async with self._lock:
                for key, task in tasks:
                    if self._tasks.get(key) is task:
                        self._tasks.pop(key)

    async def wait_for_idle(self, timeout: float = 5.0) -> None:
        end = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < end:
            async with self._lock:
                if not any(not t.done() for t in self._tasks.values()):
                    return
            await asyncio.sleep(0.01)
        raise TimeoutError('ExecutionManager did not become idle in time')

