# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""Security helpers: URL validation, path jail, filename sanitization."""

from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from urllib.parse import urlparse

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_BLOCKED_HOSTS = {
    "localhost",
    "metadata.google.internal",
    "metadata",
}


def sanitize_filename(name: str | None, *, default: str = "file", max_len: int = 120) -> str:
    raw = (name or "").strip() or default
    # basename only
    raw = Path(raw).name
    cleaned = _SAFE_NAME_RE.sub("_", raw).strip("._") or default
    return cleaned[:max_len]


def is_safe_relative_path(path: str | Path, root: str | Path) -> bool:
    """True if path resolves inside root (no traversal)."""
    try:
        root_r = Path(root).resolve()
        path_r = Path(path).resolve()
        path_r.relative_to(root_r)
        return True
    except Exception:
        return False


def jail_path(path: str | Path, root: str | Path) -> Path | None:
    """Return resolved path if inside root, else None."""
    try:
        root_r = Path(root).resolve()
        path_r = (root_r / Path(path).name).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        path_r.relative_to(root_r)
        return path_r
    except Exception:
        return None


def _is_private_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
        return bool(
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        )
    except ValueError:
        return False


def validate_http_url(
    url: str | None,
    *,
    allow_private: bool = False,
    allowed_schemes: tuple[str, ...] = ("http", "https"),
) -> tuple[bool, str]:
    """SSRF-oriented URL check for operator-supplied remote URLs."""
    if not url or not isinstance(url, str):
        return False, "empty"
    raw = url.strip()
    if len(raw) > 2048:
        return False, "too_long"
    parsed = urlparse(raw)
    if parsed.scheme not in allowed_schemes:
        return False, "scheme"
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False, "no_host"
    if host in _BLOCKED_HOSTS and not allow_private:
        return False, "blocked_host"
    if host.endswith(".local") and not allow_private:
        return False, "local_tld"
    # Literal IPs
    if not allow_private and _is_private_ip(host):
        return False, "private_ip"
    # Basic credential ban in URL
    if parsed.username or parsed.password:
        return False, "userinfo"
    return True, "ok"


def clamp_size_bytes(size: int | None, max_mb: int) -> bool:
    """True if size is within max_mb (unknown size allowed)."""
    if size is None:
        return True
    try:
        return int(size) <= max(1, int(max_mb)) * 1024 * 1024
    except Exception:
        return False


def clamp_duration_sec(duration: float | int | None, max_sec: int) -> bool:
    if duration is None:
        return True
    try:
        d = float(duration)
    except Exception:
        return True
    if d <= 0:
        return True
    return d <= max(60, int(max_sec))


def redact_secrets(text: str | None) -> str:
    """Best-effort redaction for logs."""
    if not text:
        return ""
    out = text
    patterns = [
        (re.compile(r"(api[_-]?key|token|secret|password|session)[=:]\s*([^\s,&]+)", re.I), r"\1=***"),
        (re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.I), "Bearer ***"),
        (re.compile(r"mongodb(\+srv)?://[^\s]+", re.I), "mongodb://***"),
    ]
    for pat, repl in patterns:
        out = pat.sub(repl, out)
    return out
