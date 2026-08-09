# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""Media resolve helpers: error classification, retry, matcher, fallback."""

from AnonX_3.core.resolver.error_classifier import (
    ErrorClass,
    ClassifiedError,
    classify_error,
    should_retry,
    should_fallback_source,
)
from AnonX_3.core.resolver.retry import retry_async, backoff_delays
from AnonX_3.core.resolver.matcher import score_candidate, pick_best, MatchScore
from AnonX_3.core.resolver.fallback import find_fallback_track, fallback_enabled
from AnonX_3.core.resolver.soundcloud import soundcloud, is_soundcloud_url

__all__ = [
    "ErrorClass",
    "ClassifiedError",
    "classify_error",
    "should_retry",
    "should_fallback_source",
    "retry_async",
    "backoff_delays",
    "score_candidate",
    "pick_best",
    "MatchScore",
    "find_fallback_track",
    "fallback_enabled",
    "soundcloud",
    "is_soundcloud_url",
]
