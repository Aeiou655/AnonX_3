import os
import re
from os import getenv
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from dotenv import load_dotenv

load_dotenv()

_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve_path(path: str | None) -> str | None:
    if not path:
        return path
    p = path.strip()
    if not p or p.startswith(("http://", "https://")):
        return p
    if os.path.isabs(p):
        return p
    resolved = os.path.join(_CONFIG_DIR, p)
    return resolved if os.path.exists(resolved) else p


def _int_or_auto(name: str, default: int) -> int | str:
    raw = getenv(name, str(default))
    value = (raw or "").strip()
    if not value:
        return "auto"
    if value.lower() in {"auto", "default"}:
        return "auto"
    try:
        return int(float(value))
    except Exception:
        return default


def _float_or_auto(name: str, default: float) -> float | str:
    raw = getenv(name, str(default))
    value = (raw or "").strip()
    if not value or value.lower() in {"auto", "default"}:
        return "auto"
    try:
        return float(value)
    except Exception:
        return default


def _int_env(name: str, default: int, minimum: int = 0) -> int:
    raw = getenv(name, str(default))
    try:
        value = int(float((raw or "").strip()))
    except Exception:
        value = default
    return max(minimum, value)


def _bool_env(name: str, default: bool) -> bool:
    raw = getenv(name, str(default)).lower().strip()
    if raw in ("true", "1", "yes", "on"):
        return True
    if raw in ("false", "0", "no", "off"):
        return False
    return default


def _po_token_provider_url(enabled: bool) -> str:
    """Resolve optional PO-token sidecar URL without making startup brittle."""
    raw = (
        getenv("PO_TOKEN_PROVIDER_URL", "")
        or getenv("POT_PROVIDER_URL", "")
        or ""
    ).strip()
    if not enabled:
        return ""
    if raw.lower() in {"", "auto", "default", "dynamic"}:
        return "http://127.0.0.1:4416"
    return raw.rstrip("/")


def _tcp_open(host: str, port: int, timeout: float = 0.25) -> bool:
    """True if local host:port accepts TCP (local proxy probe)."""
    import socket

    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _proxy_can_reach_youtube(proxy_url: str, timeout: float = 3.0) -> bool:
    """
    True when proxy can complete a tiny HTTPS hop (not just TCP listen).

    Port 8080/8888 often accept TCP (Java apps, reverse proxies, random
    services) but are NOT HTTP/SOCKS forward proxies — TCP-only detection
    wrongly binds YOUTUBE_PROXY and breaks all YouTube search.
    """
    if not proxy_url or "://" not in proxy_url:
        return False
    # Prefer httpx (supports http + socks when extras installed).
    try:
        import httpx

        with httpx.Client(
            proxy=proxy_url,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "AnonX_3-proxy-probe/1.0"},
        ) as client:
            # generate_204 is tiny; any 2xx/3xx means tunnel works.
            for url in (
                "https://www.youtube.com/generate_204",
                "https://i.ytimg.com/generate_204",
                "https://www.google.com/generate_204",
            ):
                try:
                    resp = client.get(url)
                    if resp.status_code in {200, 204, 301, 302, 303, 307, 308}:
                        return True
                except Exception:
                    continue
    except Exception:
        pass
    # urllib fallback (HTTP(S) proxies only)
    try:
        import urllib.request

        scheme = (urlparse(proxy_url).scheme or "").lower()
        if scheme not in {"http", "https"}:
            return False
        req = urllib.request.Request(
            "https://www.youtube.com/generate_204",
            headers={"User-Agent": "AnonX_3-proxy-probe/1.0"},
            method="GET",
        )
        handler = urllib.request.ProxyHandler(
            {"http": proxy_url, "https": proxy_url}
        )
        opener = urllib.request.build_opener(handler)
        with opener.open(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 0) or 0) in {
                200,
                204,
                301,
                302,
                303,
                307,
                308,
            }
    except Exception:
        return False
    return False


# Common local VPS / desktop proxy listeners (Clash, v2ray, 3x-ui, shadowsocks, …)
# Prefer dedicated proxy ports; 8080/8888 last (often non-proxy services).
_LOCAL_PROXY_CANDIDATES: tuple[tuple[str, str, int], ...] = (
    ("socks5h", "127.0.0.1", 1080),
    ("socks5h", "127.0.0.1", 10808),
    ("socks5h", "127.0.0.1", 10809),
    ("socks5h", "127.0.0.1", 7891),
    ("socks5h", "127.0.0.1", 7897),
    ("http", "127.0.0.1", 7890),
    ("http", "127.0.0.1", 7892),  # Clash mixed sometimes
    ("http", "127.0.0.1", 20171),
    ("http", "127.0.0.1", 20172),
    ("http", "127.0.0.1", 8118),
    ("socks5h", "127.0.0.1", 9050),  # tor
    ("http", "127.0.0.1", 3128),
    ("socks5h", "127.0.0.1", 2080),
    ("http", "127.0.0.1", 2080),
    ("http", "127.0.0.1", 8080),
    ("http", "127.0.0.1", 8888),
)

# Ports that are almost never forward-proxies on a music bot VPS.
_PROXY_PORT_BLACKLIST: frozenset[int] = frozenset(
    {
        22,
        25,
        53,
        80,
        443,
        3306,
        5432,
        6379,
        27017,
        27018,
        11211,
        # AnonX_3 CDN origin often binds here (not a YouTube proxy)
        41175,
    }
)

# Runtime cache for auto/dynamic mode (port can change when Clash restarts).
_PROXY_RUNTIME: dict = {
    "url": None,  # str | None; None = not resolved yet
    "until": 0.0,
    "mode": "auto",
    "raw": "auto",
    "last_probe_at": 0.0,
}


def _env_proxy_chain() -> list[str]:
    """Collect proxy URLs from standard process env (VPS systemd/docker often set these)."""
    keys = (
        "ALL_PROXY",
        "all_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "PROXY_URL",
        "SOCKS_PROXY",
        "socks_proxy",
    )
    out: list[str] = []
    for k in keys:
        v = (getenv(k) or "").strip()
        if v and v not in out:
            out.append(v)
    return out


def _parse_proxy_candidates_raw(raw: str) -> list[tuple[str, str, int]]:
    """Parse 'socks5h://127.0.0.1:1080,http://127.0.0.1:7890' or '1080,7890'."""
    items: list[tuple[str, str, int]] = []
    for chunk in re.split(r"[\s,;]+", raw or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "://" in chunk:
            p = urlparse(chunk)
            host = p.hostname or "127.0.0.1"
            port = int(p.port or 0)
            scheme = (p.scheme or "http").lower()
            if port > 0:
                items.append((scheme, host, port))
            continue
        # bare port → try socks5h then http on localhost
        try:
            port = int(chunk)
        except Exception:
            continue
        if 1 <= port <= 65535:
            items.append(("socks5h", "127.0.0.1", port))
            items.append(("http", "127.0.0.1", port))
    return items


def _listening_loopback_ports() -> list[int]:
    """
    Dynamic local port discovery from the kernel table (Linux VPS).

    Falls back to [] on Windows/dev where /proc is missing — static
    candidates still cover Clash/v2ray defaults.
    """
    ports: set[int] = set()

    def _ingest_proc(path: str, *, ipv6: bool = False) -> None:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                next(fh, None)  # header
                for line in fh:
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    # state 0A = LISTEN
                    if parts[3].upper() != "0A":
                        continue
                    local = parts[1]
                    if ":" not in local:
                        continue
                    addr_hex, port_hex = local.rsplit(":", 1)
                    try:
                        port = int(port_hex, 16)
                    except Exception:
                        continue
                    if port in _PROXY_PORT_BLACKLIST or not (1 <= port <= 65535):
                        continue
                    # IPv4 loopback 0100007F = 127.0.0.1 (little-endian hex)
                    if not ipv6:
                        if addr_hex.upper() not in {"0100007F", "00000000"}:
                            # 00000000 = 0.0.0.0 bind — still reachable as 127.0.0.1
                            continue
                    else:
                        # ::1 or :: (any)
                        if addr_hex.upper() not in {
                            "00000000000000000000000000000000",
                            "00000000000000000000000001000000",
                        }:
                            # still accept common any/loopback forms
                            if not (
                                addr_hex.endswith("00000000")
                                or addr_hex.upper().endswith("01000000")
                            ):
                                continue
                    ports.add(port)
        except OSError:
            return

    _ingest_proc("/proc/net/tcp", ipv6=False)
    _ingest_proc("/proc/net/tcp6", ipv6=True)

    # Prefer well-known proxy ranges first when ordering later.
    return sorted(ports)


def _schemes_for_port(port: int) -> tuple[str, ...]:
    """Guess likely schemes for a discovered local port."""
    # Clash mixed HTTP often 7890; SOCKS 7891/1080/10808.
    if port in {1080, 10808, 10809, 7891, 7897, 9050, 2080}:
        return ("socks5h", "http")
    if port in {7890, 7892, 20171, 20172, 8118, 3128, 8080, 8888}:
        return ("http", "socks5h")
    # Unknown discovered port: try both (HTTP first — more common mixed ports).
    return ("http", "socks5h")


def _build_local_proxy_candidates() -> list[tuple[str, str, int]]:
    """Static defaults + env list + dynamically discovered loopback listeners."""
    cand_raw = (getenv("YOUTUBE_PROXY_CANDIDATES", "") or "").strip()
    items: list[tuple[str, str, int]] = []
    if cand_raw:
        items.extend(_parse_proxy_candidates_raw(cand_raw))
    items.extend(_LOCAL_PROXY_CANDIDATES)

    # Dynamic: any other LISTEN port on loopback / 0.0.0.0 (Linux VPS).
    known_ports = {p for _, _, p in items}
    for port in _listening_loopback_ports():
        if port in known_ports or port in _PROXY_PORT_BLACKLIST:
            continue
        # Skip tiny/system-ish high-traffic service ranges that are never proxies.
        if port < 1024:
            continue
        for scheme in _schemes_for_port(port):
            items.append((scheme, "127.0.0.1", port))

    # Dedup preserve order
    seen: set[tuple[str, str, int]] = set()
    out: list[tuple[str, str, int]] = []
    for row in items:
        if row in seen:
            continue
        seen.add(row)
        out.append(row)
    return out


def _apply_proxy_env(url: str) -> None:
    """Sync process env for py_yt (PROXY_URL) without forcing global HTTP_PROXY."""
    if url:
        os.environ["PROXY_URL"] = url
        os.environ.setdefault("ALL_PROXY", url)
        return
    stale = (os.environ.get("PROXY_URL") or "").strip()
    if not stale:
        return
    try:
        host = urlparse(stale).hostname or ""
    except Exception:
        host = ""
    if host in {"127.0.0.1", "localhost", "::1", ""}:
        os.environ.pop("PROXY_URL", None)


def resolve_youtube_proxy(raw: str | None = None) -> str:
    """
    Dynamic local-VPS YouTube proxy (port auto):

    - Explicit URL → use as-is (operator intent)
    - auto/dynamic/local/vps/empty → detect live:
        1) env ALL_PROXY / HTTP(S)_PROXY / PROXY_URL (validated)
        2) YOUTUBE_PROXY_CANDIDATES
        3) common local ports + /proc listening ports (TCP + YouTube probe)
    - off → no proxy

    Auto refuses TCP-only listeners that cannot fetch YouTube (:8080 trap).
    """
    value = (raw if raw is not None else getenv("YOUTUBE_PROXY", "auto") or "auto").strip()
    low = value.lower()
    validate = _bool_env("YOUTUBE_PROXY_VALIDATE", True)

    if low in {"off", "false", "0", "no", "none", "disable", "disabled"}:
        return ""

    def _accept(url: str) -> str:
        if not url:
            return ""
        if not validate:
            return url
        if _proxy_can_reach_youtube(url):
            return url
        return ""

    # Explicit proxy URL (not auto)
    if value and low not in {"", "auto", "dynamic", "default", "local", "vps"}:
        if "://" in value:
            explicit = value
        elif re.match(r"^[\w.\-]+:\d+$", value):
            explicit = f"socks5h://{value}"
        else:
            explicit = value
        # Operator override: keep even if probe fails.
        if validate and _proxy_can_reach_youtube(explicit):
            return explicit
        return explicit

    auto_on = _bool_env("YOUTUBE_PROXY_AUTO", True)
    if not auto_on:
        return ""

    # 1) Process environment proxies (validated in auto mode)
    for env_proxy in _env_proxy_chain():
        accepted = _accept(env_proxy)
        if accepted:
            return accepted

    # 2) Static + dynamic local candidates — TCP-open first (fast filter), then live probe
    candidates = _build_local_proxy_candidates()
    open_urls: list[str] = []
    seen_url: set[str] = set()
    for scheme, host, port in candidates:
        if not _tcp_open(host, port):
            continue
        url = f"{scheme}://{host}:{port}"
        if url in seen_url:
            continue
        seen_url.add(url)
        open_urls.append(url)

    for url in open_urls:
        accepted = _accept(url)
        if accepted:
            return accepted

    return ""


def get_youtube_proxy(*, force_refresh: bool = False) -> str:
    """
    Runtime getter with TTL cache.

    Auto mode re-scans local ports periodically so Clash/v2ray port changes
    are picked up without bot restart. Explicit/off modes stay fixed.
    """
    import time

    raw = (_PROXY_RUNTIME.get("raw") or getenv("YOUTUBE_PROXY", "auto") or "auto").strip()
    mode = (_PROXY_RUNTIME.get("mode") or "auto").lower()
    ttl = max(15.0, float(getenv("YOUTUBE_PROXY_RELOAD_SEC", "120") or 120))
    now = time.time()

    if mode in {"off", "false", "0", "no", "none", "disable", "disabled"}:
        return ""

    # Explicit: only re-validate optionally; do not auto-switch ports.
    if mode == "explicit":
        url = str(_PROXY_RUNTIME.get("url") or "")
        if not force_refresh and url:
            return url
        url = resolve_youtube_proxy(raw)
        _PROXY_RUNTIME["url"] = url
        _PROXY_RUNTIME["until"] = now + ttl
        _PROXY_RUNTIME["last_probe_at"] = now
        _apply_proxy_env(url)
        return url

    # auto / dynamic
    cached = _PROXY_RUNTIME.get("url")
    until = float(_PROXY_RUNTIME.get("until") or 0)
    if (
        not force_refresh
        and cached is not None
        and now < until
    ):
        # Soft re-check: if cached proxy still TCP-open, keep it.
        try:
            p = urlparse(str(cached))
            host = p.hostname or "127.0.0.1"
            port = int(p.port or 0)
            if cached and port and _tcp_open(host, port, timeout=0.15):
                return str(cached)
        except Exception:
            pass
        # Cache still valid value may be "" (direct) — return until TTL ends
        if cached == "":
            return ""

    url = resolve_youtube_proxy(raw if raw else "auto")
    prev = _PROXY_RUNTIME.get("url")
    _PROXY_RUNTIME["url"] = url
    _PROXY_RUNTIME["until"] = now + ttl
    _PROXY_RUNTIME["last_probe_at"] = now
    _apply_proxy_env(url)
    return url


def refresh_youtube_proxy(*, reason: str = "") -> str:
    """Force re-scan local VPS proxy ports (call after search network failures)."""
    return get_youtube_proxy(force_refresh=True)


def youtube_proxy_status() -> dict:
    """Debug snapshot for logs / health."""
    return {
        "mode": _PROXY_RUNTIME.get("mode"),
        "raw": _PROXY_RUNTIME.get("raw"),
        "url": _PROXY_RUNTIME.get("url") or "",
        "until": _PROXY_RUNTIME.get("until"),
        "last_probe_at": _PROXY_RUNTIME.get("last_probe_at"),
    }


def _download_limit_mb_from_gb() -> int:
    raw_gb = (getenv("DOWNLOAD_LIMIT_GB", "3") or "").strip()
    try:
        gb_value = float(raw_gb)
    except Exception:
        gb_value = 1.0
    if gb_value <= 0:
        gb_value = 1.0
    return max(1, int(gb_value * 1024))


def _normalize_startgroup_url(value: str) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None

    if "://" not in raw and "/" not in raw and "?" not in raw:
        raw = f"https://t.me/{raw.lstrip('@')}"
    elif raw.startswith("@"):
        raw = f"https://t.me/{raw.lstrip('@')}"
    elif raw.startswith("t.me/"):
        raw = f"https://{raw}"

    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["startgroup"] = "true"
    return urlunparse(parsed._replace(query=urlencode(query)))


def _parse_startgroup_urls(raw: str) -> list[str]:
    links: list[str] = []
    for chunk in re.split(r"[\s,]+", raw or ""):
        normalized = _normalize_startgroup_url(chunk)
        if normalized:
            links.append(normalized)
    return links


def _parse_premium_emoji_ids(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    raw = raw.strip()
    if not raw:
        return {}
    try:
        import json
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {
                str(k): str(v)
                for k, v in parsed.items()
                if isinstance(v, (str, int))
            }
    except Exception:
        pass
    result: dict[str, str] = {}
    for chunk in re.split(r"\s*,\s*", raw):
        if "=" not in chunk:
            continue
        emoji, eid = chunk.split("=", 1)
        emoji = emoji.strip()
        eid = eid.strip()
        if emoji and eid:
            result[emoji] = eid
    return result


def _parse_weights(raw: str, expected: int) -> list[float]:
    if expected <= 0:
        return []
    values: list[float] = []
    for chunk in re.split(r"[\s,]+", raw or ""):
        part = chunk.strip()
        if not part:
            continue
        try:
            weight = float(part)
        except Exception:
            continue
        if weight >= 0:
            values.append(weight)
    return values if len(values) == expected else []


def _collect_assistant_sessions() -> list[str]:
    sessions: list[str] = []
    main = (getenv("SESSION", "") or "").strip()
    if main:
        sessions.append(main)

    numbered: list[tuple[int, str]] = []
    for key, raw in os.environ.items():
        if not key.startswith("SESSION"):
            continue
        suffix = key[7:]
        if not suffix or not suffix.isdigit():
            continue
        idx = int(suffix)
        if idx < 1:
            continue
        value = (raw or "").strip()
        if value:
            numbered.append((idx, value))

    for _, value in sorted(numbered, key=lambda item: item[0]):
        sessions.append(value)

    unique: list[str] = []
    seen: set[str] = set()
    for value in sessions:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


class Config:
    def __init__(self):
        self.API_ID = int(getenv("API_ID", 0))
        self.API_HASH = getenv("API_HASH")

        self.BOT_TOKEN = getenv("BOT_TOKEN")
        self.MONGO_URL = getenv("MONGO_URL")
        self.MONGO_TIMEOUT_MS = _int_env("MONGO_TIMEOUT_MS", 12500, 1000)

        self.LOGGER_ID = int(getenv("LOGGER_ID", 0))
        self.OWNER_ID = int(getenv("OWNER_ID", 0))

        # Defaults tuned for production hybrid; override only if needed
        self.DURATION_LIMIT = int(getenv("DURATION_LIMIT", 180)) * 60
        self.QUEUE_LIMIT = int(getenv("QUEUE_LIMIT", 25))
        self.PLAYLIST_LIMIT = int(getenv("PLAYLIST_LIMIT", 25))
        self.BROADCAST_COOLDOWN_MINUTES = max(
            0, int(getenv("BROADCAST_COOLDOWN_MINUTES", 60))
        )
        self.DOWNLOAD_LIMIT_MB = _download_limit_mb_from_gb()

        self.ASSISTANT_SESSIONS = _collect_assistant_sessions()
        self.SESSION1 = (
            self.ASSISTANT_SESSIONS[0]
            if len(self.ASSISTANT_SESSIONS) > 0
            else None
        )
        self.SESSION2 = (
            self.ASSISTANT_SESSIONS[1]
            if len(self.ASSISTANT_SESSIONS) > 1
            else None
        )
        self.SESSION3 = (
            self.ASSISTANT_SESSIONS[2]
            if len(self.ASSISTANT_SESSIONS) > 2
            else None
        )

        self.SUPPORT_CHANNEL = getenv("SUPPORT_CHANNEL", "https://t.me/fallenx")
        self.SUPPORT_CHAT = getenv("SUPPORT_CHAT", "https://t.me/DevilsHeavenMF")
        self.OWNER_USERNAME = getenv("OWNER_USERNAME", "khantpainghtet").strip("@ ")
        self.STARTGROUP_URLS = _parse_startgroup_urls(getenv("STARTGROUP_BOTS", ""))
        self.STARTGROUP_WEIGHTS = _parse_weights(
            getenv("STARTGROUP_WEIGHTS", ""),
            len(self.STARTGROUP_URLS),
        )

        self.AUTO_LEAVE: bool = getenv("AUTO_LEAVE", "False").lower() == "true"
        self.AUTO_END: bool = getenv("AUTO_END", "False").lower() == "true"
        # Teach-by-reply uses a persisted confirmation gate before activation.
        # Manual /reply rules remain immediate and never expire.
        self.AUTO_LEARN_USER_COOLDOWN_SEC = max(
            0.1, float(getenv("AUTO_LEARN_USER_COOLDOWN_SEC", "0.75") or 0.75)
        )
        self.AUTO_LEARN_KEYWORD_COOLDOWN_SEC = max(
            0.1, float(getenv("AUTO_LEARN_KEYWORD_COOLDOWN_SEC", "2") or 2)
        )
        self.AUTO_LEARN_USER_BURST = _int_env("AUTO_LEARN_USER_BURST", 12, 1)
        self.AUTO_LEARN_USER_BURST_WINDOW_SEC = max(
            1.0,
            float(getenv("AUTO_LEARN_USER_BURST_WINDOW_SEC", "60") or 60),
        )
        self.AUTO_LEARN_CONFIRMATIONS = _int_env("AUTO_LEARN_CONFIRMATIONS", 2, 2)
        self.AUTO_LEARN_TTL_HOURS = max(
            1.0, float(getenv("AUTO_LEARN_TTL_HOURS", "24") or 24)
        )
        self.AUTO_LEARN_CLEANUP_INTERVAL_SEC = _int_env(
            "AUTO_LEARN_CLEANUP_INTERVAL_SEC", 300, 60
        )
        self.AUTO_LEARN_CLEANUP_BATCH = _int_env(
            "AUTO_LEARN_CLEANUP_BATCH", 100, 1
        )
        # Delete the user's /play|/vplay command once track is queued or starts.
        self.AUTO_DELETE_PLAY_COMMAND: bool = _bool_env(
            "AUTO_DELETE_PLAY_COMMAND", True
        )
        # Timed delete of queue cards (optional). Preferred path is DYNAMIC:
        # delete when that track actually starts (play_next / now-playing).
        self.AUTO_DELETE_PLAY_QUEUED: bool = _bool_env("AUTO_DELETE_PLAY_QUEUED", False)
        self.AUTO_DELETE_PLAY_QUEUED_SEC = max(
            3, _int_env("AUTO_DELETE_PLAY_QUEUED_SEC", 12, 3)
        )
        # Delete searching/queued/play_next status when playback card posts (default ON)
        self.AUTO_DELETE_PLAY_STATUS_ON_START: bool = _bool_env(
            "AUTO_DELETE_PLAY_STATUS_ON_START", True
        )

        self.THUMB_GEN: bool = getenv("THUMB_GEN", "True").lower() == "true"
        self.VIDEO_PLAY: bool = getenv("VIDEO_PLAY", "True").lower() == "true"
        self.PREFETCH_NEXT: bool = getenv("PREFETCH_NEXT", "True").lower() == "true"
        self.PREFETCH_VIDEO: bool = getenv("PREFETCH_VIDEO", "True").lower() == "true"
        self.TELEGRAM_DIRECT_STREAM_ONLY: bool = (
            getenv("TELEGRAM_DIRECT_STREAM_ONLY", "False").lower() == "true"
        )
        self.TELEGRAM_DIRECT_CACHE_BG: bool = (
            getenv("TELEGRAM_DIRECT_CACHE_BG", "True").lower() == "true"
        )
        self.TELEGRAM_DIRECT_CACHE_TIMEOUT_SEC = _float_or_auto(
            "TELEGRAM_DIRECT_CACHE_TIMEOUT_SEC", 8
        )
        self.YOUTUBE_DIRECT_STREAM: bool = (
            getenv("YOUTUBE_DIRECT_STREAM", "True").lower() == "true"
        )
        self.YOUTUBE_DIRECT_STREAM_ONLY: bool = (
            getenv("YOUTUBE_DIRECT_STREAM_ONLY", "False").lower() == "true"
        )
        self.YOUTUBE_DIRECT_CACHE_BG: bool = (
            getenv("YOUTUBE_DIRECT_CACHE_BG", "False").lower() == "true"
        )
        self.YOUTUBE_DIRECT_CACHE_TIMEOUT_SEC = _float_or_auto(
            "YOUTUBE_DIRECT_CACHE_TIMEOUT_SEC", 8
        )
        # off (default): skip HTTP probe and try VC direct immediately.
        # soft: 403 soft-pass. strict: hard-fail probe 4xx.
        self.DIRECT_URL_PROBE = (
            getenv("DIRECT_URL_PROBE", "off") or "off"
        ).strip().lower()
        # off (default): skip the redundant local ffprobe/ffmpeg pre-open of the
        # direct URL. PyTgCalls re-opens the same URL when it starts the stream,
        # so this probe only adds pre-audio latency; the startup gate + deferred
        # local fallback already recover from a dead URL. Set True to restore the
        # eager ffmpeg pre-open (diagnostics / strict environments).
        self.DIRECT_AUDIO_PROBE: bool = (
            getenv("DIRECT_AUDIO_PROBE", "False").lower() == "true"
        )
        # Initial direct audio has already passed our GVS 200/206 preflight.
        # Feeding that URL back through PyTgCalls MediaStream makes play() run
        # MediaStream.check_stream() and a second remote FFmpeg probe before the
        # Telegram join. Use a raw SHELL AudioStream for this already-proven
        # audio URL so VC attach does not pay that duplicate probe. ShellError
        # still falls back to the stable MediaStream path and disables raw mode
        # for the rest of the process. /vplay keeps the stable MediaStream path.
        self.DIRECT_PREVALIDATED_RAW_AUDIO: bool = _bool_env(
            "DIRECT_PREVALIDATED_RAW_AUDIO", True
        )
        # The Python observer wrapper adds an extra process hop before ffmpeg.
        # Outgoing-clock telemetry already proves first packets, so keep the
        # wrapper off on the latency path unless explicitly debugging.
        self.FFMPEG_BINARY: str = (getenv("FFMPEG_BINARY", "ffmpeg") or "ffmpeg").strip()
        self.DIRECT_RAW_OBSERVER: bool = _bool_env("DIRECT_RAW_OBSERVER", False)
        # Validate the exact Boost-safe raw FFmpeg launcher chain at voice-service
        # boot. A short local-only probe catches ENOENT/dynamic-loader problems
        # before the first late-join VC attempt reaches NTgCalls.
        self.DIRECT_RAW_LAUNCH_PROBE_TIMEOUT_SEC = max(
            1.0,
            float(getenv("DIRECT_RAW_LAUNCH_PROBE_TIMEOUT_SEC", "3") or 3),
        )
        # Warm only Telegram/PyTgCalls metadata while YouTube resolves. This does
        # not join the assistant to VC; it primes input-call + unmute call refs.
        self.DIRECT_VC_METADATA_PREWARM: bool = _bool_env(
            "DIRECT_VC_METADATA_PREWARM", True
        )
        # V4 owns one native-call binding transaction per assistant/chat.  It
        # removes speculative create_call/reset/retry races and keeps unmute and
        # video attachment outside the real-audio submission critical path.
        # Set False for an immediate rollback while a canary is being observed.
        self.DIRECT_STARTUP_V4: bool = _bool_env("DIRECT_STARTUP_V4", True)
        self.DIRECT_VC_METADATA_TTL_SEC = max(
            5.0, float(getenv("DIRECT_VC_METADATA_TTL_SEC", "20") or 20)
        )
        # Pre-create only NTgCalls' local call payload while YouTube resolves.
        # PyTgCalls itself does this immediately before the MTProto JoinGroupCall
        # request. Moving that local WebRTC/SDP setup earlier does NOT make the
        # assistant visible in VC; the actual Telegram join still happens only
        # after the media source is ready. This optimization is version-pinned
        # to the bundled PyTgCalls 2.2.x API and falls back safely if unavailable.
        self.DIRECT_VC_NATIVE_PREWARM: bool = _bool_env(
            "DIRECT_VC_NATIVE_PREWARM", True
        )
        self.DIRECT_VC_NATIVE_PREWARM_TTL_SEC = max(
            3.0, float(getenv("DIRECT_VC_NATIVE_PREWARM_TTL_SEC", "12") or 12)
        )
        # Seed PyTgCalls' internal InputGroupCall cache from the same direct
        # metadata lookup and consume the pre-created NTgCalls payload on the
        # first raw play. Source lists are not needed for outgoing audio, so the
        # participant refresh can run after attach instead of extending TTFP.
        self.DIRECT_VC_DEFER_SOURCE_REFRESH: bool = _bool_env(
            "DIRECT_VC_DEFER_SOURCE_REFRESH", True
        )
        # Required self-unmute is allowed to race *after source-ready* with the
        # WebRTC connect. The retry window never starts before playback calls
        # _play_with_startup_slot, so this preserves strict late-join semantics
        # while removing the sequential ~200-300 ms unmute tail.
        self.DIRECT_VC_UNMUTE_OVERLAP: bool = _bool_env(
            "DIRECT_VC_UNMUTE_OVERLAP", True
        )
        self.DIRECT_VC_UNMUTE_INITIAL_DELAY_MS = max(
            100, int(getenv("DIRECT_VC_UNMUTE_INITIAL_DELAY_MS", "300") or 300)
        )
        self.DIRECT_VC_UNMUTE_RETRY_MS = max(
            50, int(getenv("DIRECT_VC_UNMUTE_RETRY_MS", "100") or 100)
        )
        self.DIRECT_VC_UNMUTE_ATTEMPTS = max(
            1, min(8, int(getenv("DIRECT_VC_UNMUTE_ATTEMPTS", "5") or 5))
        )
        # NTgCalls outgoing time is a local binding query. Poll it tightly only
        # during cold-start so first-packet proof/ready work is not delayed by
        # the old 50 ms observer cadence.
        self.DIRECT_FIRST_PACKET_POLL_MS = max(
            10, min(100, int(getenv("DIRECT_FIRST_PACKET_POLL_MS", "20") or 20))
        )
        # For fresh direct audio, start FFmpeg as soon as the signed URL has
        # passed the 206 preflight and feed predecoded 10ms PCM through NTgCalls
        # EXTERNAL capture immediately after VC connect. This overlaps decoder
        # startup with the late-join handshake without joining before source-ready.
        self.DIRECT_EXTERNAL_PREBUFFER_AUDIO: bool = _bool_env(
            "DIRECT_EXTERNAL_PREBUFFER_AUDIO", True
        )
        self.DIRECT_EXTERNAL_PREBUFFER_FRAMES = max(
            2, min(50, int(getenv("DIRECT_EXTERNAL_PREBUFFER_FRAMES", "4") or 4))
        )
        self.DIRECT_EXTERNAL_PREBUFFER_READY_TIMEOUT_SEC = max(
            0.05, min(1.5, float(
                getenv("DIRECT_EXTERNAL_PREBUFFER_READY_TIMEOUT_SEC", "0.35") or 0.35
            ))
        )
        # Early-connect/JIT path: install an EXTERNAL capture source while the
        # signed YouTube URL is still resolving, then feed silence until real PCM
        # is ready. This keeps the VC/WebRTC connect off the resolver critical path.
        self.DIRECT_EXTERNAL_JIT_FEED: bool = _bool_env(
            "DIRECT_EXTERNAL_JIT_FEED", True
        )
        self.DIRECT_EXTERNAL_JIT_RETRY_MS = max(
            5, min(50, int(getenv("DIRECT_EXTERNAL_JIT_RETRY_MS", "10") or 10))
        )
        self.DIRECT_EXTERNAL_REAL_FRAME_RETRY_MS = max(
            50,
            min(
                200,
                int(getenv("DIRECT_EXTERNAL_REAL_FRAME_RETRY_MS", "200") or 200),
            ),
        )
        self.DIRECT_EXTERNAL_EARLY_CONNECT_READY_TIMEOUT_SEC = max(
            0.5, min(6.0, float(getenv("DIRECT_EXTERNAL_EARLY_CONNECT_READY_TIMEOUT_SEC", "4.0") or 4.0))
        )
        self.DIRECT_VIDEO_VC_RESOLVER_OVERLAP: bool = _bool_env(
            "DIRECT_VIDEO_VC_RESOLVER_OVERLAP", True
        )
        self.DIRECT_VIDEO_AUDIO_LEAD_PACKET_TIMEOUT_SEC = max(
            0.10,
            min(
                0.60,
                float(
                    getenv(
                        "DIRECT_VIDEO_AUDIO_LEAD_PACKET_TIMEOUT_SEC",
                        "0.40",
                    )
                    or 0.40
                ),
            ),
        )
        self.DIRECT_PREVALIDATED_RAW_VIDEO: bool = _bool_env(
            "DIRECT_PREVALIDATED_RAW_VIDEO", True
        )
        # Legacy opt-in raw path for video/experiments. Audio no longer depends
        # on this flag because DIRECT_PREVALIDATED_RAW_AUDIO owns that path.
        self.DIRECT_RAW_COLD_PATH: bool = _bool_env(
            "DIRECT_RAW_COLD_PATH", False
        )
        # On a googlevideo 403 the URL's PO-token/visitor binding was rejected,
        # so the cached URL and yt-dlp's process-global PO cache are dropped and
        # the video is re-extracted once from a fresh context before the
        # existing local-download fallback takes over.
        self.DIRECT_403_RETRY_ENABLED: bool = _bool_env(
            "DIRECT_403_RETRY_ENABLED", True
        )
        # A googlevideo URL is bound to the IP that requested it. On a
        # dual-stack VPS yt-dlp can mint it over IPv6 while ffmpeg later opens
        # it over IPv4, and Google answers 403. Extraction and every media
        # fetch bind the one address resolved by AnonX_3.core.netbind, so the
        # two hops cannot diverge. "auto" prefers a globally routable IPv6,
        # "off" keeps the kernel default, a literal address is used verbatim.
        self.YTDLP_SOURCE_ADDRESS: str = getenv("YTDLP_SOURCE_ADDRESS", "auto")
        self.YTDLP_FORCE_IPV6: bool = _bool_env("YTDLP_FORCE_IPV6", True)
        # Early direct→local failover (shorter = faster recovery, less silent hang).
        self.DIRECT_FAILOVER_WINDOW_SEC = max(
            5.0, float(getenv("DIRECT_FAILOVER_WINDOW_SEC", "12") or 12)
        )
        self.DIRECT_START_PROOF_SEC = max(
            0.0, float(getenv("DIRECT_START_PROOF_SEC", "3") or 3)
        )
        self.DIRECT_MIDSTREAM_FAILOVER: bool = _bool_env(
            "DIRECT_MIDSTREAM_FAILOVER", True
        )
        self.YTDLP_PLAYER_CLIENTS = (
            getenv("YTDLP_PLAYER_CLIENTS", "default") or "default"
        ).strip()
        # Authoritative direct-resolve path.
        # Direct playback has one foreground resolver only: the configured
        # PO-token client (mweb by default) with the bgutil provider.
        self.DIRECT_AUTHORITATIVE_POT_PREFLIGHT_TIMEOUT_SEC = max(
            0.5,
            float(
                getenv("DIRECT_AUTHORITATIVE_POT_PREFLIGHT_TIMEOUT_SEC", "1.5")
                or 1.5
            ),
        )
        self.DIRECT_FOREGROUND_DEFER_PREFLIGHT: bool = _bool_env(
            "DIRECT_FOREGROUND_DEFER_PREFLIGHT", True
        )
        # Direct mweb latency optimization. yt-dlp documents
        # use_ad_playback_context for mweb/web_music as a way to eliminate
        # the mandatory pre-download/ad-playback wait. Keep it scoped to the
        # foreground direct resolver so normal downloads retain conservative
        # extractor behavior.
        self.DIRECT_MWEB_USE_AD_PLAYBACK_CONTEXT: bool = _bool_env(
            "DIRECT_MWEB_USE_AD_PLAYBACK_CONTEXT", True
        )
        # initial_data is not required for a direct format URL and may trigger
        # an extra next/initial-data request. We deliberately do not skip the
        # webpage, client config, or player JS because those are needed for
        # robust cookie/session binding and nsig solving.
        self.DIRECT_MWEB_SKIP_INITIAL_DATA: bool = _bool_env(
            "DIRECT_MWEB_SKIP_INITIAL_DATA", True
        )
        # Foreground /play and /vplay first try a lightweight mweb Innertube
        # profile that skips the watch webpage but keeps the mweb client-config
        # request needed for authenticated Data Sync ID/GVS POT and adaptive
        # audio formats. Any miss immediately falls back to the robust profile.
        self.DIRECT_MWEB_LIGHTWEIGHT: bool = _bool_env(
            "DIRECT_MWEB_LIGHTWEIGHT", True
        )
        # Guarded <=1.5s lane: use yt-dlp's pinned private player API to fetch
        # a directly usable media URL without running full extract_info(). Safe
        # signed envelopes are recovered; JS-ciphered/rejected URLs fall back.
        self.DIRECT_MWEB_MICRO_PLAYER: bool = _bool_env(
            "DIRECT_MWEB_MICRO_PLAYER", True
        )
        # Race the tiny player-response lane beside the first authoritative
        # full extractor. A micro miss therefore adds no serial latency.
        self.DIRECT_RESOLVER_PARALLEL_MICRO: bool = _bool_env(
            "DIRECT_RESOLVER_PARALLEL_MICRO", True
        )
        self.DIRECT_MICRO_PREFLIGHT_TIMEOUT_SEC = max(
            0.25, min(1.5, float(getenv("DIRECT_MICRO_PREFLIGHT_TIMEOUT_SEC", "0.75") or 0.75))
        )
        _micro_client = (
            getenv("DIRECT_MICRO_PLAYER_CLIENT", "tv_downgraded")
            or "tv_downgraded"
        ).strip()
        self.DIRECT_MICRO_PLAYER_CLIENT: str = (
            _micro_client
            if _micro_client
            in {
                "tv_downgraded",
                "web_safari",
                "android_vr",
                "web_embedded",
                "mweb",
            }
            else "tv_downgraded"
        )
        _micro_clients_raw = (
            getenv(
                "DIRECT_MICRO_PLAYER_CLIENTS",
                "tv_downgraded,web_safari,android_vr",
            )
            or "tv_downgraded,web_safari,android_vr"
        )
        _micro_allowed = {
            "tv_downgraded",
            "web_safari",
            "android_vr",
            "web_embedded",
            "mweb",
        }
        _micro_clients: list[str] = []
        for _name in _micro_clients_raw.split(","):
            _name = _name.strip()
            if _name in _micro_allowed and _name not in _micro_clients:
                _micro_clients.append(_name)
        if self.DIRECT_MICRO_PLAYER_CLIENT not in _micro_clients:
            _micro_clients.insert(0, self.DIRECT_MICRO_PLAYER_CLIENT)
        self.DIRECT_MICRO_PLAYER_CLIENTS: tuple[str, ...] = tuple(_micro_clients[:3])
        self.DIRECT_MICRO_LANE_TIMEOUT_SEC = max(
            0.35,
            min(2.5, float(getenv("DIRECT_MICRO_LANE_TIMEOUT_SEC", "1.35") or 1.35)),
        )
        self.DIRECT_MICRO_PROBE_TIMEOUT_SEC = max(
            0.20,
            min(1.5, float(getenv("DIRECT_MICRO_PROBE_TIMEOUT_SEC", "0.65") or 0.65)),
        )
        # End-to-end cap for player response + one-byte GVS proof. Keeping the
        # speculative lane below the 1.5s SLO prevents it from delaying the
        # authoritative full-extractor fallback.
        self.DIRECT_MICRO_TOTAL_BUDGET_SEC = max(
            0.50,
            min(
                1.45,
                float(getenv("DIRECT_MICRO_TOTAL_BUDGET_SEC", "1.45") or 1.45),
            ),
        )
        self.DIRECT_VIDEO_ADAPTIVE_PAIR: bool = _bool_env(
            "DIRECT_VIDEO_ADAPTIVE_PAIR", True
        )
        self.DIRECT_VIDEO_FAST_STAGE_TIMEOUT_SEC = max(
            1.0,
            min(4.0, float(getenv("DIRECT_VIDEO_FAST_STAGE_TIMEOUT_SEC", "2.20") or 2.20)),
        )
        # Second authoritative full-resolver hedge on the other sticky worker.
        # This is specifically for cold /play outliers where all micro clients miss.
        self.DIRECT_AUDIO_ESCAPE_RACE: bool = _bool_env(
            "DIRECT_AUDIO_ESCAPE_RACE", True
        )
        self.DIRECT_RESOLVER_PREWARM: bool = _bool_env(
            "DIRECT_RESOLVER_PREWARM", True
        )
        self.DIRECT_RESOLVER_WORKERS = max(
            1, min(4, int(getenv("DIRECT_RESOLVER_WORKERS", "2") or 2))
        )
        # Cold direct startup races must remain truly parallel even when the
        # dynamic resource controller temporarily shrinks the general yt-dlp
        # lane to one permit. This budget is scoped only to the two lightweight
        # foreground direct resolvers and is still bounded by sticky workers.
        self.DIRECT_FOREGROUND_RESOLVER_SLOTS = max(
            1,
            min(
                self.DIRECT_RESOLVER_WORKERS,
                int(getenv("DIRECT_FOREGROUND_RESOLVER_SLOTS", "2") or 2),
            ),
        )
        # Pre-create the sticky YoutubeDL/plugin/cookie-jar state after startup
        # cookie health completes. This does not resolve a real video; it only
        # removes constructor/plugin initialization from the first /play/vplay.
        self.DIRECT_RESOLVER_STARTUP_WARM: bool = _bool_env(
            "DIRECT_RESOLVER_STARTUP_WARM", True
        )
        self.DIRECT_RESOLVER_STARTUP_WARM_TIMEOUT_SEC = max(
            1.0,
            float(getenv("DIRECT_RESOLVER_STARTUP_WARM_TIMEOUT_SEC", "4.0") or 4.0),
        )
        # Dual-stage audio: start playback with the fast validated AAC source,
        # then mint exact itag 140 + visible POT after audible and for queued-next.
        self.DIRECT_BACKGROUND_140_ENABLED: bool = _bool_env(
            "DIRECT_BACKGROUND_140_ENABLED", True
        )
        self.DIRECT_BACKGROUND_140_TTL_SEC = max(
            60.0,
            float(getenv("DIRECT_BACKGROUND_140_TTL_SEC", "600") or 600),
        )
        self.DIRECT_BACKGROUND_140_WORKERS = max(
            1, min(2, int(getenv("DIRECT_BACKGROUND_140_WORKERS", "1") or 1))
        )
        # Cookie-free is the safe public-media default. Set False only when
        # restricted/account-only YouTube content explicitly requires cookies.
        self.COOKIE_FREE_MODE: bool = _bool_env("COOKIE_FREE_MODE", False)
        # Dynamic challenge recovery is enabled by default but remains inert
        # unless an explicit supported browser and dedicated profile are set.
        self.COOKIE_AUTH_RECOVERY_ENABLED: bool = _bool_env(
            "COOKIE_AUTH_RECOVERY_ENABLED", True
        )
        # Static cookie file mode (external youtube-cookie-guard manages refresh)
        self.AUTO_COOKIE_ENABLED: bool = _bool_env("AUTO_COOKIE_ENABLED", True)
        self.COOKIE_BROWSER = (
            getenv("COOKIE_BROWSER", "firefox") or "firefox"
        ).strip().lower()
        self.COOKIE_BROWSER_PROFILE = (
            getenv("COOKIE_BROWSER_PROFILE", "/root/firefox-profile") or "/root/firefox-profile"
        ).strip()
        self.COOKIE_BROWSER_WARMUP: bool = _bool_env("COOKIE_BROWSER_WARMUP", True)
        self.COOKIE_BROWSER_TIMEOUT_SEC = _int_env(
            "COOKIE_BROWSER_TIMEOUT_SEC", 30, 5
        )
        self.YOUTUBE_COOKIE_FILE = (
            getenv("YOUTUBE_COOKIE_FILE", "/root/youtube-cookies.txt") or "/root/youtube-cookies.txt"
        ).strip()
        self.YTDLP_BINARY = (
            getenv("YTDLP_BINARY", "/usr/local/bin/yt-dlp") or "/usr/local/bin/yt-dlp"
        ).strip()
        self.YTDLP_JS_RUNTIME = (
            getenv("YTDLP_JS_RUNTIME", "deno:/usr/local/bin/deno")
            or "deno:/usr/local/bin/deno"
        ).strip()
        self.YTDLP_REMOTE_COMPONENTS = (
            getenv("YTDLP_REMOTE_COMPONENTS", "ejs:github") or "ejs:github"
        ).strip()
        self.YTDLP_POT_SERVER_HOME = (
            getenv(
                "YTDLP_POT_SERVER_HOME",
                "/root/bgutil-ytdlp-pot-provider/server",
            )
            or "/root/bgutil-ytdlp-pot-provider/server"
        ).strip()
        self.COOKIE_REFRESH_SEC = _int_env("COOKIE_REFRESH_SEC", 21600, 300)
        self.COOKIE_EXPIRY_WINDOW_SEC = _int_env(
            "COOKIE_EXPIRY_WINDOW_SEC", 604800, 3600
        )
        self.COOKIE_FAILURE_COOLDOWN_SEC = _int_env(
            "COOKIE_FAILURE_COOLDOWN_SEC", 300, 30
        )
        # Real-time cookie watcher (dynamic sync from configured Firefox profile)
        self.COOKIE_WATCHER_ENABLED: bool = _bool_env(
            "COOKIE_WATCHER_ENABLED", False
        )
        self.COOKIE_WATCHER_USER_DATA_DIR = (
            getenv("COOKIE_WATCHER_USER_DATA_DIR", "") or ""
        ).strip()
        self.COOKIE_WATCHER_INTERVAL_SEC = _int_env(
            "COOKIE_WATCHER_INTERVAL_SEC", 60, 30
        )
        self.COOKIE_WATCHER_YOUTUBE_ONLY: bool = _bool_env(
            "COOKIE_WATCHER_YOUTUBE_ONLY", True
        )
        self.SINGLEFLIGHT_BACKEND = (
            getenv("SINGLEFLIGHT_BACKEND", "memory") or "memory"
        ).strip().lower()
        self.REDIS_RESULT_TTL_SEC = _int_env("REDIS_RESULT_TTL_SEC", 300, 30)
        # Resource manager / yt-dlp resilience (Phase B)
        self.MAX_YTDLP_CONCURRENT = _int_env("MAX_YTDLP_CONCURRENT", 2, 1)
        self.MAX_DOWNLOAD_CONCURRENT = _int_env("MAX_DOWNLOAD_CONCURRENT", 2, 1)
        self.MAX_FFMPEG_CONCURRENT = _int_env("MAX_FFMPEG_CONCURRENT", 3, 1)
        self.MAX_VIDEO_JOBS = _int_env("MAX_VIDEO_JOBS", 1, 1)
        self.MAX_ACTIVE_STREAMS = _int_env("MAX_ACTIVE_STREAMS", 20, 1)
        # --- Dynamic Resource Control ---
        # The MAX_* values above stay the guaranteed fallback contract. When
        # DYNAMIC_RESOURCE_CONTROL is on, each lane's live capacity is recomputed
        # from CPU/RAM/loadavg/event-loop lag plus real queued demand, bounded by
        # a per-lane floor and ceiling. Any internal failure latches back to the
        # MAX_* numbers for the rest of the process lifetime.
        self.DYNAMIC_RESOURCE_CONTROL: bool = _bool_env(
            "DYNAMIC_RESOURCE_CONTROL", True
        )
        # Growth ceiling as a multiple of the fixed limit (also bounded by cores
        # and RAM headroom). Never unlimited.
        self.DYNAMIC_CAPACITY_MAX_MULTIPLIER = min(
            8.0, max(1.0, float(getenv("DYNAMIC_CAPACITY_MAX_MULTIPLIER", "4") or 4))
        )
        # Pressure below this ⇒ allowed to grow with demand.
        self.DYNAMIC_PRESSURE_GROW_BELOW = min(
            0.95, max(0.10, float(getenv("DYNAMIC_PRESSURE_GROW_BELOW", "0.55") or 0.55))
        )
        # Pressure at or above this ⇒ shrink straight to the per-lane floor.
        self.DYNAMIC_PRESSURE_RELIEF = min(
            0.99, max(0.20, float(getenv("DYNAMIC_PRESSURE_RELIEF", "0.85") or 0.85))
        )
        # Event-loop lag that counts as fully saturated (ms). This is the signal
        # that actually predicts VC audio stutter on a GIL-bound bot.
        self.DYNAMIC_LOOP_LAG_HIGH_MS = _int_env("DYNAMIC_LOOP_LAG_HIGH_MS", 250, 40)
        self.DYNAMIC_LOAD_PER_CORE_HIGH = max(
            0.5, float(getenv("DYNAMIC_LOAD_PER_CORE_HIGH", "1.5") or 1.5)
        )
        self.DYNAMIC_RECOMPUTE_INTERVAL_SEC = max(
            0.5, float(getenv("DYNAMIC_RECOMPUTE_INTERVAL_SEC", "2") or 2)
        )
        # Pause background cache/prefetch/thumbnail admission while any
        # foreground /play or /vplay request is queued.
        self.DYNAMIC_BACKGROUND_PAUSE: bool = _bool_env(
            "DYNAMIC_BACKGROUND_PAUSE", True
        )
        # Permits background work must leave free for foreground bursts.
        self.DYNAMIC_FOREGROUND_RESERVE = _int_env("DYNAMIC_FOREGROUND_RESERVE", 1, 0)
        # Optional explicit per-lane bounds (0 = derive from machine + MAX_*).
        self.DYNAMIC_YTDLP_CEILING = _int_env("DYNAMIC_YTDLP_CEILING", 0, 0)
        self.DYNAMIC_DOWNLOADS_CEILING = _int_env("DYNAMIC_DOWNLOADS_CEILING", 0, 0)
        self.DYNAMIC_VIDEO_CEILING = _int_env("DYNAMIC_VIDEO_CEILING", 0, 0)
        self.DYNAMIC_FFMPEG_CEILING = _int_env("DYNAMIC_FFMPEG_CEILING", 0, 0)
        self.DYNAMIC_STREAMS_CEILING = _int_env("DYNAMIC_STREAMS_CEILING", 0, 0)
        self.DYNAMIC_YTDLP_FLOOR = _int_env("DYNAMIC_YTDLP_FLOOR", 0, 0)
        self.DYNAMIC_DOWNLOADS_FLOOR = _int_env("DYNAMIC_DOWNLOADS_FLOOR", 0, 0)
        self.DYNAMIC_VIDEO_FLOOR = _int_env("DYNAMIC_VIDEO_FLOOR", 0, 0)
        self.DYNAMIC_FFMPEG_FLOOR = _int_env("DYNAMIC_FFMPEG_FLOOR", 0, 0)
        self.DYNAMIC_STREAMS_FLOOR = _int_env("DYNAMIC_STREAMS_FLOOR", 0, 0)
        # ── Stream scaling ──────────────────────────────────────────────
        # MAX_ACTIVE_STREAMS is the *baseline*, not the cap. With
        # DYNAMIC_STREAMS_CEILING=0 the safe ceiling is derived at runtime from
        # CPU, RAM, event-loop lag, active streams and refused stream requests.
        # A relayed VC stream costs one ntgcalls pipe plus loop share — far
        # less than a yt-dlp/FFmpeg job — so it is sized with its own units.
        self.DYNAMIC_STREAM_RAM_MB = max(
            1.0, float(getenv("DYNAMIC_STREAM_RAM_MB", "24") or 24)
        )
        self.DYNAMIC_STREAMS_PER_CORE = _int_env("DYNAMIC_STREAMS_PER_CORE", 16, 1)
        self.DYNAMIC_STREAM_RAM_RESERVE_MB = max(
            0.0, float(getenv("DYNAMIC_STREAM_RAM_RESERVE_MB", "512") or 512)
        )
        # How long a refused /play keeps counting as a waiting stream request.
        self.DYNAMIC_STREAM_DEMAND_WINDOW_SEC = max(
            1.0, float(getenv("DYNAMIC_STREAM_DEMAND_WINDOW_SEC", "20") or 20)
        )
        # Spare slots kept above observed demand so the next group does not
        # have to be refused once before capacity grows.
        self.DYNAMIC_STREAM_HEADROOM = _int_env("DYNAMIC_STREAM_HEADROOM", 2, 0)
        # Internal seconds only — always mirrors DURATION_LIMIT (env minutes).
        # No MAX_MEDIA_DURATION_SEC env; use DURATION_LIMIT=180 only.
        self.MAX_MEDIA_DURATION_SEC = int(getattr(self, "DURATION_LIMIT", 0) or 0)
        self.MAX_DOWNLOAD_MB = _int_env("MAX_DOWNLOAD_MB", 512, 16)
        self.YTDLP_MAX_RETRIES = _int_env("YTDLP_MAX_RETRIES", 3, 1)
        self.YOUTUBE_AUTH_CHALLENGE_COOLDOWN_SEC = _int_env(
            "YOUTUBE_AUTH_CHALLENGE_COOLDOWN_SEC", 180, 30
        )
        self.DISK_HIGH_WATER_PCT = min(
            98, max(50, _int_env("DISK_HIGH_WATER_PCT", 85, 50))
        )
        self.DISK_TARGET_PCT = min(
            95, max(40, _int_env("DISK_TARGET_PCT", 75, 40))
        )
        self.CDN_UNCOMMON_TTL_HOURS = max(
            1.0, float(getenv("CDN_UNCOMMON_TTL_HOURS", "6") or 6)
        )
        self.CDN_POPULAR_TTL_HOURS = max(
            1.0, float(getenv("CDN_POPULAR_TTL_HOURS", "48") or 48)
        )
        self.CDN_TMP_GRACE_SEC = max(
            600, _int_env("CDN_TMP_GRACE_SEC", 7200, 600)
        )
        # Phase C — SoundCloud fallback + optional PO Token Provider
        self.FALLBACK_SOUNDCLOUD: bool = _bool_env("FALLBACK_SOUNDCLOUD", True)
        self.FALLBACK_MIN_SCORE = max(
            0.0, min(1.0, float(getenv("FALLBACK_MIN_SCORE", "0.85") or 0.85))
        )
        self.FALLBACK_SOFT_MIN_SCORE = max(
            0.0, min(1.0, float(getenv("FALLBACK_SOFT_MIN_SCORE", "0.70") or 0.70))
        )
        self.FALLBACK_SEARCH_LIMIT = _int_env("FALLBACK_SEARCH_LIMIT", 8, 1)
        self.FALLBACK_QUERY_ATTEMPTS = _int_env(
            "FALLBACK_QUERY_ATTEMPTS", 2, 1
        )
        # Allow provider fallback and metadata recovery to finish before /play
        # reports a retryable timeout.
        self.PLAY_RESOLVE_TIMEOUT_SEC = min(
            60, _int_env("PLAY_RESOLVE_TIMEOUT_SEC", 18, 8)
        )
        self.SOUNDCLOUD_PROXY_TIMEOUT_SEC = _int_env(
            "SOUNDCLOUD_PROXY_TIMEOUT_SEC", 4, 1
        )
        self.SOUNDCLOUD_DIRECT_TIMEOUT_SEC = _int_env(
            "SOUNDCLOUD_DIRECT_TIMEOUT_SEC", 5, 1
        )
        self.SOUNDCLOUD_CANDIDATE_PROBE_LIMIT = _int_env(
            "SOUNDCLOUD_CANDIDATE_PROBE_LIMIT", 3, 1
        )
        # Soft-score band (0.70–0.84) auto-use only when True; default hard ≥0.85 only
        self.FALLBACK_SOFT_AUTO: bool = _bool_env("FALLBACK_SOFT_AUTO", False)
        self.PO_TOKEN_PROVIDER_ENABLED: bool = _bool_env(
            "PO_TOKEN_PROVIDER_ENABLED", True
        )
        self.PO_TOKEN_PROVIDER_URL = _po_token_provider_url(
            self.PO_TOKEN_PROVIDER_ENABLED
        )
        self.POT_PROVIDER_URL = (
            getenv("POT_PROVIDER_URL", self.PO_TOKEN_PROVIDER_URL)
            or self.PO_TOKEN_PROVIDER_URL
        ).strip().rstrip("/")
        self.PO_TOKEN_CLIENT = (
            getenv("PO_TOKEN_CLIENT", "mweb") or "mweb"
        ).strip() or "mweb"
        self.PO_TOKEN_CACHE_SEC = _int_env("PO_TOKEN_CACHE_SEC", 300, 30)
        self.PO_TOKEN_TIMEOUT_SEC = _int_env("PO_TOKEN_TIMEOUT_SEC", 12, 3)
        self.POT_TOKEN_HEALTH_COOLDOWN_SEC = _int_env(
            "POT_TOKEN_HEALTH_COOLDOWN_SEC", 1800, 300
        )
        # Phase D — health/metrics + optional Redis singleflight
        self.HEALTH_PORT = _int_env("HEALTH_PORT", 0, 0)
        self.HEALTH_HOST = (getenv("HEALTH_HOST", "127.0.0.1") or "127.0.0.1").strip()
        self.HEALTH_TOKEN = (getenv("HEALTH_TOKEN", "") or "").strip()
        self.REDIS_URL = (getenv("REDIS_URL", "") or "").strip()
        self.REDIS_LOCK_TTL_SEC = _int_env("REDIS_LOCK_TTL_SEC", 120, 30)
        self.TIKTOK_DIRECT_STREAM: bool = (
            getenv("TIKTOK_DIRECT_STREAM", "True").lower() == "true"
        )
        self.TIKTOK_DIRECT_STREAM_ONLY: bool = (
            getenv("TIKTOK_DIRECT_STREAM_ONLY", "False").lower() == "true"
        )
        self.TIKTOK_DIRECT_CACHE_BG: bool = (
            getenv("TIKTOK_DIRECT_CACHE_BG", "True").lower() == "true"
        )
        self.TIKTOK_DIRECT_CACHE_TIMEOUT_SEC = _float_or_auto(
            "TIKTOK_DIRECT_CACHE_TIMEOUT_SEC", 8
        )
        self.FACEBOOK_DIRECT_STREAM: bool = (
            getenv("FACEBOOK_DIRECT_STREAM", "True").lower() == "true"
        )
        self.FACEBOOK_DIRECT_STREAM_ONLY: bool = (
            getenv("FACEBOOK_DIRECT_STREAM_ONLY", "False").lower() == "true"
        )
        self.FACEBOOK_DIRECT_CACHE_BG: bool = (
            getenv("FACEBOOK_DIRECT_CACHE_BG", "True").lower() == "true"
        )
        self.FACEBOOK_DIRECT_CACHE_TIMEOUT_SEC = _float_or_auto(
            "FACEBOOK_DIRECT_CACHE_TIMEOUT_SEC", 8
        )
        # Share/shortener links (facebook.com/share/..., fb.watch/...,
        # vm.tiktok.com/...) hide their target behind a redirect. Disabling
        # this keeps only the offline URL rewrite.
        self.SOCIAL_URL_RESOLVE_ENABLED: bool = (
            getenv("SOCIAL_URL_RESOLVE_ENABLED", "True").lower() == "true"
        )
        self.SOCIAL_URL_RESOLVE_TIMEOUT_SEC = _float_or_auto(
            "SOCIAL_URL_RESOLVE_TIMEOUT_SEC", 8
        )
        self.SOCIAL_URL_RESOLVE_MAX_HOPS = _int_env("SOCIAL_URL_RESOLVE_MAX_HOPS", 5, 1)
        self.SOCIAL_URL_CACHE_TTL_SEC = _int_env("SOCIAL_URL_CACHE_TTL_SEC", 3600, 60)
        # A failed hop is remembered only briefly, so an unreachable provider
        # cannot stall the foreground /play path once per attempt.
        self.SOCIAL_URL_NEGATIVE_TTL_SEC = _int_env(
            "SOCIAL_URL_NEGATIVE_TTL_SEC", 60, 5
        )
        self.YOUTUBE_API_KEY = getenv("YOUTUBE_API_KEY", "").strip()
        self.YOUTUBE_API_RELOAD_SEC = _float_or_auto(
            "YOUTUBE_API_RELOAD_SEC", "auto"
        )
        self.DEEPSEEK_API_KEY = getenv("DEEPSEEK_API_KEY", "").strip()
        self.DEEPSEEK_MODEL = getenv("DEEPSEEK_MODEL", "deepseek-v4-pro").strip() or "deepseek-v4-pro"
        self.DEEPSEEK_FAST_MODEL = (
            getenv("DEEPSEEK_FAST_MODEL", "deepseek-v4-flash").strip()
            or "deepseek-v4-flash"
        )
        self.DEEPSEEK_REVIEW_ENABLED: bool = _bool_env("DEEPSEEK_REVIEW_ENABLED", True)
        self.DEEPSEEK_REVIEW_MODEL = getenv("DEEPSEEK_REVIEW_MODEL", "").strip()
        self.DEEPSEEK_REVIEW_TIMEOUT_SEC = _int_env(
            "DEEPSEEK_REVIEW_TIMEOUT_SEC", 8, 3
        )
        self.DEEPSEEK_ASSISTANT_TIMEOUT_SEC = _int_env(
            "DEEPSEEK_ASSISTANT_TIMEOUT_SEC", 35, 10
        )
        self.DEEPSEEK_FAST_TIMEOUT_SEC = _int_env(
            "DEEPSEEK_FAST_TIMEOUT_SEC", 15, 5
        )
        self.DEEPSEEK_AUTH_FAILURE_COOLDOWN_SEC = _int_env(
            "DEEPSEEK_AUTH_FAILURE_COOLDOWN_SEC", 3600, 300
        )
        self.DEEPSEEK_ERROR_MONITOR: bool = _bool_env("DEEPSEEK_ERROR_MONITOR", True)
        self.DEEPSEEK_ERROR_ANALYZE: bool = _bool_env("DEEPSEEK_ERROR_ANALYZE", True)
        self.DEEPSEEK_ERROR_MIN_LEVEL = getenv("DEEPSEEK_ERROR_MIN_LEVEL", "ERROR").upper()
        self.DEEPSEEK_ERROR_COOLDOWN_SEC = _int_env("DEEPSEEK_ERROR_COOLDOWN_SEC", 60, 0)
        self.DEEPSEEK_ERROR_TIMEOUT_SEC = _int_env("DEEPSEEK_ERROR_TIMEOUT_SEC", 18, 3)
        self.DEEPSEEK_ERROR_MAX_CHARS = _int_env("DEEPSEEK_ERROR_MAX_CHARS", 3500, 800)
        # Dynamic local-VPS proxy + auto port detect (default auto; no .env needed)
        _yp_raw = (getenv("YOUTUBE_PROXY", "auto") or "auto").strip()
        if not _yp_raw and (getenv("PROXY_URL") or "").strip():
            _yp_raw = (getenv("PROXY_URL") or "").strip()
        self.YOUTUBE_PROXY_RAW = _yp_raw
        self.YOUTUBE_PROXY_MODE = (
            "off"
            if _yp_raw.lower() in {"off", "false", "0", "no", "none", "disable", "disabled"}
            else (
                "explicit"
                if _yp_raw
                and _yp_raw.lower()
                not in {"", "auto", "dynamic", "default", "local", "vps"}
                else "auto"
            )
        )
        self.YOUTUBE_PROXY_RELOAD_SEC = max(
            15.0, float(getenv("YOUTUBE_PROXY_RELOAD_SEC", "120") or 120)
        )
        _PROXY_RUNTIME["raw"] = _yp_raw
        _PROXY_RUNTIME["mode"] = self.YOUTUBE_PROXY_MODE
        _PROXY_RUNTIME["url"] = None  # force first resolve
        _PROXY_RUNTIME["until"] = 0.0
        # Initial resolve + env sync (validates live YouTube hop in auto mode)
        self.YOUTUBE_PROXY = get_youtube_proxy(force_refresh=True)
        self.PREFETCH_JOIN_TIMEOUT = float(getenv("PREFETCH_JOIN_TIMEOUT", 8))
        self.ACTIVEVC_SAMPLE_INTERVAL_SEC = max(
            60, int(getenv("ACTIVEVC_SAMPLE_INTERVAL_SEC", 300))
        )
        self.STREAM_ADAPTIVE: bool = getenv("STREAM_ADAPTIVE", "True").lower() == "true"

        self.DYNAMIC_QUALITY: bool = _bool_env("DYNAMIC_QUALITY", True)
        self.AUDIO_QUALITY = (
            "auto"
            if self.DYNAMIC_QUALITY
            else getenv("AUDIO_QUALITY", "medium").lower()
        )
        self.VIDEO_QUALITY = (
            "auto"
            if self.DYNAMIC_QUALITY
            else getenv("VIDEO_QUALITY", "480").lower()
        )
        self.VIDEO_STRICT_AVC: bool = getenv("VIDEO_STRICT_AVC", "True").lower() == "true"
        self.VIDEO_MAX_HEIGHT = (
            "auto" if self.DYNAMIC_QUALITY else _int_or_auto("VIDEO_MAX_HEIGHT", 480)
        )
        self.VIDEO_MAX_WIDTH = (
            "auto" if self.DYNAMIC_QUALITY else _int_or_auto("VIDEO_MAX_WIDTH", 1280)
        )
        self.VIDEO_MAX_FPS = (
            "auto" if self.DYNAMIC_QUALITY else _int_or_auto("VIDEO_MAX_FPS", 24)
        )
        self.ADAPTIVE_CPU_HIGH = float(getenv("ADAPTIVE_CPU_HIGH", 70))
        self.ADAPTIVE_CPU_RECOVER = float(getenv("ADAPTIVE_CPU_RECOVER", 55))
        self.ADAPTIVE_PING_HIGH = float(getenv("ADAPTIVE_PING_HIGH", 180))
        self.ADAPTIVE_PING_RECOVER = float(getenv("ADAPTIVE_PING_RECOVER", 140))
        self.ADAPTIVE_RAM_HIGH = float(getenv("ADAPTIVE_RAM_HIGH", 88))
        self.ADAPTIVE_RAM_RECOVER = float(getenv("ADAPTIVE_RAM_RECOVER", 78))
        self.PREFETCH_JOIN_TIMEOUT = float(getenv("PREFETCH_JOIN_TIMEOUT", 8))
        self.PRELOAD_DEPTH = max(1, min(2, _int_env("PRELOAD_DEPTH", 1, 1)))
        self.GAPLESS_PLAYBACK_ENABLED = _bool_env(
            "GAPLESS_PLAYBACK_ENABLED", True
        )
        # ntgcalls cannot guarantee overlapping inputs; retain the flag for a
        # future capability probe but keep it safely disabled by default.
        self.CROSSFADE_ENABLED = _bool_env("CROSSFADE_ENABLED", False)
        self.CROSSFADE_SECONDS = max(
            0.5, min(5.0, float(getenv("CROSSFADE_SECONDS", "2.5") or 2.5))
        )
        self.SILENCE_TRIM_ENABLED = _bool_env("SILENCE_TRIM_ENABLED", False)
        self.LOUDNESS_NORMALIZATION_ENABLED = _bool_env(
            "LOUDNESS_NORMALIZATION_ENABLED", False
        )
        self.SOURCE_FAILURE_THRESHOLD = max(
            1, _int_env("SOURCE_FAILURE_THRESHOLD", 3, 1)
        )
        self.SOURCE_COOLDOWN_SECONDS = max(
            5, _int_env("SOURCE_COOLDOWN_SECONDS", 60, 5)
        )
        self.ACTIVEVC_SAMPLE_INTERVAL_SEC = max(
            60, int(getenv("ACTIVEVC_SAMPLE_INTERVAL_SEC", 300))
        )
        self.ACTIVEVC_TIMEZONE = getenv("ACTIVEVC_TIMEZONE", "Asia/Yangon")
        self.AUTOPLAY_MAX_ARTIST_STREAK = max(
            1, int(getenv("AUTOPLAY_MAX_ARTIST_STREAK", 1))
        )
        self.AUTOPLAY_RECENT_WINDOW = max(
            5, int(getenv("AUTOPLAY_RECENT_WINDOW", 30))
        )
        self.AUTOPLAY_REQUIRED_OVERLAP_MIN = max(
            0, int(getenv("AUTOPLAY_REQUIRED_OVERLAP_MIN", 2))
        )
        self.AUTOPLAY_SAME_ARTIST_PENALTY = max(
            0.0, float(getenv("AUTOPLAY_SAME_ARTIST_PENALTY", 2.2))
        )
        self.AUTOPLAY_REPEAT_ARTIST_STREAK_PENALTY = max(
            0.0, float(getenv("AUTOPLAY_REPEAT_ARTIST_STREAK_PENALTY", 12.0))
        )
        self.AUTOPLAY_RECENT_TITLE_PENALTY = max(
            0.0, float(getenv("AUTOPLAY_RECENT_TITLE_PENALTY", 8.0))
        )
        self.AUTOPLAY_SEED_EXACT_TITLE_PENALTY = max(
            0.0, float(getenv("AUTOPLAY_SEED_EXACT_TITLE_PENALTY", 9.0))
        )
        self.PREMIUM_EMOJI_IDS: dict[str, str] = _parse_premium_emoji_ids(getenv("PREMIUM_EMOJI_IDS"))
        self.CUSTOM_EMOJI_FORCE_BOT_API = _bool_env("CUSTOM_EMOJI_FORCE_BOT_API", True)
        self.LANG_CODE = getenv("LANG_CODE", "en")

        self.COOKIES_URL = [
            url for url in getenv("COOKIES_URL", "").split(" ")
            if url and "batbin.me" in url
        ]
        self.DEFAULT_THUMB = _resolve_path(getenv("DEFAULT_THUMB", "AnonX_3/plugins/img/welcome.jpg"))
        self.PING_IMG = _resolve_path(getenv("PING_IMG", "AnonX_3/plugins/img/ping.jpg"))
        self.START_IMG = _resolve_path(getenv("START_IMG", "AnonX_3/plugins/img/welcome.jpg"))
        self.THUMB_BOT_NAME = getenv("THUMB_BOT_NAME") or "SUIIII MUSIC 🇲🇲"
        self.THUMB_TOP_TEXT = getenv(
            "THUMB_TOP_TEXT",
            "If you want to create your own music bot, please contact @khantpainghtet",
        )
        self.THUMB_CREDIT_TEXT = getenv(
            "THUMB_CREDIT_TEXT",
            "Credit by@khantpainghtet",
        )

        # CDN pipeline — fully dynamic/auto (no .env keys required).
        # Optional overrides only: CDN_ENABLED=False to disable, CDN_PUBLIC_BASE_URL, CDN_MEDIA_ROOT.
        # Defaults: ON, hybrid play (local ready first), origin auto IP+free port, 24h GC.
        self.CDN_ENABLED: bool = _bool_env("CDN_ENABLED", True)
        mode_raw = (getenv("CDN_PLAY_MODE", "auto") or "auto").strip().lower()
        if mode_raw in {"auto", "dynamic", "default", ""}:
            self.CDN_PLAY_MODE = "hybrid"
        elif mode_raw in {"hybrid", "cdn", "local"}:
            self.CDN_PLAY_MODE = mode_raw
        else:
            self.CDN_PLAY_MODE = "hybrid"
        root_raw = (getenv("CDN_MEDIA_ROOT", "auto") or "auto").strip()
        if root_raw.lower() in {"", "auto", "dynamic", "default"}:
            self.CDN_MEDIA_ROOT = "media"
        else:
            self.CDN_MEDIA_ROOT = root_raw
        self.CDN_PUBLIC_BASE_URL = (getenv("CDN_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
        prefix = (getenv("CDN_URL_PREFIX", "auto") or "auto").strip()
        if prefix.lower() in {"", "auto", "dynamic", "default"}:
            prefix = "/media"
        if not prefix.startswith("/"):
            prefix = f"/{prefix}"
        self.CDN_URL_PREFIX = prefix.rstrip("/") or "/media"
        ttl_raw = (getenv("CDN_TTL_HOURS", "auto") or "auto").strip().lower()
        self.CDN_TTL_HOURS = 24 if ttl_raw in {"", "auto", "dynamic", "default"} else max(1, _int_env("CDN_TTL_HOURS", 24, 1))
        gc_raw = (getenv("CDN_GC_INTERVAL_SEC", "auto") or "auto").strip().lower()
        self.CDN_GC_INTERVAL_SEC = 900 if gc_raw in {"", "auto", "dynamic", "default"} else max(60, _int_env("CDN_GC_INTERVAL_SEC", 900, 60))
        wait_raw = (getenv("CDN_READY_WAIT_SEC", "auto") or "auto").strip().lower()
        # auto → 15s (was 45): long waits made "Downloading..." feel stuck ~30s.
        self.CDN_READY_WAIT_SEC = (
            15.0
            if wait_raw in {"", "auto", "dynamic", "default"}
            else max(5.0, float(wait_raw or 15))
        )
        # Origin always auto when CDN on and no external domain.
        origin_raw = (getenv("CDN_ORIGIN_ENABLED", "auto") or "auto").strip().lower()
        if not self.CDN_ENABLED:
            self.CDN_ORIGIN_ENABLED = False
        elif self.CDN_PUBLIC_BASE_URL:
            # External Nginx/CF domain provided → origin off unless forced on
            self.CDN_ORIGIN_ENABLED = origin_raw in {"true", "1", "yes", "on"}
        elif origin_raw in {"false", "0", "no", "off"}:
            self.CDN_ORIGIN_ENABLED = False
        else:
            self.CDN_ORIGIN_ENABLED = True  # auto/dynamic/default/true
        host_raw = (getenv("CDN_ORIGIN_HOST", "auto") or "auto").strip()
        self.CDN_ORIGIN_HOST = (
            "0.0.0.0"
            if host_raw.lower() in {"", "auto", "dynamic", "default"}
            else host_raw
        )
        port_raw = (getenv("CDN_ORIGIN_PORT", "auto") or "auto").strip().lower()
        self.CDN_ORIGIN_PORT = 0 if port_raw in {"", "auto", "dynamic", "default"} else max(0, int(float(port_raw or 0)))
        # Runtime-filled when built-in origin starts (http://auto-ip:auto-port).
        self.CDN_ORIGIN_PUBLIC_BASE: str = ""

        # Integrated Downloader API
        # Optional sidecar: opt in explicitly so a core music-bot deployment
        # does not require or repeatedly warn about the FastAPI extra stack.
        self.DOWNLOADER_API_ENABLED: bool = _bool_env("DOWNLOADER_API_ENABLED", False)
        self.DOWNLOADER_API_HOST: str = getenv("DOWNLOADER_API_HOST", "127.0.0.1")
        self.DOWNLOADER_API_PORT: int = _int_env("DOWNLOADER_API_PORT", 8000, 1)

    def resolve_cache_timeout(self, value: float | str, ping: float | None = None) -> float:
        if isinstance(value, (int, float)):
            return max(1.0, float(value))
        if isinstance(value, str) and value.strip().lower() == "auto":
            if ping is None:
                return 8.0
            if ping <= 80:
                return 5.0
            if ping <= 150:
                return 8.0
            if ping <= 250:
                return 12.0
            return 16.0
        return 8.0

    def check(self):
        missing = [
            var
            for var in ["API_ID", "API_HASH", "BOT_TOKEN", "MONGO_URL", "LOGGER_ID", "OWNER_ID"]
            if not getattr(self, var)
        ]
        if not self.ASSISTANT_SESSIONS:
            missing.append("SESSION/SESSION<n>")
        if missing:
            raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")
