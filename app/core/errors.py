from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ErrorClassification:
    code: str
    category: str
    message: str
    http_status: int = 500
    retryable: bool = False
    recovery_options: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_response(self, *, include_raw_detail: str | None = None, error_type: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "detail": include_raw_detail or self.message,
            "classified_message": self.message,
            "error_code": self.code,
            "error_category": self.category,
            "retryable": self.retryable,
            "recovery_options": self.recovery_options,
        }
        if error_type:
            payload["error_type"] = error_type
        if self.details:
            payload["error_details"] = self.details
        return payload


class AppError(Exception):
    status_code = 400
    error_code = "app_error"
    error_category = "application"
    retryable = False
    recovery_options: tuple[str, ...] = ()

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        error_category: str | None = None,
        retryable: bool | None = None,
        recovery_options: list[str] | tuple[str, ...] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        if error_code is not None:
            self.error_code = error_code
        if error_category is not None:
            self.error_category = error_category
        if retryable is not None:
            self.retryable = retryable
        if recovery_options is not None:
            self.recovery_options = tuple(recovery_options)
        self.details = details or {}

    def classify(self) -> ErrorClassification:
        return ErrorClassification(
            code=self.error_code,
            category=self.error_category,
            message=str(self),
            http_status=self.status_code,
            retryable=bool(self.retryable),
            recovery_options=list(self.recovery_options),
            details=dict(self.details),
        )


class NotFoundError(AppError):
    status_code = 404
    error_code = "not_found"
    error_category = "resource"
    recovery_options = ("verify_id", "refresh", "create_new")


class ConflictError(AppError):
    status_code = 409
    error_code = "state_conflict"
    error_category = "control_state"
    recovery_options = ("refresh_state", "pause", "resume", "release_takeover")


class ValidationError(AppError):
    status_code = 422
    error_code = "invalid_request"
    error_category = "request"
    recovery_options = ("fix_input", "retry")


def _contains_any(text: str, tokens: list[str] | tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def classify_exception(exc: Exception) -> ErrorClassification:
    if isinstance(exc, AppError):
        return exc.classify()

    msg = str(exc)
    msg_l = msg.lower()
    exc_type = type(exc).__name__

    # Common direct-testing failures first (real browser + OpenAI path).
    if "openai_api_key" in msg_l:
        return ErrorClassification(
            code="missing_openai_api_key",
            category="configuration",
            message="OPENAI_API_KEY is missing for OpenAI browser-use mode.",
            http_status=500,
            retryable=False,
            recovery_options=["set_env_openai_api_key", "restart_app"],
        )
    if "unsupported browser_use_llm_provider" in msg_l:
        return ErrorClassification(
            code="invalid_llm_provider",
            category="configuration",
            message="Invalid browser-use LLM provider configuration.",
            http_status=500,
            retryable=False,
            recovery_options=["set_browser_use_llm_provider_to_openai_or_ollama", "restart_app"],
        )
    if "browser-use import failed" in msg_l:
        return ErrorClassification(
            code="browser_use_missing",
            category="dependency",
            message="browser-use is not importable in the current environment.",
            http_status=500,
            retryable=False,
            recovery_options=["pip_install_browser_use", "restart_app"],
        )
    if "playwright" in msg_l and _contains_any(msg_l, ["executable", "install", "chromium"]):
        return ErrorClassification(
            code="playwright_browser_missing",
            category="dependency",
            message="Playwright browser binaries are missing for browser automation.",
            http_status=500,
            retryable=False,
            recovery_options=["run_playwright_install_chromium", "retry"],
        )
    if "ollama" in msg_l and _contains_any(msg_l, ["connection", "refused", "unavailable"]):
        return ErrorClassification(
            code="ollama_unreachable",
            category="llm_provider",
            message="Ollama is not reachable for browser-use navigation.",
            http_status=502,
            retryable=True,
            recovery_options=["start_ollama", "verify_ollama_host", "retry"],
        )

    # Timeouts / cancellation boundaries
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return ErrorClassification(
            code="timeout",
            category="timeout",
            message="Operation timed out.",
            http_status=504,
            retryable=True,
            recovery_options=["retry", "manual_takeover", "reduce_scope"],
        )

    # Optional provider/network libraries (import guarded)
    try:
        import httpx  # type: ignore

        if isinstance(exc, httpx.TimeoutException):
            return ErrorClassification(
                code="network_timeout",
                category="network",
                message="Network request timed out.",
                http_status=504,
                retryable=True,
                recovery_options=["retry", "check_network", "manual_takeover"],
            )
        if isinstance(exc, httpx.ConnectError):
            return ErrorClassification(
                code="network_connect_error",
                category="network",
                message="Unable to connect to an upstream service.",
                http_status=502,
                retryable=True,
                recovery_options=["check_network", "verify_service_running", "retry"],
            )
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status in (401, 403):
                return ErrorClassification(
                    code="upstream_auth_error",
                    category="authentication",
                    message="Upstream service rejected authentication credentials.",
                    http_status=502,
                    retryable=False,
                    recovery_options=["verify_api_key", "verify_permissions"],
                    details={"upstream_status": status},
                )
            if status == 429:
                return ErrorClassification(
                    code="upstream_rate_limited",
                    category="rate_limit",
                    message="Upstream service rate-limited the request.",
                    http_status=429,
                    retryable=True,
                    recovery_options=["wait_and_retry", "switch_model", "reduce_request_rate"],
                    details={"upstream_status": status},
                )
    except Exception:
        pass

    try:
        from openai import APIConnectionError, APIStatusError, AuthenticationError, RateLimitError  # type: ignore

        if isinstance(exc, AuthenticationError):
            return ErrorClassification(
                code="openai_auth_error",
                category="authentication",
                message="OpenAI authentication failed.",
                http_status=502,
                retryable=False,
                recovery_options=["verify_openai_api_key", "verify_openai_base_url"],
            )
        if isinstance(exc, RateLimitError):
            return ErrorClassification(
                code="openai_rate_limited",
                category="rate_limit",
                message="OpenAI rate limit reached.",
                http_status=429,
                retryable=True,
                recovery_options=["wait_and_retry", "switch_model", "reduce_request_rate"],
            )
        if isinstance(exc, APIConnectionError):
            return ErrorClassification(
                code="openai_connection_error",
                category="network",
                message="Failed to connect to OpenAI.",
                http_status=502,
                retryable=True,
                recovery_options=["check_network", "verify_openai_base_url", "retry"],
            )
        if isinstance(exc, APIStatusError):
            status = getattr(exc, "status_code", None)
            return ErrorClassification(
                code="openai_api_status_error",
                category="llm_provider",
                message="OpenAI returned an API error.",
                http_status=502,
                retryable=bool(status and status >= 500),
                recovery_options=["retry", "switch_model", "manual_takeover"],
                details={"upstream_status": status},
            )
    except Exception:
        pass

    try:
        from sqlalchemy.exc import IntegrityError, OperationalError  # type: ignore

        if isinstance(exc, IntegrityError):
            return ErrorClassification(
                code="db_integrity_error",
                category="database",
                message="Database integrity constraint failed.",
                http_status=500,
                retryable=False,
                recovery_options=["inspect_data", "retry_with_new_ids"],
            )
        if isinstance(exc, OperationalError):
            if "locked" in msg_l:
                return ErrorClassification(
                    code="db_locked",
                    category="database",
                    message="SQLite database is locked.",
                    http_status=503,
                    retryable=True,
                    recovery_options=["retry", "reduce_parallel_writes"],
                )
            return ErrorClassification(
                code="db_operational_error",
                category="database",
                message="Database operational error.",
                http_status=500,
                retryable=True,
                recovery_options=["retry", "inspect_db_file"],
            )
    except Exception:
        pass

    # Generic message heuristics
    if _contains_any(msg_l, ["permission denied", "access is denied", "accès refusé"]):
        return ErrorClassification(
            code="filesystem_permission_error",
            category="filesystem",
            message="Filesystem permission error.",
            http_status=500,
            retryable=False,
            recovery_options=["check_file_permissions", "change_output_dir"],
        )
    if _contains_any(msg_l, ["connection refused", "name or service not known", "temporary failure in name resolution"]):
        return ErrorClassification(
            code="network_unreachable",
            category="network",
            message="Network/service is unreachable.",
            http_status=502,
            retryable=True,
            recovery_options=["check_network", "verify_service_running", "retry"],
        )
    if _contains_any(msg_l, ["no module named", "modulenotfounderror"]):
        return ErrorClassification(
            code="missing_python_dependency",
            category="dependency",
            message="A required Python dependency is missing.",
            http_status=500,
            retryable=False,
            recovery_options=["pip_install_missing_package", "restart_app"],
        )

    return ErrorClassification(
        code="internal_error",
        category="internal",
        message="Unhandled internal error.",
        http_status=500,
        retryable=False,
        recovery_options=["retry", "manual_takeover", "inspect_logs"],
        details={"exception_type": exc_type},
    )
