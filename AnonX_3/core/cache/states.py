"""Playback cache state machine."""

from enum import Enum


class CacheState(str, Enum):
    MISS = "miss"
    RESOLVING = "resolving"
    DOWNLOADING = "downloading"
    READY = "ready"
    FAILED_TEMPORARY = "failed_temporary"
    FAILED_PERMANENT = "failed_permanent"
    EXPIRED = "expired"


_TRANSITIONS = {
    CacheState.MISS: {
        CacheState.RESOLVING,
        CacheState.DOWNLOADING,
        CacheState.READY,
        CacheState.FAILED_TEMPORARY,
        CacheState.FAILED_PERMANENT,
    },
    CacheState.RESOLVING: {
        CacheState.DOWNLOADING,
        CacheState.READY,
        CacheState.FAILED_TEMPORARY,
        CacheState.FAILED_PERMANENT,
    },
    CacheState.DOWNLOADING: {
        CacheState.READY,
        CacheState.FAILED_TEMPORARY,
        CacheState.FAILED_PERMANENT,
    },
    CacheState.READY: {
        CacheState.READY,
        CacheState.RESOLVING,
        CacheState.DOWNLOADING,
        CacheState.EXPIRED,
        CacheState.FAILED_TEMPORARY,
    },
    CacheState.FAILED_TEMPORARY: {
        CacheState.RESOLVING,
        CacheState.DOWNLOADING,
        CacheState.READY,
        CacheState.FAILED_PERMANENT,
    },
    CacheState.EXPIRED: {
        CacheState.RESOLVING,
        CacheState.DOWNLOADING,
        CacheState.READY,
        CacheState.FAILED_TEMPORARY,
    },
    CacheState.FAILED_PERMANENT: {CacheState.FAILED_PERMANENT},
}


def can_transition(from_state: CacheState | str, to_state: CacheState | str) -> bool:
    try:
        src = from_state if isinstance(from_state, CacheState) else CacheState(str(from_state))
        dst = to_state if isinstance(to_state, CacheState) else CacheState(str(to_state))
    except ValueError:
        return False
    return dst in _TRANSITIONS.get(src, set())
