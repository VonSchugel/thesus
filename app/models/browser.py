from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.errors import AppError, ConflictError, ValidationError


class BrowserUnavailable(AppError):
    status_code = 503
    error_code = 'browser_unavailable'
    error_category = 'browser'
    retryable = True
    recovery_options = ('reconnect_browser',)


class BrowserOutcomeUnknown(BrowserUnavailable):
    error_code = 'browser_outcome_unknown'
    retryable = False
    recovery_options = ('inspect_browser',)


class StaleBrowserState(ConflictError):
    error_code = 'stale_browser_state'
    recovery_options = ('refresh_state', 'observe_again')


class BrowserCapacity(ConflictError):
    error_code = 'browser_capacity'


class BrowserPolicyError(ValidationError):
    error_code = 'browser_policy_blocked'
    error_category = 'browser_policy'


BrowserCommandKind = Literal[
    'open', 'open_link', 'back', 'forward', 'reload', 'new_tab', 'switch_tab', 'close_tab',
    'click', 'type', 'key', 'wheel', 'resize', 'upload',
]

_COMMAND_FIELDS = {
    'open': ('url',), 'open_link': ('target_id',), 'new_tab': ('url',),
    'switch_tab': ('tab_id',), 'close_tab': ('tab_id',),
    'click': ('target_id', 'x', 'y', 'button'), 'type': ('target_id', 'text'), 'key': ('key',),
    'wheel': ('x', 'y', 'delta_x', 'delta_y'), 'resize': ('width', 'height'),
    'upload': ('target_id', 'artifact_id'),
}
_REQUIRED_FIELDS = {
    'open': ('url',), 'open_link': ('target_id',), 'switch_tab': ('tab_id',), 'type': ('text',),
    'key': ('key',), 'resize': ('width', 'height'), 'upload': ('artifact_id',),
}


class BrowserCommand(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)

    kind: BrowserCommandKind
    url: str | None = Field(default=None, min_length=1, max_length=4096)
    tab_id: str | None = Field(default=None, min_length=1, max_length=80)
    target_id: str | None = Field(default=None, min_length=1, max_length=80)
    x: float | None = Field(default=None, ge=0, le=3840)
    y: float | None = Field(default=None, ge=0, le=2160)
    button: Literal['left', 'right', 'middle'] = 'left'
    text: str | None = Field(default=None, max_length=8192, repr=False)
    key: str | None = Field(default=None, min_length=1, max_length=80)
    delta_x: float = Field(default=0, ge=-4000, le=4000)
    delta_y: float = Field(default=0, ge=-4000, le=4000)
    width: int | None = Field(default=None, ge=320, le=1920)
    height: int | None = Field(default=None, ge=240, le=1200)
    artifact_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')

    @classmethod
    def planner_schema(cls, *, has_targets: bool = True) -> dict:
        properties = cls.model_json_schema()['properties']
        target_commands = {'open_link', 'click', 'type', 'upload'}
        return {'anyOf': [
            {'type': 'object', 'additionalProperties': False,
             'properties': {'kind': {'const': kind}, **{key: properties[key] for key in _COMMAND_FIELDS.get(kind, ())}},
             'required': ['kind', *_REQUIRED_FIELDS.get(kind, ())]}
            for kind in properties['kind']['enum'] if has_targets or kind not in target_commands
        ]}

    @model_validator(mode='after')
    def validate_command(self) -> BrowserCommand:
        if self.model_fields_set - {'kind', *_COMMAND_FIELDS.get(self.kind, ())}:
            raise ValueError('Unexpected field for this browser command')
        if any(getattr(self, field) is None for field in _REQUIRED_FIELDS.get(self.kind, ())):
            raise ValueError('Required browser command field is missing')
        if (self.x is None) != (self.y is None):
            raise ValueError('Both coordinates are required')
        if self.kind == 'click' and (self.target_id is None) == (self.x is None):
            raise ValueError('Click requires either a target or coordinates')
        if self.kind == 'wheel' and not (self.delta_x or self.delta_y):
            raise ValueError('Wheel requires a nonzero scroll delta')
        return self


class BrowserElement(BaseModel):
    target_id: str
    role: str
    label: str
    href: str | None = None
    tag: str
    input_type: str | None = None
    form_action: str | None = None
    form_method: str | None = None


class BrowserObservation(BaseModel):
    revision: str
    tab_id: str
    url: str
    title: str
    ax_tree: str
    elements: list[BrowserElement]
    screenshot: bytes | None = Field(default=None, exclude=True, repr=False)


class BrowserFrame(BaseModel):
    revision: str
    tab_id: str
    frame_id: str
    width: int
    height: int


class BrowserArtifact(BaseModel):
    id: str
    name: str
    kind: Literal['upload', 'download']
    size: int


class BrowserActionResult(BaseModel):
    summary: str
    url: str
    revision: str
    tab_id: str
    artifacts: list[BrowserArtifact] = Field(default_factory=list)
