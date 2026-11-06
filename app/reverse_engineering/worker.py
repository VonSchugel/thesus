from __future__ import annotations

import sys

from app.reverse_engineering.contracts import EngineFailure, MAX_ARTIFACT_BYTES, MAX_REPORT_BYTES


def main() -> None:
    from app.reverse_engineering.engines import analyze

    artifact = sys.stdin.buffer.read(MAX_ARTIFACT_BYTES + 1)
    if not artifact or len(artifact) > MAX_ARTIFACT_BYTES:
        sys.stdout.buffer.write(b'input_limit\n')
        return
    try:
        result = analyze(artifact, sys.argv[2], sys.argv[3]).model_dump_json().encode('utf-8')
        if len(result) + 1 > MAX_REPORT_BYTES:
            raise EngineFailure('output_limit')
    except EngineFailure as error:
        sys.stdout.buffer.write(error.code.encode('ascii') + b'\n')
    except (ImportError, OSError):
        sys.stdout.buffer.write(b'engine_unavailable\n')
    except Exception:
        sys.stdout.buffer.write(b'engine_error\n')
    else:
        sys.stdout.buffer.write(b'\n' + result)
