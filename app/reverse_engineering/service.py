from __future__ import annotations

import asyncio
import hashlib
import json
import platform
from collections import defaultdict
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, JSON, LargeBinary, String, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column, undefer

from app.core.errors import AppError, ConflictError, NotFoundError
from app.core.execution_manager import ExecutionManager
from app.db.base import Base
from app.db.repo_sessions import SessionRepository
from app.reverse_engineering.contracts import (
    AuditEvent, Authorization, Comparison, EngineFailure, EngineReport, ExchangeDelta,
    Investigation, Mode, Plan, RequestedTarget, Target,
)
from app.reverse_engineering.execution import RETRYABLE_ERRORS, inspect_artifact


_MAX_RUNS = 48
_MAX_ATTEMPTS = 4
_RESUME_AUDIT_LIMIT = 50
_BACKEND_REVISION = '1'
_ENGINE_PACKAGES = {
    'pe': ('pefile',),
    'elf': ('pyelftools',),
    'zip': (),
    'javascript': (),
    'har': (),
}
_QUESTIONS = {
    'pe': 'What image layout and entry bytes can be verified?',
    'elf': 'What image layout and entry bytes can be verified?',
    'zip': 'Which members are present, and which literal JavaScript links can be observed?',
    'javascript': 'Which literal imports and IPC endpoints can be located?',
    'har': 'Which request method, path digest, and status combinations were recorded?',
}


class InvestigationRow(Base):
    __tablename__ = 'reverse_engineering_investigations'

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey('sessions.id'), index=True)
    artifact_sha256: Mapped[str] = mapped_column(String(64))
    artifact_size: Mapped[int]
    artifact: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    authorization: Mapped[str] = mapped_column(String(12))
    review: Mapped[bool]
    status: Mapped[str] = mapped_column(String(32), index=True)
    plan: Mapped[dict] = mapped_column(JSON)
    attempt: Mapped[int] = mapped_column(default=0)
    evidence_id: Mapped[str | None] = mapped_column(String(64))
    report: Mapped[dict | None] = mapped_column(JSON, deferred=True)
    error: Mapped[str | None] = mapped_column(String(32))
    audit: Mapped[list[dict]] = mapped_column(JSON)


def _record(row: InvestigationRow, action: str, code: str | None = None) -> None:
    event = AuditEvent(at=datetime.now(timezone.utc), action=action, attempt=row.attempt, code=code)
    row.audit = [*row.audit, event.model_dump(mode='json')]


def _view(row: InvestigationRow, *, include_report: bool = True) -> Investigation:
    return Investigation(
        id=row.id, session_id=row.session_id, artifact_sha256=row.artifact_sha256, artifact_size=row.artifact_size,
        authorization=row.authorization, review=row.review, status=row.status, plan=row.plan, attempt=row.attempt,
        evidence_id=row.evidence_id, report=row.report if include_report else None, error=row.error, audit=row.audit,
    )


def _target(artifact: bytes, requested: RequestedTarget) -> Target:
    detected: Target | None = None
    if artifact.startswith(b'MZ'):
        detected = 'pe'
    elif artifact.startswith(b'\x7fELF'):
        detected = 'elf'
    elif artifact.startswith((b'PK\x03\x04', b'PK\x05\x06')):
        detected = 'zip'
    elif artifact.removeprefix(b'\xef\xbb\xbf').lstrip().startswith(b'{'):
        detected = 'har'
    if requested == 'javascript' and detected in (None, 'har'):
        return requested
    if detected is None or requested not in ('auto', detected):
        raise AppError('Select a supported target or supply an artifact with a matching format signature.',
                       status_code=422, error_code='unsupported_artifact')
    return detected


def _plan(target: Target, mode: Mode) -> Plan:
    packages = _ENGINE_PACKAGES[target]
    if mode == 'inspect' and target in ('pe', 'elf'):
        packages += ('capstone',)
    if mode == 'inspect' and target in ('javascript', 'zip'):
        packages += ('tree-sitter', 'tree-sitter-javascript')
    versions = {name: version(name) for name in packages}
    versions['python'] = platform.python_version()
    return Plan(target=target, mode=mode, backend=f'thesus.{target}/{_BACKEND_REVISION}',
                versions=versions, question=_QUESTIONS[target])


def _result_id(sha256: str, plan: Plan, report: EngineReport) -> str:
    body = [sha256, plan.model_dump(mode='json'), report.model_dump(mode='json')]
    canonical = json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('ascii')).hexdigest()


def _exchanges(report: EngineReport) -> tuple[dict[tuple[str, str, int], list[int]], bool]:
    exchanges = defaultdict(list)
    coverage = None
    for item in report.observations:
        if item.key == 'har_coverage':
            coverage = item.value
        if item.key == 'http_exchange' and isinstance(item.value, dict) and item.locator.record is not None:
            fields = item.value
            exchanges[(fields['method'], fields['path_sha256'], fields['status'])].append(item.locator.record)
    count = sum(map(len, exchanges.values()))
    return exchanges, isinstance(coverage, dict) and count > 0 and coverage == {'total': count, 'observed': count}


def _difference(left: dict, right: dict) -> list[ExchangeDelta]:
    return [
        ExchangeDelta(method=key[0], path_sha256=key[1], status=key[2], records=left[key][overlap:])
        for key in sorted(left)
        if (overlap := len(right.get(key, ()))) < len(left[key])
    ]


class Investigations:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.repository = SessionRepository()
        self.manager = ExecutionManager(self._process, rerun_when_busy=True)
        self.upload_slots = asyncio.Semaphore(2)
        self._worker_slots = asyncio.Semaphore(2)
        self._transitions = asyncio.Lock()
        self._active: dict[str, asyncio.Task[EngineReport]] = {}

    async def start(self) -> None:
        async with self.sessions() as db:
            rows = (await db.scalars(select(InvestigationRow).where(
                InvestigationRow.status.in_(('queued', 'running'))
            ))).all()
            queued = []
            for row in rows:
                if row.status == 'queued':
                    queued.append((row.session_id, row.id))
                else:
                    row.status = 'interrupted'
                    _record(row, 'recovered_interruption')
            await db.commit()
        await self.manager.start()
        await self.manager.schedule_many(queued)

    async def _session(self, db: AsyncSession, session_id: UUID) -> None:
        if not await self.repository.get_session(db, str(session_id)):
            raise NotFoundError('Session not found.')

    async def _row(
        self, db: AsyncSession, session_id: str, run_id: str, *, artifact: bool = False,
    ) -> InvestigationRow | None:
        query = select(InvestigationRow).where(InvestigationRow.id == run_id, InvestigationRow.session_id == session_id)
        return await db.scalar(query.options(undefer(InvestigationRow.artifact if artifact else InvestigationRow.report)))

    async def submit(
        self, session_id: UUID, artifact: bytes, requested: RequestedTarget, mode: Mode,
        authorization: Authorization, review: bool, run_id: UUID | None,
    ) -> Investigation:
        key = str(run_id or uuid4())
        digest = hashlib.sha256(artifact).hexdigest()
        target = _target(artifact, requested)
        async with self._transitions, self.sessions() as db:
            await self._session(db, session_id)
            row = await self._row(db, str(session_id), key)
            if row is None:
                if await db.get(InvestigationRow, key) is not None:
                    raise ConflictError('This investigation ID is unavailable.')
                if await db.scalar(select(func.count()).select_from(InvestigationRow)) >= _MAX_RUNS:
                    raise AppError('Investigation storage is full. Remove a finished investigation before adding another.',
                                   status_code=429, error_code='investigation_capacity')
                try:
                    plan = _plan(target, mode)
                except PackageNotFoundError as exc:
                    raise AppError('Install the optional analysis dependencies for this backend.',
                                   status_code=503, error_code='engine_unavailable') from exc
                row = InvestigationRow(
                    id=key, session_id=str(session_id), artifact_sha256=digest, artifact_size=len(artifact),
                    artifact=artifact, authorization=authorization, review=review,
                    status='waiting_for_review' if review else 'queued',
                    plan=plan.model_dump(mode='json'), attempt=0, audit=[], report=None,
                )
                _record(row, 'admitted')
                _record(row, 'plan_checkpoint')
                db.add(row)
                await db.commit()
            elif (row.artifact_sha256, row.plan['target'], row.plan['mode'], row.authorization, row.review) != (
                digest, target, mode, authorization, review,
            ):
                raise ConflictError('This investigation ID already has a different artifact or plan.')
            view = _view(row)
        if view.status == 'queued':
            await self.manager.schedule(str(session_id), key)
        return view

    async def get(self, session_id: UUID, run_id: UUID) -> Investigation:
        async with self.sessions() as db:
            row = await self._row(db, str(session_id), str(run_id))
            if row is None:
                raise NotFoundError('Investigation not found in this session.')
            return _view(row)

    async def list(self, session_id: UUID) -> list[Investigation]:
        async with self.sessions() as db:
            await self._session(db, session_id)
            rows = (await db.scalars(select(InvestigationRow).where(
                InvestigationRow.session_id == str(session_id),
            ).order_by(InvestigationRow.id))).all()
            return [_view(row, include_report=False) for row in rows]

    async def control(self, session_id: UUID, run_id: UUID, action: str) -> Investigation:
        async with self._transitions, self.sessions() as db:
            row = await self._row(db, str(session_id), str(run_id))
            if row is None:
                raise NotFoundError('Investigation not found in this session.')
            if action == 'cancel':
                if row.status in ('waiting_for_review', 'queued', 'running'):
                    row.status = 'cancelled'
                    _record(row, 'cancelled')
                    await db.commit()
                    task = self._active.get(row.id)
                    if task is not None:
                        task.cancel()
                elif row.status != 'cancelled':
                    raise ConflictError('Only pending or running investigations can be cancelled.')
            elif row.status != 'queued':
                can_resume = row.status in ('waiting_for_review', 'cancelled', 'interrupted') or (
                    row.status == 'failed' and row.error in RETRYABLE_ERRORS
                )
                if not can_resume or row.attempt >= _MAX_ATTEMPTS or len(row.audit) >= _RESUME_AUDIT_LIMIT:
                    raise ConflictError('This investigation cannot be resumed.')
                row.status, row.error = 'queued', None
                _record(row, 'resumed')
                await db.commit()
            view = _view(row)
        if action == 'resume':
            await self.manager.schedule(str(session_id), str(run_id))
        return view

    async def remove(self, session_id: UUID, run_id: UUID) -> None:
        async with self._transitions, self.sessions() as db:
            row = await self._row(db, str(session_id), str(run_id))
            if row is None:
                raise NotFoundError('Investigation not found in this session.')
            if row.status in ('queued', 'running') or row.id in self._active:
                raise ConflictError('Cancel the investigation and wait for its worker to finish before removing it.')
            await db.delete(row)
            await db.commit()

    async def compare(self, session_id: UUID, run_id: UUID, other_id: UUID) -> Comparison:
        async with self.sessions() as db:
            left = await self._row(db, str(session_id), str(run_id))
            right = await self._row(db, str(session_id), str(other_id))
            if left is None or right is None:
                raise NotFoundError('Both investigations must belong to this session.')
            if any(row.status != 'succeeded' or row.plan['target'] != 'har' for row in (left, right)):
                raise ConflictError('Comparison requires two successful HTTP capture investigations.')
            left_id, right_id = left.evidence_id, right.evidence_id
            left_report, right_report = EngineReport.model_validate(left.report), EngineReport.model_validate(right.report)
        left_set, left_complete = _exchanges(left_report)
        right_set, right_complete = _exchanges(right_report)
        left_only, right_only = _difference(left_set, right_set), _difference(right_set, left_set)
        result = 'inconclusive'
        if left_complete and right_complete:
            result = 'difference' if left_only or right_only else 'match'
        return Comparison(left_evidence_id=left_id, right_evidence_id=right_id, result=result,
                          left_only=left_only, right_only=right_only)

    async def _process(self, session_id: str, run_id: str) -> None:
        async with self._worker_slots:
            await self._inspect(session_id, run_id)

    async def _inspect(self, session_id: str, run_id: str) -> None:
        async with self._transitions, self.sessions() as db:
            row = await self._row(db, session_id, run_id, artifact=True)
            if row is None or row.status != 'queued':
                return
            plan, artifact, digest = Plan.model_validate(row.plan), row.artifact, row.artifact_sha256
            row.status = 'running'
            row.attempt += 1
            attempt = row.attempt
            _record(row, 'started')
            await db.commit()
            task = self._active[run_id] = asyncio.create_task(self._invoke(plan, artifact, digest))

        report, error = None, None
        try:
            report = await asyncio.shield(task)
        except EngineFailure as failure:
            error = failure.code
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                if not task.done() and not task.cancelling():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            return
        finally:
            self._active.pop(run_id, None)

        async with self._transitions, self.sessions() as db:
            row = await self._row(db, session_id, run_id)
            if row is None or row.attempt != attempt or row.status != 'running':
                return
            if report is not None:
                row.status = 'succeeded'
                row.report = report.model_dump(mode='json')
                row.evidence_id = _result_id(digest, plan, report)
            else:
                row.status, row.error = 'failed', error
            _record(row, row.status, error)
            await db.commit()

    async def _invoke(self, plan: Plan, artifact: bytes, digest: str) -> EngineReport:
        if hashlib.sha256(artifact).hexdigest() != digest:
            raise EngineFailure('invalid_artifact')
        try:
            current = _plan(plan.target, plan.mode)
        except PackageNotFoundError as exc:
            raise EngineFailure('backend_changed') from exc
        if current != plan:
            raise EngineFailure('backend_changed')
        return await inspect_artifact(plan, artifact)
