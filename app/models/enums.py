from enum import StrEnum


class SessionStatus(StrEnum):
    ACTIVE = 'active'
    ARCHIVED = 'archived'


class RunStatus(StrEnum):
    IDLE = 'idle'
    QUEUED = 'queued'
    PLANNING = 'planning'
    NAVIGATING = 'navigating'
    EXTRACTING = 'extracting'
    PAUSED = 'paused'
    AWAITING_APPROVAL = 'awaiting_approval'
    MANUAL = 'manual'
    COMPLETED = 'completed'
    FAILED = 'failed'
    INTERRUPTED = 'interrupted'
    CANCELLED = 'cancelled'
    AWAITING_INPUT = 'awaiting_input'


class ControlMode(StrEnum):
    AGENT = 'agent'
    MANUAL = 'manual'


class ControlHolder(StrEnum):
    AGENT = 'agent'
    USER = 'user'
    NONE = 'none'


class ApprovalStatus(StrEnum):
    PENDING = 'pending'
    APPROVED = 'approved'
    REJECTED = 'rejected'


class Actor(StrEnum):
    USER = 'user'
    SYSTEM = 'system'
    ASSISTANT = 'assistant'
    PLANNER_AGENT = 'planner_agent'
    NAVIGATOR_AGENT = 'navigator_agent'
    EXTRACTOR_AGENT = 'extractor_agent'


class MessageRole(StrEnum):
    USER = 'user'
    ASSISTANT = 'assistant'
    SYSTEM = 'system'
    PLANNER_AGENT = 'planner_agent'
    NAVIGATOR_AGENT = 'navigator_agent'
    EXTRACTOR_AGENT = 'extractor_agent'


class ActionType(StrEnum):
    CLICK = 'click'
    TYPE = 'type'
    OPEN = 'open'
    EXTRACT = 'extract'
    NOTE = 'note'
    PAUSE = 'pause'
    RESUME = 'resume'
    APPROVE = 'approve'
    REJECT = 'reject'
    TAKEOVER = 'takeover'
    RELEASE = 'release'
    PLAN = 'plan'
    CHECKPOINT = 'checkpoint'
    ERROR = 'error'
    RETRY = 'retry'
    BACK = 'back'
    NAVIGATE = 'navigate'
    SWITCH_TAB = 'switch_tab'
    TAB_CREATED = 'tab_created'
    TAB_CLOSED = 'tab_closed'
    SNAPSHOT = 'snapshot'


class CheckpointReason(StrEnum):
    PLANNER_CHECKPOINT = 'planner_checkpoint'
    MANUAL_BEFORE_TAKEOVER = 'manual_before_takeover'
    MANUAL_RELEASE = 'manual_release'
    PRE_SENSITIVE_ACTION = 'pre_sensitive_action'
    USER_REQUEST = 'user_request'
    AUTO_PERIODIC = 'auto_periodic'


class BrowserRestoreMode(StrEnum):
    NONE = 'none'
    EXACT = 'exact'
    LOGICAL_ONLY = 'logical_only'

