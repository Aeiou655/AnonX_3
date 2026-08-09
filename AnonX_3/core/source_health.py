"""Deterministic process-local source scoring and circuit breakers."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from AnonX_3 import config, logger


@dataclass
class SourceState:
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    latency_ewma: float | None = None
    opened_until: float = 0.0
    half_open_probe: bool = False


class SourceHealthRegistry:
    def __init__(self) -> None:
        self._states: dict[str, SourceState] = {}
        self._lock = threading.Lock()

    def _state(self, source: str) -> SourceState:
        return self._states.setdefault(str(source or "unknown"), SourceState())

    def allow(self, source: str, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            state = self._state(source)
            if state.opened_until <= 0:
                return True
            if current < state.opened_until:
                return False
            if state.half_open_probe:
                return False
            state.half_open_probe = True
        logger.info("source circuit half-open source=%s", source)
        return True

    def success(
        self,
        source: str,
        *,
        latency_sec: float | None = None,
    ) -> None:
        with self._lock:
            state = self._state(source)
            was_open = state.opened_until > 0
            state.successes += 1
            state.consecutive_failures = 0
            state.opened_until = 0.0
            state.half_open_probe = False
            if latency_sec is not None and latency_sec >= 0:
                state.latency_ewma = (
                    float(latency_sec)
                    if state.latency_ewma is None
                    else (0.30 * float(latency_sec)) + (0.70 * state.latency_ewma)
                )
        if was_open:
            logger.info("source circuit closed source=%s", source)

    def failure(self, source: str, *, reason: str = "") -> None:
        opened = False
        with self._lock:
            state = self._state(source)
            state.failures += 1
            state.consecutive_failures += 1
            state.half_open_probe = False
            threshold = max(
                1, int(getattr(config, "SOURCE_FAILURE_THRESHOLD", 3) or 3)
            )
            if state.consecutive_failures >= threshold:
                cooldown = max(
                    5.0,
                    float(
                        getattr(config, "SOURCE_COOLDOWN_SECONDS", 60) or 60
                    ),
                )
                state.opened_until = time.monotonic() + cooldown
                opened = True
        if opened:
            logger.warning(
                "source circuit opened source=%s reason=%s",
                source,
                str(reason or "failure")[:120],
            )

    def score(
        self,
        source: str,
        *,
        cached: bool = False,
        compatible: bool = True,
        quality: float = 1.0,
        resource_cost: float = 0.0,
        expiration_risk: float = 0.0,
    ) -> float:
        with self._lock:
            state = self._state(source)
            attempts = state.successes + state.failures
            reliability = (
                state.successes / attempts if attempts else 0.75
            )
            latency_penalty = min(2.0, float(state.latency_ewma or 0.0)) * 0.8
            failure_penalty = min(4, state.consecutive_failures) * 1.5
        return round(
            (4.0 if cached else 0.0)
            + (reliability * 4.0)
            + (2.0 if compatible else -8.0)
            + max(0.0, min(2.0, quality))
            - latency_penalty
            - failure_penalty
            - max(0.0, resource_cost)
            - max(0.0, expiration_risk),
            3,
        )

    def snapshot(self) -> dict[str, dict]:
        now = time.monotonic()
        with self._lock:
            return {
                name: {
                    "successes": state.successes,
                    "failures": state.failures,
                    "consecutive_failures": state.consecutive_failures,
                    "latency_ewma": state.latency_ewma,
                    "circuit": (
                        "open"
                        if state.opened_until > now
                        else "half_open"
                        if state.half_open_probe
                        else "closed"
                    ),
                }
                for name, state in self._states.items()
            }


source_health = SourceHealthRegistry()
