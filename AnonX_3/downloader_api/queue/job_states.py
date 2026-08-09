"""Job state definitions."""

from AnonX_3.downloader_api.core.constants import JobState, JobPriority

JOB_STATE_TRANSITIONS = {
    JobState.CREATED: [JobState.VALIDATING, JobState.FAILED, JobState.CANCELLED],
    JobState.VALIDATING: [JobState.CACHE_CHECK, JobState.FAILED, JobState.CANCELLED],
    JobState.CACHE_CHECK: [JobState.QUEUED, JobState.READY, JobState.FAILED, JobState.CANCELLED],
    JobState.QUEUED: [JobState.EXTRACTING, JobState.FAILED, JobState.CANCELLED],
    JobState.EXTRACTING: [JobState.DOWNLOADING, JobState.FAILED, JobState.CANCELLED, JobState.RETRY_WAIT],
    JobState.DOWNLOADING: [JobState.PROCESSING, JobState.VALIDATING_FILE, JobState.FAILED, JobState.CANCELLED, JobState.RETRY_WAIT],
    JobState.PROCESSING: [JobState.VALIDATING_FILE, JobState.FAILED, JobState.CANCELLED],
    JobState.VALIDATING_FILE: [JobState.SAVING, JobState.FAILED, JobState.CANCELLED],
    JobState.SAVING: [JobState.READY, JobState.FAILED, JobState.CANCELLED],
    JobState.READY: [JobState.STREAMING, JobState.COMPLETED, JobState.EXPIRED],
    JobState.STREAMING: [JobState.COMPLETED, JobState.FAILED],
    JobState.RETRY_WAIT: [JobState.EXTRACTING, JobState.DOWNLOADING, JobState.FAILED, JobState.CANCELLED],
    JobState.COMPLETED: [],
    JobState.FAILED: [],
    JobState.CANCELLED: [],
    JobState.EXPIRED: [],
}

TERMINAL_STATES = {
    JobState.COMPLETED,
    JobState.FAILED,
    JobState.CANCELLED,
    JobState.EXPIRED,
}

ACTIVE_STATES = {
    JobState.VALIDATING,
    JobState.CACHE_CHECK,
    JobState.EXTRACTING,
    JobState.DOWNLOADING,
    JobState.PROCESSING,
    JobState.VALIDATING_FILE,
    JobState.SAVING,
    JobState.STREAMING,
}

PENDING_STATES = {
    JobState.CREATED,
    JobState.QUEUED,
    JobState.RETRY_WAIT,
}


def can_transition(from_state: JobState, to_state: JobState) -> bool:
    allowed = JOB_STATE_TRANSITIONS.get(from_state, [])
    return to_state in allowed


def is_terminal(state: JobState) -> bool:
    return state in TERMINAL_STATES


def is_active(state: JobState) -> bool:
    return state in ACTIVE_STATES


def is_pending(state: JobState) -> bool:
    return state in PENDING_STATES
