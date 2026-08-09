# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""Classify yt-dlp / HTTP / YouTube extraction errors for retry vs fallback."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ErrorClass(str, Enum):
    TEMPORARY = "temporary"
    RATE_LIMIT = "rate_limit"
    AUTH_CHALLENGE = "auth_challenge"  # explicit YouTube sign-in/bot gate
    CLIENT_PO = "client_po"  # 403 / bot-check / PO token
    PERMANENT = "permanent"
    REGION = "region"
    FORMAT = "format"
    UNKNOWN = "unknown"


@dataclass
class ClassifiedError:
    cls: ErrorClass
    message: str
    retryable: bool
    use_alt_client: bool = False
    use_po_token: bool = False
    drop_cookies: bool = False
    fallback_source: bool = False

    @property
    def name(self) -> str:
        return self.cls.value


_PATTERNS: list[tuple[ErrorClass, re.Pattern[str], dict[str, Any]]] = [
    (
        ErrorClass.RATE_LIMIT,
        re.compile(r"\b(429|too many requests|rate[-\s]?limit|slow down)\b", re.I),
        {"retryable": True},
    ),
    (
        ErrorClass.REGION,
        re.compile(
            r"\b(not available in your country|geo(?:graphically)?(?:\s|-)blocked|"
            r"region(?:al)?(?:\s|-)block|blocked in your country)\b",
            re.I,
        ),
        {"retryable": False, "fallback_source": True},
    ),
    (
        ErrorClass.PERMANENT,
        re.compile(
            r"\b(video unavailable|private video|this video has been removed|"
            r"deleted|terminated|account has been terminated|"
            r"copyright|dmca|uploader has not made|members[-\s]?only|"
            r"login required|sign in to confirm your age|age[-\s]?restricted|"
            r"premieres in|live event will begin|"
            r"(?:this )?(?:video|content) is not available)\b",
            re.I,
        ),
        {"retryable": False, "fallback_source": True},
    ),
    (
        ErrorClass.AUTH_CHALLENGE,
        re.compile(
            r"\b(sign in to confirm you.?re not a bot|"
            r"confirm you.?re not a bot|use --cookies-from-browser|"
            r"use --cookies for (?:the )?authentication)\b",
            re.I,
        ),
        {
            "retryable": False,
            "use_alt_client": False,
            "use_po_token": True,
            "drop_cookies": False,
            "fallback_source": True,
        },
    ),
    (
        ErrorClass.CLIENT_PO,
        re.compile(
            r"\b(403|forbidden|bot|"
            r"po[_\s-]?token|player response|sabr|gvs|http error 403|"
            r"cookies?.*(expired|invalid)|"
            r"only images are available)\b",
            re.I,
        ),
        {
            "retryable": True,
            "use_alt_client": True,
            "use_po_token": True,
            "drop_cookies": False,
        },
    ),
    (
        ErrorClass.TEMPORARY,
        re.compile(
            r"\b(5\d{2}|timeout|timed out|temporarily|try again|"
            r"connection (?:reset|refused|aborted)|network|"
            r"ssl|tls|incomplete|eof|broken pipe|unable to download|"
            r"http error 50[0-4]|server error)\b",
            re.I,
        ),
        {"retryable": True},
    ),
    (
        ErrorClass.FORMAT,
        re.compile(
            r"\b(format|no suitable|no video formats found|"
            r"requested format is not available|"
            r"requested formats? are incompatible|"
            r"ffmpeg|merge|postprocess)\b",
            re.I,
        ),
        {"retryable": True},
    ),
]


def classify_error(exc: BaseException | str | None) -> ClassifiedError:
    """Map an exception or message to a ClassifiedError."""
    if exc is None:
        return ClassifiedError(
            cls=ErrorClass.UNKNOWN,
            message="",
            retryable=True,
        )
    if isinstance(exc, ClassifiedError):
        return exc
    text = str(exc) if not isinstance(exc, str) else exc
    text = (text or "").strip()
    if not text:
        return ClassifiedError(
            cls=ErrorClass.UNKNOWN, message="", retryable=True
        )

    for cls, pattern, flags in _PATTERNS:
        if pattern.search(text):
            return ClassifiedError(
                cls=cls,
                message=text[:500],
                retryable=bool(flags.get("retryable", False)),
                use_alt_client=bool(flags.get("use_alt_client", False)),
                use_po_token=bool(flags.get("use_po_token", False)),
                drop_cookies=bool(flags.get("drop_cookies", False)),
                fallback_source=bool(flags.get("fallback_source", False)),
            )

    # yt-dlp DownloadError wrapping often includes nested hints
    low = text.lower()
    if "unavailable" in low or "private" in low:
        return ClassifiedError(
            cls=ErrorClass.PERMANENT,
            message=text[:500],
            retryable=False,
            fallback_source=True,
        )
    if "403" in low or "forbidden" in low:
        return ClassifiedError(
            cls=ErrorClass.CLIENT_PO,
            message=text[:500],
            retryable=True,
            use_alt_client=True,
            use_po_token=True,
        )

    return ClassifiedError(
        cls=ErrorClass.UNKNOWN,
        message=text[:500],
        retryable=True,
    )


def should_retry(classified: ClassifiedError, attempt: int, max_attempts: int = 3) -> bool:
    if attempt >= max_attempts:
        return False
    if classified.cls == ErrorClass.PERMANENT:
        return False
    if classified.cls == ErrorClass.REGION:
        return False
    if classified.cls == ErrorClass.AUTH_CHALLENGE:
        return False
    return classified.retryable or classified.cls in {
        ErrorClass.TEMPORARY,
        ErrorClass.RATE_LIMIT,
        ErrorClass.CLIENT_PO,
        ErrorClass.FORMAT,
        ErrorClass.UNKNOWN,
    }


def should_fallback_source(classified: ClassifiedError) -> bool:
    if classified.fallback_source:
        return True
    return classified.cls in {
        ErrorClass.AUTH_CHALLENGE,
        ErrorClass.PERMANENT,
        ErrorClass.REGION,
    }
