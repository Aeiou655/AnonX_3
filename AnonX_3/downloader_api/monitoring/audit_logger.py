"""Audit logging for security events."""

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("app.security")


def log_auth_attempt(
    success: bool,
    ip_address: str,
    api_key_hash: Optional[str] = None,
    admin: bool = False,
) -> None:
    event = "auth_success" if success else "auth_failure"
    logger.info(
        event,
        extra={
            "event": event,
            "ip_address": ip_address,
            "api_key_hash": api_key_hash,
            "admin": admin,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


def log_rate_limit(
    ip_address: str,
    endpoint: str,
) -> None:
    logger.warning(
        "rate_limit_exceeded",
        extra={
            "event": "rate_limit_exceeded",
            "ip_address": ip_address,
            "endpoint": endpoint,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


def log_blocked_request(
    ip_address: str,
    reason: str,
    url: Optional[str] = None,
) -> None:
    logger.warning(
        "request_blocked",
        extra={
            "event": "request_blocked",
            "ip_address": ip_address,
            "reason": reason,
            "url": url[:100] if url else None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


def log_admin_action(
    action: str,
    ip_address: str,
    details: Optional[dict] = None,
) -> None:
    logger.info(
        f"admin_action:{action}",
        extra={
            "event": f"admin_action:{action}",
            "ip_address": ip_address,
            "details": details,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
