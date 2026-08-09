# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""One egress address for extraction and media fetch alike.

A signed googlevideo URL is bound to the source IP that asked for it. yt-dlp
mints the URL from whichever address the kernel picks, and ffmpeg later opens
that URL from whichever address the kernel picks *again* -- on a dual-stack VPS
those are routinely different families. Google sees the mismatch and answers
403, which is exactly the intermittent failure this module removes.

So every hop that touches YouTube resolves its bind address here:

* yt-dlp        -- ``source_address`` / ``--source-address``
* aiohttp probe -- connector ``family`` + ``local_addr``
* ffmpeg/ffprobe-- ``-local_addr``

``AUTO`` detection prefers a globally routable IPv6 address, since that is the
family Google issued the working 206 against. When the host has no such
address, everything falls back to the kernel default and the bot behaves
exactly as it did before -- this module never makes a working host worse.
"""

from __future__ import annotations

import socket
import time

from AnonX_3 import config, logger

#: Detection is a syscall against the routing table, not a network round trip,
#: but it still runs on every extraction. Cache it for a few minutes so a
#: replugged interface is picked up without re-probing per request.
_DETECT_TTL_SEC = 300.0

_cached: tuple[float, str] | None = None


def _cfg(name: str, default):
    try:
        value = getattr(config, name, default)
    except Exception:
        return default
    return default if value is None else value


def _probe_family(family: int, peer: str) -> str:
    """Ask the routing table which local address would reach ``peer``.

    UDP connect binds a source without sending a packet, the same trick
    ``core/cdn/origin.py:_local_ip`` uses.
    """
    sock = socket.socket(family, socket.SOCK_DGRAM)
    try:
        sock.connect((peer, 80))
        return str(sock.getsockname()[0] or "")
    finally:
        sock.close()


def _is_global_v6(addr: str) -> bool:
    """Reject loopback, link-local and unique-local IPv6."""
    low = (addr or "").strip().lower()
    if not low or ":" not in low:
        return False
    if low.startswith(("::1", "fe80:", "fc", "fd")):
        return False
    return True


def _detect() -> str:
    """Return the address to bind, or "" for kernel default."""
    configured = str(_cfg("YTDLP_SOURCE_ADDRESS", "") or "").strip()
    if configured and configured.lower() not in ("auto", "off", "none", "default"):
        return configured
    if configured.lower() in ("off", "none", "default"):
        return ""
    if not bool(_cfg("YTDLP_FORCE_IPV6", True)):
        return ""
    try:
        # Google's public resolver: routable, never actually contacted.
        addr = _probe_family(socket.AF_INET6, "2001:4860:4860::8888")
    except OSError as ex:
        logger.debug("netbind: no IPv6 route (%s), using kernel default", type(ex).__name__)
        return ""
    # A scope id (fe80::1%eth0) would not survive into ffmpeg's -local_addr.
    addr = addr.split("%", 1)[0]
    if not _is_global_v6(addr):
        logger.debug("netbind: IPv6 %s is not globally routable, using default", addr)
        return ""
    return addr


def source_address(*, force_refresh: bool = False) -> str:
    """The single address every YouTube hop binds to. "" means kernel default."""
    global _cached
    now = time.monotonic()
    if not force_refresh and _cached is not None and _cached[0] > now:
        return _cached[1]
    try:
        addr = _detect()
    except Exception as ex:
        logger.debug("netbind: detection failed (%s)", type(ex).__name__)
        addr = ""
    if _cached is None or _cached[1] != addr:
        logger.info(
            "netbind source_address=%s family=%s",
            addr or "kernel-default",
            "ipv6" if ":" in addr else ("ipv4" if addr else "auto"),
        )
    _cached = (now + _DETECT_TTL_SEC, addr)
    return addr


def socket_family() -> int:
    """``AF_INET6`` / ``AF_INET`` / ``AF_UNSPEC`` matching :func:`source_address`."""
    addr = source_address()
    if not addr:
        return socket.AF_UNSPEC
    return socket.AF_INET6 if ":" in addr else socket.AF_INET


def ffmpeg_local_addr_args() -> list[str]:
    """``-local_addr`` argv pair for ffmpeg/ffprobe, or [] for kernel default.

    Verified to reach the socket through https -> tls -> tcp: ffmpeg's tcp
    protocol exposes ``-local_addr`` and the https demuxer forwards unknown
    AVOptions down the protocol chain. Note ffmpeg needs a concrete address --
    a bare "::" fails getaddrinfo -- which is why detection resolves one.
    """
    addr = source_address()
    return ["-local_addr", addr] if addr else []


def aiohttp_connector():
    """A ``TCPConnector`` pinned to the same address, or None for the default."""
    addr = source_address()
    if not addr:
        return None
    try:
        import aiohttp
    except ImportError:
        return None
    try:
        return aiohttp.TCPConnector(
            family=socket_family(),
            # Port 0: pin the source address, let the kernel pick the port.
            local_addr=(addr, 0),
            ttl_dns_cache=300,
        )
    except Exception as ex:
        logger.debug("netbind: connector unavailable (%s)", type(ex).__name__)
        return None


def reset_cache() -> None:
    """Force re-detection. Used by tests and /reload paths."""
    global _cached
    _cached = None
