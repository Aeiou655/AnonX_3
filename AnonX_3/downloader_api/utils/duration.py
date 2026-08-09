"""Duration utilities."""


def format_duration(seconds: int | float | None) -> str:
    if seconds is None or seconds < 0:
        return "00:00"

    seconds = int(seconds)

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def parse_duration(duration_str: str) -> int:
    if not duration_str:
        return 0

    parts = duration_str.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 1:
            return int(parts[0])
    except ValueError:
        pass

    return 0


def is_short_media(duration: int | None, threshold: int = 300) -> bool:
    if duration is None:
        return False
    return duration <= threshold


def estimate_download_time(file_size_bytes: int, speed_bps: int) -> int:
    if speed_bps <= 0:
        return 0
    return int(file_size_bytes / speed_bps)
