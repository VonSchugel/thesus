from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_REPORT_BYTES = 512 * 1024
MAX_OBSERVATIONS = 160
Target = Literal['pe', 'elf', 'zip', 'javascript', 'har']
RequestedTarget = Literal['auto', 'pe', 'elf', 'zip', 'javascript', 'har']
Mode = Literal['inventory', 'inspect']
Authorization = Literal['owned', 'authorized']
Status = Literal['waiting_for_review', 'queued', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted']


class EngineFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Locator(BaseModel):
    """Zero-based file byte range, archive member index, or capture record index."""

    model_config = ConfigDict(extra='forbid')

    offset: int | None = Field(default=None, ge=0)
    length: int | None = Field(default=None, ge=1)
    member: int | None = Field(default=None, ge=0)
    record: int | None = Field(default=None, ge=0)

    @model_validator(mode='after')
    def validate_coordinates(self) -> Locator:
        if (self.offset is None) != (self.length is None):
            raise ValueError('A byte range requires both offset and length')
        if self.record is not None and (self.member is not None or self.offset is not None):
            raise ValueError('A capture record is a separate coordinate space')
        return self


class Observation(BaseModel):
    model_config = ConfigDict(extra='forbid')

    key: str = Field(min_length=1, max_length=80)
    value: JsonValue
    locator: Locator


class Inference(BaseModel):
    model_config = ConfigDict(extra='forbid')

    text: str = Field(min_length=1, max_length=1000)
    observations: list[int] = Field(min_length=1, max_length=32)


class EngineReport(BaseModel):
    model_config = ConfigDict(extra='forbid')

    observations: list[Observation] = Field(default_factory=list, max_length=MAX_OBSERVATIONS)
    inferences: list[Inference] = Field(default_factory=list, max_length=32)
    limitations: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode='after')
    def validate_references(self) -> EngineReport:
        if any(index < 0 or index >= len(self.observations) for item in self.inferences for index in item.observations):
            raise ValueError('Every inference must cite observations in its own report')
        if any(len(item) > 500 for item in self.limitations):
            raise ValueError('A limitation exceeds the text budget')
        return self


class Plan(BaseModel):
    schema_version: Literal[1] = 1
    target: Target
    mode: Mode
    backend: str
    versions: dict[str, str]
    question: str


class AuditEvent(BaseModel):
    at: datetime
    action: str
    attempt: int
    code: str | None = None


class Investigation(BaseModel):
    id: UUID
    session_id: UUID
    artifact_sha256: str
    artifact_size: int
    authorization: Authorization
    review: bool
    status: Status
    plan: Plan
    attempt: int
    evidence_id: str | None
    report: EngineReport | None
    error: str | None
    audit: list[AuditEvent]


class ComparisonRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    other_id: UUID


class ExchangeDelta(BaseModel):
    method: str
    path_sha256: str
    status: int
    records: list[int]


class Comparison(BaseModel):
    left_evidence_id: str
    right_evidence_id: str
    result: Literal['match', 'difference', 'inconclusive']
    left_only: list[ExchangeDelta]
    right_only: list[ExchangeDelta]
    scope: str = 'Recorded HTTP method, path digest, status, and multiplicity. No claims about unrecorded behavior.'
