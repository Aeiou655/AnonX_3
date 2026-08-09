"""Time utilities."""

from datetime import datetime, timezone, timedelta
from typing import Optional


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp_now() -> float:
    return utc_now().timestamp()


def from_timestamp(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def format_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat()


def parse_iso(iso_str: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except Exception:
        return None


def get_expiry_time(ttl_hours: int) -> datetime:
    return utc_now() + timedelta(hours=ttl_hours)


def is_expired(expiry: datetime) -> bool:
    return utc_now() > expiry


def seconds_until(dt: datetime) -> int:
    delta = dt - utc_now()
    return max(0, int(delta.total_seconds()))


def format_eta(seconds: int | None) -> str:
    if seconds is None or seconds < 0:
        return "unknown"

    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes}m {secs}s"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"


def format_speed(bytes_per_second: float) -> str:
    if bytes_per_second < 0:
        return "0 B/s"

    if bytes_per_second < 1024:
        return f"{bytes_per_second:.0f} B/s"
    elif bytes_per_second < 1024 * 1024:
        return f"{bytes_per_second / 1024:.1f} KB/s"
    else:
        return f"{bytes_per_second / (1024 * 1024):.2f} MB/s"
