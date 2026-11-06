from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from pydantic import ValidationError

from app.reverse_engineering.contracts import EngineFailure, EngineReport, MAX_REPORT_BYTES, Plan


TIMEOUT_SECONDS = 12
ERRORS = {
    'input_limit': 'The artifact exceeds the analysis input limits.',
    'output_limit': 'The analysis exceeded its evidence size limit.',
    'engine_unavailable': 'The selected engine is unavailable. Check the analysis dependencies.',
    'engine_error': 'The analysis engine could not process this artifact.',
    'invalid_artifact': 'The artifact is malformed or has an unsupported format variant.',
    'unsupported_artifact': 'The artifact uses an unsupported format variant.',
    'analysis_timeout': 'The analysis exceeded its time limit.',
    'backend_changed': 'The pinned backend is unavailable or changed. Create a new investigation.',
}
RETRYABLE_ERRORS = frozenset({'analysis_timeout', 'engine_error', 'engine_unavailable'})
_BOOT = 'import sys; sys.path.insert(0, sys.argv[1]); from app.reverse_engineering.worker import main; main()'


async def inspect_artifact(plan: Plan, artifact: bytes) -> EngineReport:
    root = str(Path(__file__).resolve().parents[2])
    env = {key: value for key, value in os.environ.items() if key.upper() in {'SYSTEMROOT', 'WINDIR'}}
    process: asyncio.subprocess.Process | None = None
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            process = await asyncio.create_subprocess_exec(
                sys.executable, '-I', '-B', '-c', _BOOT, root, plan.target, plan.mode,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
            assert process.stdin is not None and process.stdout is not None
            try:
                process.stdin.write(artifact)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()
            try:
                await process.stdout.readexactly(MAX_REPORT_BYTES + 1)
            except asyncio.IncompleteReadError as incomplete:
                payload = incomplete.partial
            else:
                raise EngineFailure('output_limit')
            if await process.wait() != 0:
                raise EngineFailure('engine_error')
    except TimeoutError as exc:
        raise EngineFailure('analysis_timeout') from exc
    except OSError as exc:
        raise EngineFailure('engine_unavailable') from exc
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await asyncio.gather(process.wait(), process.stdout.read())

    code, separator, report = payload.partition(b'\n')
    if not separator or code:
        message = code.decode('ascii', errors='replace')
        raise EngineFailure(message if message in ERRORS else 'engine_error')
    try:
        result = EngineReport.model_validate_json(report)
    except ValidationError as exc:
        raise EngineFailure('engine_error') from exc
    if any(item.locator.member is None and item.locator.offset is not None
           and item.locator.offset + item.locator.length > len(artifact) for item in result.observations):
        raise EngineFailure('engine_error')
    return result
