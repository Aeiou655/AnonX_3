# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""Bounded exponential backoff with jitter for temporary extraction failures."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from AnonX_3.core.resolver.error_classifier import (
    ClassifiedError,
    ErrorClass,
    classify_error,
    should_retry,
)

T = TypeVar("T")


def backoff_delays(
    max_attempts: int = 3,
    *,
    base: float = 0.0,
    factor: float = 2.0,
    cap: float = 12.0,
    jitter: float = 0.35,
) -> list[float]:
    """
    Attempt delays before attempt 1, 2, 3...
    Prompt policy: immediate, short, longer — with jitter.
    Default: [0, ~1-2s, ~4-8s]
    """
    delays: list[float] = []
    for i in range(max(1, max_attempts)):
        if i == 0:
            delays.append(max(0.0, base))
            continue
        raw = min(cap, (1.0 if base <= 0 else base) * (factor ** (i - 1)) * 1.5)
        # Prefer ~1.5s then ~4s style progression when base=0
        if base <= 0:
            raw = min(cap, 1.5 * (factor ** (i - 1)))
        j = raw * jitter
        delays.append(max(0.0, raw + random.uniform(-j, j)))
    return delays


async def retry_async(
    factory: Callable[[int, ClassifiedError | None], Awaitable[T]],
    *,
    max_attempts: int = 3,
    is_success: Callable[[T], bool] | None = None,
    on_classify: Callable[[ClassifiedError, int], None] | None = None,
) -> T:
    """
    Call factory(attempt_index, last_error) up to max_attempts.
    factory should raise on hard failure or return a value; is_success
    decides if a non-raising return counts as success.
    """
    delays = backoff_delays(max_attempts)
    last_exc: BaseException | None = None
    last_classified: ClassifiedError | None = None
    last_result: T | None = None

    for attempt in range(max_attempts):
        if delays[attempt] > 0:
            await asyncio.sleep(delays[attempt])
        try:
            result = await factory(attempt, last_classified)
            last_result = result
            if is_success is None or is_success(result):
                return result
            # Treat unsuccessful result as temporary unknown
            last_classified = ClassifiedError(
                cls=ErrorClass.UNKNOWN,
                message="unsuccessful_result",
                retryable=True,
            )
            if on_classify:
                on_classify(last_classified, attempt)
            if not should_retry(last_classified, attempt + 1, max_attempts):
                return result
        except Exception as ex:
            last_exc = ex
            last_classified = classify_error(ex)
            if on_classify:
                on_classify(last_classified, attempt)
            if not should_retry(last_classified, attempt + 1, max_attempts):
                raise
            continue

    if last_exc is not None:
        raise last_exc
    return last_result  # type: ignore[return-value]
