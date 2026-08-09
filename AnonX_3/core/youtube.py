# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import os
import gc
import re
import json
import inspect
import logging
import shutil
import subprocess
import yt_dlp
import random
import asyncio
import aiohttp
import hashlib
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from aiohttp_socks import ProxyConnector
except Exception:
    ProxyConnector = None

from py_yt import Playlist, VideosSearch

from AnonX_3 import config, db, logger
from AnonX_3.core.autoplay import StrictAutoplaySelector
from AnonX_3.core.dynamic_capacity import background_scope, dynamic_capacity
from AnonX_3.core import netbind
from AnonX_3.core.resource_manager import resource_manager
from AnonX_3.core.ytdlp_runtime import create_youtube_dl
from AnonX_3.core.resolver.error_classifier import (
    ErrorClass,
    classify_error,
    should_retry,
)
from AnonX_3.core.resolver.player_response import (
    normalize_unciphered_player_format,
    summarize_player_response,
)
from AnonX_3.core.resolver.retry import backoff_delays
from AnonX_3.core.stream_profile import resolve_video_caps
from AnonX_3.helpers import Media, Track, buttons, utils


# py_yt logs full dependency tracebacks before raising recoverable request errors.
# Keep provider failures in this module's concise fallback logs instead.
logging.getLogger("py_yt.core.requests").setLevel(logging.CRITICAL)


@dataclass(frozen=True)
class DirectStreamSource:
    url: str | None
    local_path: str
    headers: dict[str, str]
    proxy: str
    format_id: str
    ext: str
    acodec: str
    vcodec: str
    protocol: str
    abr: float | int | str
    audio_format: str
    host: str
    video: bool
    # /vplay micro lanes may return independently signed adaptive audio/video
    # URLs. ``url`` remains the audio input; raw A/V consumes ``video_url``.
    video_url: str = ""
    video_format_id: str = ""
    video_host: str = ""
    reason: str = ""
    # yt-dlp tags formats with the player client that minted the media URL.
    # Keep it for authoritative mweb/POT binding diagnostics.
    client: str = ""
    # A freshly minted authoritative source may already have passed the exact
    # 1-byte Range probe used by the outer validator. Carry that verdict only
    # for the current resolve so we do not pay for the same GVS 206 twice.
    # Cached sources are always re-probed because cache age changes validity.
    preflight_status: int = 0
    # Binding provenance is deliberately non-secret. ``visible_url`` means a
    # ``pot`` query parameter was actually observable on the selected media URL.
    # ``gvs_206_no_visible_pot`` means the configured mweb/provider route still
    # produced a fetchable URL, but we do not over-claim token binding when the
    # token itself is not present in the URL (for example a Premium/no-POT path).
    pot_bound: bool = False
    pot_provenance: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.url)


def _safe_hash_prefix(value: str | None, length: int = 8) -> str:
    """Short sha256 prefix for correlating tokens across logs.

    PO tokens and visitorData are credentials. Only ever emit a digest so two
    log lines can be compared without the raw value reaching disk.
    """
    text = str(value or "").strip()
    if not text:
        return "none"
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:length]


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(value: str | None) -> str:
    """Drop terminal colour escapes from yt-dlp text before it reaches a log."""
    return _ANSI_RE.sub("", str(value or ""))


class _YtdlpLogSink:
    """Routes yt-dlp's own output into this module's logger.

    With no ``logger`` in the params, ``YoutubeDL.trouble`` writes failures
    straight to stderr in colour, bypassing the log format: an expected
    fast-lane miss prints a raw red ``ERROR:`` block carrying no chat, video, or
    lane context. ``quiet`` does not cover it — that suppresses informational
    stdout, not errors. Everything lands at debug because each caller already
    classifies and logs the outcome with its own context.
    """

    __slots__ = ()

    # The options dict is deep-copied per resolve; the sink is stateless, so
    # share the one instance instead of allocating a clone on every play.
    def __copy__(self) -> "_YtdlpLogSink":
        return self

    def __deepcopy__(self, memo: dict) -> "_YtdlpLogSink":
        return self

    def debug(self, msg: str) -> None:
        logger.debug("ytdlp: %s", _strip_ansi(msg))

    def info(self, msg: str) -> None:
        logger.debug("ytdlp: %s", _strip_ansi(msg))

    def warning(self, msg: str) -> None:
        logger.debug("ytdlp warning: %s", _strip_ansi(msg))

    def error(self, msg: str) -> None:
        logger.debug("ytdlp error: %s", _strip_ansi(msg))


_YTDLP_LOG_SINK = _YtdlpLogSink()


def _purge_ytdlp_pot_cache() -> int:
    """Drop yt-dlp's process-global PO-token LRU. Returns entries removed.

    The builtin MemoryLRUPCP lives in module state, so a long-running bot keeps
    serving the same token for up to its 6h TTL across every extraction. Cache
    keys are sha256 of the binding dict (pot/_director.py:150), so a single
    video's entry cannot be located -- the whole jar has to go.
    """
    try:
        from yt_dlp.extractor.youtube.pot._registry import _pot_memory_cache

        store = _pot_memory_cache.value
        cache = store.get("cache")
        if not isinstance(cache, dict):
            return 0
        lock = store.get("lock")
        if lock is not None:
            with lock:
                removed = len(cache)
                cache.clear()
                return removed
        removed = len(cache)
        cache.clear()
        return removed
    except Exception as ex:
        logger.debug("pot cache purge skipped: %s", type(ex).__name__)
        return 0


class YouTubeRuntimeConfigError(RuntimeError):
    """Fail-closed runtime configuration error before creating yt-dlp."""


class YouTube:
    BROWSER_CANDIDATES = [
        ("chrome", ("google-chrome", "google-chrome-stable", "chrome")),
        ("chromium", ("chromium", "chromium-browser")),
        ("firefox", ("firefox", "firefox-esr")),
        ("edge", ("microsoft-edge", "microsoft-edge-stable", "msedge")),
        ("brave", ("brave", "brave-browser")),
        ("opera", ("opera",)),
        ("vivaldi", ("vivaldi",)),
        ("safari", ("safari",)),
    ]
    AUTH_COOKIE_NAMES = {
        "SID",
        "HSID",
        "SSID",
        "APISID",
        "SAPISID",
        "__Secure-1PSID",
        "__Secure-3PSID",
    }

    def __init__(self):
        self.base = "https://www.youtube.com/watch?v="
        self.cookies = []
        self.checked = False
        cookie_file = Path(self._configured_cookie_file())
        self.cookie_dir = str(cookie_file.parent)
        self.cookie_json_name = "cookies.json"
        self.cookie_txt_name = cookie_file.name
        self.warned = False
        self.cookie_browser = None
        self.cookie_browser_executable = None
        self.cookie_browser_checked = False
        self._cookie_refresh_lock = asyncio.Lock()
        self._cookie_refresh_task: asyncio.Task | None = None
        self._cookie_last_refresh = 0.0
        self._cookie_refresh_generation = 0
        self._cookie_last_failure_refresh = 0.0
        self._cookie_watcher = None
        self._pot_health_next_probe = 0.0
        self._pot_health_problem: str | None = None
        # One physical acquisition is shared by every request for the same
        # YouTube media type.  A later quality preference may select a
        # different playback profile, but it must not spin up a second
        # yt-dlp process while the first file is being acquired.
        self._inflight_downloads: dict[str, dict[tuple[bool, str | None], asyncio.Task]] = {}
        # A playback acquisition has one yt-dlp owner. Its first progress hook
        # publishes the already-selected direct URL so VC startup can overlap
        # the same file download instead of launching a second extract_info.
        self._download_stream_events: dict[tuple[str, bool, str | None], asyncio.Event] = {}
        self._download_stream_sources: dict[
            tuple[str, bool, str | None], tuple[str, str]
        ] = {}
        self._active_tasks: dict[int, asyncio.Task] = {}
        self._download_watchers: dict[asyncio.Task, dict[int, dict]] = {}
        self._download_progress_state: dict[asyncio.Task, dict[str, float | int]] = {}
        # Concurrency limits come from ResourceManager (env-tunable).
        # Keep attributes for any external readers; actual acquire uses RM.
        self._download_semaphore = resource_manager.download_semaphore()
        self._extract_semaphore = resource_manager.extract_semaphore()
        self._search_cache: dict[tuple[str, bool], tuple[float, Track]] = {}
        self._search_inflight: dict[tuple[str, bool], asyncio.Task] = {}
        self._search_cache_ttl: float = 1800.0  # 30 min (was 600s — faster repeat searches)
        self._search_negative_cache: dict[tuple[str, bool], float] = {}
        self._search_negative_cache_ttl: float = 5.0
        self._deep_search_cache: dict[
            tuple[str, bool, int, bool], tuple[float, list[Track]]
        ] = {}
        self._deep_search_inflight: dict[
            tuple[str, bool, int, bool], asyncio.Task
        ] = {}
        self._deep_search_cache_ttl: float = 600.0
        self._permanent_failures: dict[str, tuple[float, str]] = {}
        self._permanent_failure_ttl: float = 21600.0
        self._auth_challenge_until = 0.0
        self._auth_challenge_reason = ""
        self._auth_challenge_videos: dict[str, float] = {}
        self._auth_circuit_skip_logged: set[tuple[str, str, bool]] = set()
        self._api_failure_count = 0
        self._api_circuit_until = 0.0
        self._direct_stream_cache: dict[
            tuple[str, bool, str | None], tuple[float, str, str]
        ] = {}
        self._direct_stream_source_cache: dict[
            tuple[str, bool, str | None], tuple[float, DirectStreamSource]
        ] = {}
        # Direct URL warm-up already asks yt-dlp for the complete info dict.
        # Retain its display metadata so /play and /vplay do not show the
        # fast-path placeholder while performing a duplicate lookup.
        self._direct_metadata_cache: dict[str, tuple[float, dict]] = {}
        self._direct_stream_inflight: dict[
            tuple[str, bool, str | None], asyncio.Task
        ] = {}
        # Dedicated direct-resolve workers keep YoutubeDL request/cache state
        # warm across /play and /vplay calls.  asyncio.to_thread uses the
        # process-wide executor and constructs a fresh YoutubeDL object for
        # every track; that discards the request director, cookie jar and
        # extractor-side caches precisely on the latency-critical path.
        direct_workers = max(1, int(getattr(config, "DIRECT_RESOLVER_WORKERS", 2) or 2))
        # Sticky single-thread workers: a resolver profile fingerprint always
        # lands on the same OS thread, so its YoutubeDL instance and cookie /
        # request state survive across unrelated tracks.  A multi-worker
        # ThreadPoolExecutor + thread-local cache could report ydl_warm=0 on
        # consecutive songs simply because the scheduler chose another thread.
        self._direct_resolver_executors = [
            ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"yt-direct-{idx}")
            for idx in range(direct_workers)
        ]
        # Keep the latency-critical authoritative hedge separate from the
        # dynamic/background yt-dlp semaphore. Production showed the global
        # lane shrinking to 1 permit, which serialized slot0/slot1 and defeated
        # the race. This semaphore is process-global, bounded by sticky workers,
        # and used only by prestarted cold direct resolver lanes.
        self._direct_foreground_resolver_slots = max(
            1,
            min(
                len(self._direct_resolver_executors),
                int(getattr(config, "DIRECT_FOREGROUND_RESOLVER_SLOTS", 2) or 2),
            ),
        )
        self._direct_foreground_resolver_semaphore = asyncio.Semaphore(
            self._direct_foreground_resolver_slots
        )
        # A one-player-response micro lane races neither exact-140 promotion nor
        # the full extractor worker. Its three defaults are the union of
        # yt-dlp's official authenticated and JS-less defaults: tv_downgraded,
        # web_safari, and android_vr. Per-worker auth filtering prevents a
        # cookie-bearing runtime from calling a client that rejects cookies.
        micro_workers = max(
            direct_workers,
            min(
                3,
                len(
                    tuple(
                        getattr(
                            config,
                            "DIRECT_MICRO_PLAYER_CLIENTS",
                            ("tv_downgraded", "web_safari", "android_vr"),
                        )
                        or ("android_vr",)
                    )
                ),
            ),
        )
        self._direct_micro_executors = [
            ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"yt-micro-{idx}")
            for idx in range(micro_workers)
        ]
        # Slow exact-140 promotion must never queue ahead of a new foreground
        # /play on the same single-thread worker. Give it a physically separate
        # executor pool so an 8-10s adaptive/POT extraction cannot regress
        # time-to-audible for another chat or the next command.
        background140_workers = max(
            1, min(2, int(getattr(config, "DIRECT_BACKGROUND_140_WORKERS", 1) or 1))
        )
        self._direct_background140_executors = [
            ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"yt-140-bg-{idx}")
            for idx in range(background140_workers)
        ]
        self._direct_background140_semaphore = asyncio.Semaphore(background140_workers)
        self._direct_resolver_tls = threading.local()
        self._direct_resolver_generation = 0
        # Prewarm tasks hold the *fully resolved and validated* DirectStreamSource.
        # Foreground /play and /vplay join this exact task instead of joining only
        # the inner extraction and then repeating post-processing/diagnostics.
        self._direct_warm_tasks: dict[tuple[str, bool, str | None], asyncio.Task] = {}
        self._direct_warm_started_at: dict[tuple[str, bool, str | None], float] = {}
        # One-shot handoff cache for a prewarm that finishes before foreground
        # playback reaches resolve_direct_stream_source(). This preserves the
        # fully validated object and avoids an immediate duplicate cache probe.
        self._direct_warm_results: dict[
            tuple[str, bool, str | None], tuple[float, DirectStreamSource]
        ] = {}
        # Dual-stage audio delivery: foreground playback uses the fastest
        # validated mweb AAC/progressive source, while exact itag 140 + POT is
        # minted after playback becomes audible (and for queued-next tracks).
        # The slow exact-140 URL never blocks the initial /play critical path.
        self._direct_audio140_cache: dict[str, tuple[float, DirectStreamSource]] = {}
        self._direct_audio140_tasks: dict[str, asyncio.Task] = {}
        # Strong ownership for resolver-race losers until executor work exits.
        self._direct_race_tasks: set[asyncio.Task] = set()
        # Monotonic stamp of when each video's current direct URL was minted,
        # so a 403 can report how stale the URL it just used actually was.
        self._direct_url_minted_at: dict[str, float] = {}
        # pot hash -> first time that exact token was observed. A token reused
        # from yt-dlp's 6h LRU shows a large age here while url_age_ms is small,
        # which is the signature of a stale-token 403.
        self._pot_first_seen: dict[str, float] = {}
        self.autoplay_selector = StrictAutoplaySelector(logger)
        self._youtube_api_key = (getattr(config, "YOUTUBE_API_KEY", "") or "").strip()
        self._youtube_api_reload_sec_raw = getattr(config, "YOUTUBE_API_RELOAD_SEC", 30)
        self._api_session: aiohttp.ClientSession | None = None
        self._api_session_proxy_key = ""
        self._api_session_lock = asyncio.Lock()
        # Local VPS proxy: auto port detect + TTL re-scan (see config.get_youtube_proxy)
        self._youtube_proxy = self._live_proxy(force_refresh=True)
        self._proxy_fail_streak = 0
        self._log_proxy_state(initial=True)
        self._youtube_api_last_refresh = 0.0
        self.regex = re.compile(
            r"(https?://)?(www\.|m\.|music\.)?"
            r"(youtube\.com/(watch\?v=|shorts/|playlist\?list=)|youtu\.be/)"
            r"([A-Za-z0-9_-]{11}|PL[A-Za-z0-9_-]+)(&[?][^\s]*)?"
        )
        self.iregex = re.compile(
            r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)"
            r"(?!/(watch\?v=[A-Za-z0-9_-]{11}|shorts/[A-Za-z0-9_-]{11}"
            r"|playlist\?list=PL[A-Za-z0-9_-]+|[A-Za-z0-9_-]{11}))\S*"
        )

    @staticmethod
    def cookie_free_mode() -> bool:
        return bool(getattr(config, "COOKIE_FREE_MODE", True))

    @staticmethod
    def auth_cookie_recovery_enabled() -> bool:
        """Allow challenge-only cookie access only with an explicit profile."""
        if not getattr(config, "COOKIE_AUTH_RECOVERY_ENABLED", False):
            return False
        if not getattr(config, "AUTO_COOKIE_ENABLED", True):
            return False
        profile = (
            getattr(config, "COOKIE_BROWSER_PROFILE", "") or ""
        ).strip()
        browser = (
            getattr(config, "COOKIE_BROWSER", "") or ""
        ).strip().lower()
        supported = {name for name, _ in YouTube.BROWSER_CANDIDATES}
        return bool(profile and browser in supported)

    def _cookie_access_allowed(self, auth_recovery: bool = False) -> bool:
        return not self.cookie_free_mode() or (
            auth_recovery and self.auth_cookie_recovery_enabled()
        )

    @staticmethod
    def _split_ytdlp_csv(value: str | None) -> list[str]:
        return [
            item.strip()
            for item in re.split(r"[\s,]+", value or "")
            if item.strip()
        ]

    @staticmethod
    def _configured_cookie_file() -> str:
        return (getattr(config, "YOUTUBE_COOKIE_FILE", "") or "").strip()

    @staticmethod
    def _configured_pot_provider_url() -> str:
        return (
            getattr(config, "POT_PROVIDER_URL", "")
            or getattr(config, "PO_TOKEN_PROVIDER_URL", "")
            or ""
        ).strip()

    @staticmethod
    def _configured_ytdlp_binary() -> str:
        return (getattr(config, "YTDLP_BINARY", "") or "").strip()

    @staticmethod
    def _configured_js_runtime() -> tuple[str, str]:
        """Return the shared JS runtime source as (runtime_name, runtime_path)."""
        raw = (getattr(config, "YTDLP_JS_RUNTIME", "") or "").strip()
        if not raw:
            return "", ""
        # Config stores the CLI spelling, e.g. deno:/usr/local/bin/deno.
        # The Python API must receive the parsed structured representation.
        name, sep, path = raw.partition(":")
        return name.strip(), path.strip() if sep else ""

    def _configured_js_runtime_cli_value(self) -> str:
        name, path = self._configured_js_runtime()
        if not name:
            return ""
        return f"{name}:{path}" if path else name

    def _configured_js_runtime_api_value(self) -> dict[str, dict]:
        name, path = self._configured_js_runtime()
        if not name:
            return {}
        runtime_config: dict[str, str] = {}
        if path:
            runtime_config["path"] = path
        return {name: runtime_config}

    @staticmethod
    def _validate_youtube_cookie_file(cookie_file: str) -> bool:
        try:
            path = Path(cookie_file)
            if not path.exists() or not path.is_file():
                return False
            if path.stat().st_size <= 0:
                return False
            with path.open("rb") as fh:
                fh.read(1)
            return True
        except Exception:
            return False

    def _build_ytdlp_base_api_opts(
        self,
        *,
        action: str,
        video_id: str | None = None,
        socket_timeout: int | None = None,
        skip_download: bool | None = None,
        include_proxy: bool = True,
        validate_cookie: bool | None = None,
    ) -> tuple[dict, str, str]:
        """Shared config-to-runtime parser for YouTube yt-dlp operations.

        Playback uses the exported Netscape cookie file only. Browser profiles
        remain a cookie source/health input and are intentionally not passed to
        normal extract/download operations through browser-profile extraction.
        """
        opts: dict = {
            "quiet": True,
            "noplaylist": True,
            "geo_bypass": True,
            "no_warnings": True,
            "nocheckcertificate": True,
            "ignoreconfig": True,
            # quiet does not silence report_error: without these, yt-dlp writes
            # its own coloured ERROR block to stderr outside the log format.
            "no_color": True,
            "logger": _YTDLP_LOG_SINK,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            },
        }
        if socket_timeout is not None:
            opts["socket_timeout"] = int(socket_timeout)
        if skip_download is not None:
            opts["skip_download"] = bool(skip_download)

        # Bind extraction to the same address ffmpeg will fetch from. Google
        # signs the media URL against the requesting IP, so letting the kernel
        # pick independently on each hop is what produces the 403.
        bind_address = netbind.source_address()
        if bind_address:
            opts["source_address"] = bind_address

        cookie_file = self._configured_cookie_file()
        if validate_cookie is None:
            validate_cookie = action in {"direct", "download"}
        if validate_cookie and not self.cookie_free_mode():
            if not cookie_file or not self._validate_youtube_cookie_file(cookie_file):
                logger.error(
                    "youtube_cookie_file_invalid=True cookie_path=%s",
                    cookie_file or "missing",
                )
                raise YouTubeRuntimeConfigError("youtube cookie file invalid")
        if cookie_file and not self.cookie_free_mode():
            opts["cookiefile"] = cookie_file

        remote_components = self._split_ytdlp_csv(
            getattr(config, "YTDLP_REMOTE_COMPONENTS", "")
        )
        if remote_components:
            opts["remote_components"] = remote_components

        js_runtime_cli = self._configured_js_runtime_cli_value()
        js_runtime_api = self._configured_js_runtime_api_value()
        if js_runtime_api:
            opts["js_runtimes"] = js_runtime_api

        if include_proxy:
            proxy = self._live_proxy()
            if proxy:
                opts["proxy"] = proxy

        pot_provider_url = self._configured_pot_provider_url()
        try:
            from AnonX_3.core.provider.po_token import po_token_provider

            if po_token_provider.enabled():
                opts = po_token_provider.apply_to_ydl_opts_sync(
                    opts,
                    video_id=video_id,
                )
        except Exception as ex:
            logger.debug("po_token inject skipped (%s): %s", action, ex)

        logger.info(
            "youtube_runtime_args_ready=True youtube_path=exact_cli action=%s "
            "video_id=%s binary=%s cookies=%s js_runtime=%s pot_provider=%s "
            "source_address=%s",
            action,
            video_id or "",
            self._configured_ytdlp_binary(),
            opts.get("cookiefile") or "disabled",
            js_runtime_cli or "disabled",
            pot_provider_url or "disabled",
            opts.get("source_address") or "kernel-default",
        )
        return opts, js_runtime_cli, pot_provider_url

    def build_ytdlp_api_opts(
        self,
        *,
        action: str,
        video_id: str | None = None,
        socket_timeout: int | None = None,
        skip_download: bool | None = None,
        include_proxy: bool = True,
        validate_cookie: bool | None = None,
    ) -> dict:
        """Build Python ``yt_dlp.YoutubeDL`` options.

        ``js_runtimes`` must be a dict for the Python API:
        {"deno": {"path": "/usr/local/bin/deno"}}.
        """
        opts, _js_runtime_cli, _pot_provider_url = self._build_ytdlp_base_api_opts(
            action=action,
            video_id=video_id,
            socket_timeout=socket_timeout,
            skip_download=skip_download,
            include_proxy=include_proxy,
            validate_cookie=validate_cookie,
        )
        return opts

    def build_ytdlp_cli_args(
        self,
        *,
        action: str,
        video_id: str | None = None,
        socket_timeout: int | None = None,
        skip_download: bool | None = None,
        include_proxy: bool = True,
        validate_cookie: bool | None = None,
    ) -> list[str]:
        """Build subprocess/CLI yt-dlp arguments from the same config values."""
        opts, js_runtime_cli, _pot_provider_url = self._build_ytdlp_base_api_opts(
            action=action,
            video_id=video_id,
            socket_timeout=socket_timeout,
            skip_download=skip_download,
            include_proxy=include_proxy,
            validate_cookie=validate_cookie,
        )
        args = [self._configured_ytdlp_binary(), "--ignore-config"]
        cookie_file = opts.get("cookiefile")
        if cookie_file:
            args.extend(["--cookies", str(cookie_file)])
        if opts.get("source_address"):
            args.extend(["--source-address", str(opts["source_address"])])
        for remote_component in opts.get("remote_components") or []:
            args.extend(["--remote-components", str(remote_component)])
        if js_runtime_cli:
            args.extend(["--js-runtimes", js_runtime_cli])
        extractor_cli = self._extractor_args_to_cli(opts.get("extractor_args") or {})
        if extractor_cli:
            args.extend(["--extractor-args", extractor_cli])
        if socket_timeout is not None:
            args.extend(["--socket-timeout", str(int(socket_timeout))])
        if skip_download:
            args.append("--skip-download")
        if include_proxy and opts.get("proxy"):
            args.extend(["--proxy", str(opts["proxy"])])
        return args

    @staticmethod
    def _extractor_args_to_cli(extractor_args: dict) -> str:
        parts: list[str] = []
        for extractor, mapping in (extractor_args or {}).items():
            if not isinstance(mapping, dict):
                continue
            for key, values in mapping.items():
                if isinstance(values, (list, tuple, set)):
                    value = ",".join(str(item) for item in values)
                else:
                    value = str(values)
                parts.append(f"{extractor}:{key}={value}")
        return ";".join(parts)

    @staticmethod
    def _authenticated_default_client_opts(
        opts: dict,
        *,
        preserve_provider_client: bool = True,
    ) -> dict:
        """Let yt-dlp select clients that support the supplied account cookies."""
        authenticated = dict(opts)
        extractor_args = dict(authenticated.get("extractor_args") or {})
        youtube_args = dict(extractor_args.get("youtube") or {})
        # PO tokens are client-bound, so their provider-selected client must stay.
        provider_configured = "youtubepot-bgutilhttp" in extractor_args
        if not youtube_args.get("po_token") and not (
            provider_configured and preserve_provider_client
        ):
            youtube_args.pop("player_client", None)
        if youtube_args:
            extractor_args["youtube"] = youtube_args
        else:
            extractor_args.pop("youtube", None)
        if extractor_args:
            authenticated["extractor_args"] = extractor_args
        else:
            authenticated.pop("extractor_args", None)
        return authenticated

    def _remember_permanent_failure(self, video_id: str, reason: str = "") -> None:
        """Prevent parallel/follow-up workers from retrying a dead YouTube ID."""
        clean_id = str(video_id or "").strip()
        if not clean_id:
            return
        now = time.monotonic()
        self._permanent_failures[clean_id] = (
            now + self._permanent_failure_ttl,
            str(reason or "")[:240],
        )
        # Keep the cache bounded without adding a background maintenance task.
        if len(self._permanent_failures) > 512:
            expired = [
                key
                for key, (expires, _) in self._permanent_failures.items()
                if expires <= now
            ]
            for key in expired:
                self._permanent_failures.pop(key, None)
            while len(self._permanent_failures) > 512:
                self._permanent_failures.pop(next(iter(self._permanent_failures)))

    def is_permanently_unavailable(self, video_id: str | None) -> bool:
        clean_id = str(video_id or "").strip()
        failure = self._permanent_failures.get(clean_id)
        if not failure:
            return False
        if failure[0] <= time.monotonic():
            self._permanent_failures.pop(clean_id, None)
            return False
        return True

    def _remember_auth_challenge(
        self, reason: str = "", *, video_id: str | None = None
    ) -> None:
        """Open a per-video auth challenge circuit. Only blocks the specific video."""
        ttl = max(
            30,
            int(
                getattr(config, "YOUTUBE_AUTH_CHALLENGE_COOLDOWN_SEC", 180)
                or 180
            ),
        )
        expires = time.monotonic() + ttl
        clean_id = str(video_id or "").strip()
        if clean_id:
            self._auth_challenge_videos[clean_id] = expires
            logger.warning(
                "YouTube auth challenge circuit opened video_id=%s cooldown_sec=%s",
                clean_id,
                ttl,
            )
        else:
            self._auth_challenge_until = max(self._auth_challenge_until, expires)
            self._auth_challenge_reason = str(reason or "")[:240]
            logger.warning(
                "YouTube auth challenge GLOBAL circuit opened cooldown_sec=%s",
                ttl,
            )

    def auth_challenge_active(self) -> bool:
        """Return whether equivalent unauthenticated YouTube work must pause."""
        if self._auth_challenge_until <= time.monotonic():
            self._auth_challenge_until = 0.0
            self._auth_challenge_reason = ""
            return False
        return True

    def auth_challenge_for(self, video_id: str | None) -> bool:
        """Return whether this exact video has an active auth challenge circuit."""
        if self.auth_challenge_active():
            return True
        clean_id = str(video_id or "").strip()
        if not clean_id:
            return False
        expires = self._auth_challenge_videos.get(clean_id, 0.0)
        if expires <= time.monotonic():
            self._auth_challenge_videos.pop(clean_id, None)
            self._auth_circuit_skip_logged = {
                key
                for key in self._auth_circuit_skip_logged
                if key[1] != clean_id
            }
            return False
        return True

    def _clear_auth_challenge(self) -> None:
        self._auth_challenge_until = 0.0
        self._auth_challenge_reason = ""
        self._auth_challenge_videos.clear()
        self._auth_circuit_skip_logged.clear()

    def _detect_browser_cookie(self, *, auth_recovery: bool = False):
        if not self._cookie_access_allowed(auth_recovery):
            # A normal cookie-free lookup must not inspect the browser or poison
            # the cache needed by a later, explicitly authorized auth recovery.
            return None
        if self.cookie_browser_checked:
            return self.cookie_browser
        self.cookie_browser_checked = True
        configured = (
            getattr(config, "COOKIE_BROWSER", "auto") or "auto"
        ).strip().lower()
        if configured not in {"", "auto", "detect", "default"}:
            supported = {name for name, _ in self.BROWSER_CANDIDATES}
            if configured not in supported:
                logger.warning(
                    "Unsupported COOKIE_BROWSER=%s; supported=%s",
                    configured,
                    ",".join(sorted(supported)),
                )
                return None
            self.cookie_browser = configured
            for browser, executables in self.BROWSER_CANDIDATES:
                if browser != configured:
                    continue
                self.cookie_browser_executable = next(
                    (
                        shutil.which(executable)
                        for executable in executables
                        if shutil.which(executable)
                    ),
                    None,
                )
                break
            logger.info("Cookie agent using configured browser: %s", configured)
            return configured
        for browser, executables in self.BROWSER_CANDIDATES:
            executable_path = next(
                (
                    shutil.which(executable)
                    for executable in executables
                    if shutil.which(executable)
                ),
                None,
            )
            if executable_path:
                self.cookie_browser = browser
                self.cookie_browser_executable = executable_path
                logger.info("Cookie agent detected browser: %s", browser)
                return browser
        return None

    def _browser_cookie_spec(self, *, auth_recovery: bool = False):
        """Return one consistent yt-dlp browser/profile tuple."""
        if not self._cookie_access_allowed(auth_recovery):
            return None
        browser = self._detect_browser_cookie(auth_recovery=auth_recovery)
        if not browser:
            return None
        profile = (
            getattr(config, "COOKIE_BROWSER_PROFILE", "") or ""
        ).strip()
        return (browser, profile, None, None) if profile else (browser,)

    def _warm_browser_cookie_session(self, *, auth_recovery: bool = False) -> bool:
        """Do not use browser extraction in the bot process.

        The VPS Firefox profile is owned by the external cookie exporter/guard.
        Playback and recovery consume only config.YOUTUBE_COOKIE_FILE.
        """
        browser_spec = self._browser_cookie_spec(auth_recovery=auth_recovery)
        if not browser_spec:
            return False

        if self.cookie_browser_executable:
            try:
                subprocess.run(
                    [self.cookie_browser_executable, "--version"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        logger.debug(
            "Browser cookie warmup skipped; using exported cookie file only: %s",
            self._configured_cookie_file(),
        )
        return False

    @staticmethod
    def _strip_cookie_bom(cookie_path: str) -> None:
        try:
            with open(cookie_path, "rb") as fh:
                raw = fh.read()
            if raw.startswith(b"\xef\xbb\xbf"):
                with open(cookie_path, "wb") as fh:
                    fh.write(raw[3:])
                logger.warning("Removed UTF-8 BOM from cookie file: %s", cookie_path)
        except Exception as ex:
            logger.warning("Failed to normalize cookie file %s: %s", cookie_path, ex)

    @staticmethod
    def _decode_cookie_bytes(raw: bytes) -> str:
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                return raw.decode(encoding)
            except Exception:
                continue
        return ""

    @staticmethod
    def _looks_like_netscape_cookie(data: str) -> bool:
        text = data.strip()
        if not text:
            return False
        if text.startswith("# Netscape HTTP Cookie File"):
            return True
        for line in text.splitlines():
            row = line.strip()
            if not row or row.startswith("#"):
                continue
            if len(row.split("\t")) >= 7:
                return True
        return False

    @staticmethod
    def _json_cookie_to_netscape(data: str) -> str:
        payload = json.loads(data)
        if isinstance(payload, dict):
            payload = payload.get("cookies", [payload])
        if not isinstance(payload, list):
            raise ValueError("Unsupported cookie JSON structure")
        lines = [
            "# Netscape HTTP Cookie File",
            "# This file was generated from JSON cookies.",
            "",
        ]
        for cookie in payload:
            if not isinstance(cookie, dict):
                continue
            name = str(cookie.get("name", "")).strip()
            value = str(cookie.get("value", ""))
            domain = str(cookie.get("domain", "")).strip()
            if not name or not domain:
                continue
            flag = "FALSE" if cookie.get("hostOnly", False) else "TRUE"
            path = str(cookie.get("path", "/") or "/")
            secure = "TRUE" if cookie.get("secure", False) else "FALSE"
            exp_raw = cookie.get("expirationDate", 0) or 0
            try:
                expires = int(float(exp_raw))
            except Exception:
                expires = 0
            domain_prefix = "#HttpOnly_" if cookie.get("httpOnly", False) else ""
            lines.append(
                f"{domain_prefix}{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}"
            )
        return "\n".join(lines) + "\n"

    def _generate_cookie_txt_from_json_files(self) -> None:
        json_path = f"{self.cookie_dir}/{self.cookie_json_name}"
        txt_path = f"{self.cookie_dir}/{self.cookie_txt_name}"
        if not os.path.isfile(json_path):
            return
        try:
            # ── Cleanup: remove any extra .json files, keep only cookies.json ──
            for fname in os.listdir(self.cookie_dir):
                if fname != self.cookie_json_name and fname.endswith(".json"):
                    extra = f"{self.cookie_dir}/{fname}"
                    try:
                        os.remove(extra)
                        logger.info("Removed extra cookie json: %s", extra)
                    except Exception as ex:
                        logger.warning("Failed to remove extra json %s: %s", extra, ex)

            with open(json_path, "rb") as fh:
                raw = fh.read()
            text = self._decode_cookie_bytes(raw)
            netscape = self._json_cookie_to_netscape(text)
            cookie_count = netscape.count("\t") // 7
            with open(txt_path, "wb") as fw:
                fw.write(netscape.encode("utf-8"))
            self._strip_cookie_bom(txt_path)
            self._protect_cookie_file(txt_path)
            logger.info(
                "AUTO-CONVERT: cookies.json → cookies.txt (%s cookies) | Path: %s",
                cookie_count,
                txt_path,
            )
        except Exception as ex:
            logger.warning("Failed to convert cookie json %s: %s", json_path, ex)

    @classmethod
    def _cookie_file_health(cls, cookie_path: str) -> dict:
        health = {
            "valid": False,
            "total": 0,
            "usable": 0,
            "expiring": 0,
            "authenticated": 0,
        }
        try:
            with open(cookie_path, "rb") as fh:
                raw = fh.read()
            text = cls._decode_cookie_bytes(raw)
            if not cls._looks_like_netscape_cookie(text):
                return health
            if "youtube.com" not in text.lower():
                return health
            now = int(time.time())
            expiry_window = int(
                getattr(config, "COOKIE_EXPIRY_WINDOW_SEC", 604800) or 604800
            )
            for line in text.splitlines():
                row = line.strip()
                if not row or (
                    row.startswith("#") and not row.startswith("#HttpOnly_")
                ):
                    continue
                parts = row.split("\t")
                if len(parts) < 7:
                    continue
                domain = parts[0].replace("#HttpOnly_", "").strip()
                if "youtube.com" not in domain.lower():
                    continue
                health["total"] += 1
                try:
                    expires = int(parts[4])
                except ValueError:
                    continue
                value = parts[6].strip()
                if not value:
                    continue
                # Netscape expiry=0 means a live browser-session cookie, not an
                # already-expired cookie.
                if expires == 0 or expires > now:
                    health["usable"] += 1
                    if parts[5] in cls.AUTH_COOKIE_NAMES:
                        health["authenticated"] += 1
                    if 0 < expires <= now + expiry_window:
                        health["expiring"] += 1
            health["valid"] = health["usable"] > 0
            if not health["valid"]:
                logger.warning(
                    "All YouTube cookies expired in %s; skipping cookie file",
                    cookie_path,
                )
            return health
        except Exception as ex:
            logger.warning("Failed to read cookie file %s: %s", cookie_path, ex)
            return health

    @classmethod
    def _is_cookie_file_valid(cls, cookie_path: str) -> bool:
        return bool(cls._cookie_file_health(cookie_path)["valid"])

    @staticmethod
    def _protect_cookie_file(cookie_path: str) -> None:
        try:
            os.chmod(cookie_path, 0o600)
        except OSError as ex:
            logger.warning("Could not restrict cookie file permissions: %s", ex)

    @staticmethod
    def _batbin_raw_link(url: str) -> tuple[str | None, str | None]:
        clean = (url or "").strip()
        if not clean:
            return None, None
        parsed = urlparse(clean)
        host = (parsed.netloc or "").lower()
        if not (host == "batbin.me" or host.endswith(".batbin.me")):
            return None, None
        path = (parsed.path or "").strip("/")
        if not path:
            return None, None
        if path.startswith("raw/"):
            paste_id = path.split("/", 1)[1]
        else:
            paste_id = path.split("/", 1)[0]
        paste_id = re.sub(r"[^A-Za-z0-9_-]", "", paste_id)
        if not paste_id:
            return None, None
        return paste_id, f"https://batbin.me/raw/{paste_id}"

    def _try_export_browser_cookies(self, *, auth_recovery: bool = False) -> bool:
        """Bot does not export browser cookies - uses externally-managed static file."""
        # Static cookie file mode: config.YOUTUBE_COOKIE_FILE is managed
        # externally by youtube-cookie-guard. Playback never uses browser
        # extraction directly.
        return False

    async def refresh_local_cookies(
        self,
        *,
        force: bool = False,
        reason: str = "periodic",
        auth_recovery: bool = False,
    ) -> str | None:
        """Refresh the private local cookie file from an existing VPS profile."""
        if not self._cookie_access_allowed(auth_recovery):
            return None
        if not getattr(config, "AUTO_COOKIE_ENABLED", True):
            return None if self.cookie_free_mode() else self.get_cookies()

        requested_generation = self._cookie_refresh_generation
        async with self._cookie_refresh_lock:
            txt_path = f"{self.cookie_dir}/{self.cookie_txt_name}"
            if not os.path.isfile(txt_path):
                self._generate_cookie_txt_from_json_files()
            health = self._cookie_file_health(txt_path) if os.path.isfile(txt_path) else {}
            refreshed_by_peer = (
                force
                and self._cookie_refresh_generation != requested_generation
                and health.get("valid", False)
            )
            needs_refresh = (
                (force and not refreshed_by_peer)
                or not health.get("valid", False)
                or bool(health.get("expiring", 0))
            )
            exported = False
            if needs_refresh:
                if auth_recovery:
                    self._warm_browser_cookie_session(auth_recovery=auth_recovery)
                exported = await asyncio.to_thread(
                    self._try_export_browser_cookies,
                    auth_recovery=auth_recovery,
                )

            self.cookies.clear()
            self.checked = False
            if self.cookie_free_mode():
                recovery_health = (
                    self._cookie_file_health(txt_path)
                    if os.path.isfile(txt_path)
                    else {}
                )
                selected = (
                    txt_path
                    if recovery_health.get("valid", False)
                    and recovery_health.get("authenticated", 0) > 0
                    else None
                )
            else:
                selected = self.get_cookies()
            self._cookie_last_refresh = time.monotonic()
            self._cookie_refresh_generation += 1

            if selected:
                selected_health = self._cookie_file_health(selected)
                self._protect_cookie_file(selected)
                logger.info(
                    "Cookie agent health reason=%s refreshed=%s usable=%s "
                    "auth_markers=%s expiring=%s",
                    reason,
                    int(exported),
                    selected_health["usable"],
                    selected_health["authenticated"],
                    selected_health["expiring"],
                )
                if selected_health["authenticated"] == 0:
                    logger.warning(
                        "Cookie agent found YouTube cookies but no signed-in "
                        "authentication cookies; bot-check recovery may still fail"
                    )
            elif self._detect_browser_cookie(auth_recovery=auth_recovery):
                logger.warning(
                    "Cookie agent could not create cookies.txt; ensure the "
                    "configured VPS browser profile has a signed-in YouTube session"
                )
            return selected

    def request_cookie_refresh(
        self,
        reason: str = "bot-check",
        *,
        auth_recovery: bool = False,
    ) -> None:
        """Schedule one cooldown-limited refresh without blocking direct playback."""
        if not self._cookie_access_allowed(auth_recovery):
            return
        if not getattr(config, "AUTO_COOKIE_ENABLED", True):
            return
        now = time.monotonic()
        cooldown = max(
            30,
            int(getattr(config, "COOKIE_FAILURE_COOLDOWN_SEC", 300) or 300),
        )
        if now - self._cookie_last_failure_refresh < cooldown:
            return
        if self._cookie_refresh_task and not self._cookie_refresh_task.done():
            return

        # Trigger cookie watcher immediate sync if available
        if self._cookie_watcher and hasattr(self._cookie_watcher, 'force_sync'):
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._cookie_watcher.force_sync())
                logger.info("Cookie watcher triggered immediate sync for %s", reason)
            except Exception as ex:
                logger.debug("Cookie watcher sync trigger failed: %s", ex)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._cookie_last_failure_refresh = now
        self._cookie_refresh_task = loop.create_task(
            self.refresh_local_cookies(
                force=True,
                reason=reason,
                auth_recovery=auth_recovery,
            )
        )

        def _consume(task: asyncio.Task) -> None:
            try:
                task.result()
            except Exception as ex:
                logger.warning(
                    "Cookie agent background refresh failed: %s",
                    type(ex).__name__,
                )

        self._cookie_refresh_task.add_done_callback(_consume)

    def get_cookies(self):
        if self.cookie_free_mode():
            self.cookies.clear()
            self.checked = True
            return None
        if not self.checked:
            self.cookies.clear()
            try:
                cookie_path = self._configured_cookie_file()
                if cookie_path:
                    self._strip_cookie_bom(cookie_path)
                    if self._is_cookie_file_valid(cookie_path):
                        self._protect_cookie_file(cookie_path)
                        self.cookies.append(cookie_path)
            except OSError:
                try:
                    os.makedirs(self.cookie_dir, exist_ok=True)
                except OSError:
                    pass
            self.checked = True

        if self.cookies:
            return random.choice(self.cookies)

        if not self.warned:
            self.warned = True
            logger.warning(
                "Configured YouTube cookie file is missing or invalid: %s",
                self._configured_cookie_file(),
            )
        return None

    async def save_cookies(self, urls: list[str]) -> None:
        if self.cookie_free_mode():
            logger.info("Cookie-free mode ignored COOKIES_URL input")
            return
        logger.info("Saving cookies from urls...")
        saved = 0
        async with aiohttp.ClientSession() as session:
            for url in urls:
                name, link = self._batbin_raw_link(url)
                if not name or not link:
                    logger.warning("Skipping invalid COOKIES_URL entry: %s", url)
                    continue
                async with session.get(link) as resp:
                    resp.raise_for_status()
                    raw = await resp.read()
                if not self._looks_like_netscape_cookie(self._decode_cookie_bytes(raw)):
                    logger.warning("Downloaded cookie payload is not Netscape format: %s", link)
                    continue
                cookie_path = f"{self.cookie_dir}/{name}.txt"
                with open(cookie_path, "wb") as fw:
                    fw.write(raw)
                self._strip_cookie_bom(cookie_path)
                self._protect_cookie_file(cookie_path)
                if not self._is_cookie_file_valid(cookie_path):
                    logger.warning("Downloaded cookie file is invalid for YouTube: %s", cookie_path)
                    try:
                        os.remove(cookie_path)
                    except Exception:
                        pass
                    continue
                saved += 1
        self.cookies.clear()
        self.checked = False
        logger.info("Cookies saved in %s (%s valid file(s)).", self.cookie_dir, saved)

    def valid(self, url: str) -> bool:
        return bool(re.match(self.regex, url))

    def invalid(self, url: str) -> bool:
        return bool(re.match(self.iregex, url))

    def _refresh_api_key_if_due(self) -> str:
        reload_sec = self._resolve_api_reload_sec()
        now = time.time()
        if (now - self._youtube_api_last_refresh) < reload_sec:
            return self._youtube_api_key
        self._youtube_api_last_refresh = now
        env_key = (os.getenv("YOUTUBE_API_KEY", "") or "").strip()
        cfg_key = (getattr(config, "YOUTUBE_API_KEY", "") or "").strip()
        new_key = env_key or cfg_key
        if new_key != self._youtube_api_key:
            logger.info(
                "youtube_path=api_key_refresh changed=%s source=%s",
                bool(new_key),
                "env" if env_key else ("config" if cfg_key else "none"),
            )
            self._youtube_api_key = new_key
        return self._youtube_api_key

    def _resolve_api_reload_sec(self) -> float:
        raw = self._youtube_api_reload_sec_raw
        if isinstance(raw, str) and raw.strip().lower() == "auto":
            # Auto mode: poll more frequently when key exists, less when absent.
            return 30.0 if self._youtube_api_key else 60.0
        try:
            return max(5.0, float(raw))
        except Exception:
            return 30.0

    @staticmethod
    def _api_error_category(status: int | None, text: str | None = None) -> str:
        body = (text or "").lower()
        if status in {400, 401, 403}:
            if "quota" in body or "ratelimit" in body:
                return "quota_exceeded"
            if "key" in body or "api key" in body or "accessnotconfigured" in body:
                return "invalid_key"
            if status == 403:
                return "quota_exceeded"
            return "invalid_key"
        if status and status >= 500:
            return "network_error"
        return "unexpected"

    @staticmethod
    def _parse_playlist_id(url: str) -> str | None:
        try:
            parsed = urlparse(url)
            vals = parse_qs(parsed.query).get("list", [])
            if vals and vals[0].strip():
                return vals[0].strip()
        except Exception:
            return None
        return None

    @staticmethod
    def _iso8601_to_seconds(value: str | None) -> int:
        if not value:
            return 0
        pattern = re.compile(r"^P(?:\d+Y)?(?:\d+M)?(?:\d+D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")
        m = pattern.match(value.strip().upper())
        if not m:
            return 0
        hours = int(m.group(1) or 0)
        mins = int(m.group(2) or 0)
        secs = int(m.group(3) or 0)
        return (hours * 3600) + (mins * 60) + secs

    @staticmethod
    def _seconds_to_hms(total_sec: int) -> str:
        sec = max(0, int(total_sec))
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    @staticmethod
    def _pick_best_thumb(thumbnails: list[dict] | None) -> str:
        if not thumbnails:
            return ""
        best = thumbnails[-1] or {}
        url = str(best.get("url") or "")
        return url.split("?")[0]

    async def _log_startup_health(self) -> None:
        """Comprehensive health report for all external services at boot."""
        lines = [
            "YouTube services health:",
            "youtube_runtime_config "
            f"binary={self._configured_ytdlp_binary()} "
            f"cookies={self._configured_cookie_file()} "
            f"js_runtime={(getattr(config, 'YTDLP_JS_RUNTIME', '') or '').strip()} "
            f"pot_provider={self._configured_pot_provider_url()} "
            f"cookie_free={bool(self.cookie_free_mode())}",
        ]
        
        # 1. Cookies health
        txt_path = f"{self.cookie_dir}/{self.cookie_txt_name}"
        cookie_health = self._cookie_file_health(txt_path) if os.path.isfile(txt_path) else {}
        if cookie_health.get("valid"):
            lines.append(
                f"  cookies OK: usable={cookie_health['usable']} "
                f"auth={cookie_health['authenticated']} "
                f"expiring={cookie_health['expiring']}"
            )
        else:
            lines.append("  cookies WARN: no valid YouTube cookies found")
            # Check whether the configured Firefox/source profile exists.
            profile = (getattr(config, "COOKIE_BROWSER_PROFILE", "") or "").strip()
            if profile and os.path.isdir(profile):
                lines.append(f"    Firefox profile exists: {profile}")
            else:
                lines.append(f"    Firefox profile MISSING: {profile}")
        
        # 2. Cookie watcher status
        if self._cookie_watcher:
            running = getattr(self._cookie_watcher, '_running', False)
            gen = getattr(self._cookie_watcher, '_sync_generation', 0)
            lines.append(f"  cookie_watcher {'active' if running else 'inactive'} gen={gen}")
        elif getattr(config, "COOKIE_WATCHER_ENABLED", False):
            lines.append("  cookie_watcher configured but not started")
        else:
            lines.append("  cookie_watcher disabled")
        
        # 3. POT token provider
        try:
            from AnonX_3.core.provider.po_token import po_token_provider
            if po_token_provider.operational():
                url = self._configured_pot_provider_url()
                lines.append(f"  POT token OK: provider={url}")
                # Quick health probe
                try:
                    import aiohttp
                    async with aiohttp.ClientSession() as s:
                        async with s.get(f"{url}/ping", timeout=aiohttp.ClientTimeout(total=3)) as r:
                            if r.status == 200:
                                lines.append(f"    health probe: OK ({r.status})")
                            else:
                                lines.append(f"    health probe: HTTP {r.status}")
                except Exception:
                    lines.append("    health probe: unreachable (may still work via yt-dlp)")
            else:
                enabled = getattr(config, "PO_TOKEN_PROVIDER_ENABLED", False)
                lines.append(f"  POT token {'disabled' if not enabled else 'WARN: enabled but not operational'}")
        except Exception as ex:
            lines.append(f"  POT token WARN: {ex}")
        
        # 4. Proxy status
        proxy = self._youtube_proxy
        if proxy:
            lines.append(f"  proxy: {proxy.split('@')[-1]}")
        else:
            lines.append("  proxy: direct (no proxy)")
        
        # 5. Cookie-free mode
        if self.cookie_free_mode():
            lines.append("  mode: cookie-free")
        else:
            lines.append("  mode: authenticated")
        
        logger.info("\n".join(lines))

    def _live_proxy(self, *, force_refresh: bool = False) -> str | None:
        """Dynamic local-VPS proxy (auto port / TTL re-scan)."""
        try:
            from config import get_youtube_proxy

            url = (get_youtube_proxy(force_refresh=force_refresh) or "").strip()
        except Exception:
            url = (getattr(config, "YOUTUBE_PROXY", "") or "").strip()
        self._youtube_proxy = url or None
        try:
            config.YOUTUBE_PROXY = url
        except Exception:
            pass
        return self._youtube_proxy

    async def _periodic_service_health(self) -> None:
        """Periodic health check for external services (every 5 min). Logs warnings if degraded."""
        while True:
            await asyncio.sleep(300)  # every 5 minutes
            try:
                # Cookie health
                txt_path = f"{self.cookie_dir}/{self.cookie_txt_name}"
                if os.path.isfile(txt_path):
                    health = self._cookie_file_health(txt_path)
                    if not health.get("valid"):
                        logger.warning(
                            "HEALTH: cookies invalid — YouTube may fail. "
                            "Check cookie guard: systemctl status youtube-cookie-guard"
                        )
                    elif health.get("expiring", 0) > 0:
                        logger.info(
                            "HEALTH: cookies OK but %s expiring soon — cookie guard should refresh",
                            health["expiring"],
                        )
                    if health.get("authenticated", 0) == 0:
                        logger.warning(
                            "HEALTH: cookies have NO auth markers — "
                            "YouTube may require sign-in. Open Firefox and sign into YouTube."
                        )
                else:
                    logger.warning(
                        "HEALTH: cookies.txt missing — cookie guard may be down. "
                        "Check: systemctl status youtube-cookie-guard"
                    )
                
                # POT token health
                try:
                    from AnonX_3.core.provider.po_token import po_token_provider
                    if (
                        po_token_provider.operational()
                        and time.monotonic() >= self._pot_health_next_probe
                    ):
                        url = self._configured_pot_provider_url()
                        try:
                            import aiohttp
                            async with aiohttp.ClientSession() as s:
                                async with s.get(
                                    f"{url}/ping",
                                    timeout=aiohttp.ClientTimeout(total=3)
                                ) as r:
                                    if r.status != 200:
                                        problem = f"http-{r.status}"
                                        if self._pot_health_problem != problem:
                                            logger.warning(
                                                "HEALTH: POT token server returned %s — "
                                                "formats may be limited; probe paused",
                                                r.status,
                                            )
                                        self._pot_health_problem = problem
                                        self._pot_health_next_probe = (
                                            time.monotonic()
                                            + max(300, int(getattr(config, "POT_TOKEN_HEALTH_COOLDOWN_SEC", 1800)))
                                        )
                                    else:
                                        self._pot_health_problem = None
                                        self._pot_health_next_probe = 0.0
                        except Exception:
                            problem = "unreachable"
                            if self._pot_health_problem != problem:
                                logger.warning(
                                    "HEALTH: POT token server unreachable at %s — "
                                    "check: systemctl status bgutil-pot; probe paused",
                                    url,
                                )
                            self._pot_health_problem = problem
                            self._pot_health_next_probe = (
                                time.monotonic()
                                + max(300, int(getattr(config, "POT_TOKEN_HEALTH_COOLDOWN_SEC", 1800)))
                            )
                    elif getattr(config, "PO_TOKEN_PROVIDER_ENABLED", False):
                        logger.warning(
                            "HEALTH: POT token enabled but not operational — "
                            "bgutil plugin may be missing"
                        )
                except Exception:
                    pass
                
                # Cookie watcher health
                if self._cookie_watcher:
                    running = getattr(self._cookie_watcher, '_running', False)
                    gen = getattr(self._cookie_watcher, '_sync_generation', 0)
                    if not running:
                        logger.warning(
                            "HEALTH: cookie watcher stopped — "
                            "cookies will not auto-refresh"
                        )
                elif getattr(config, "COOKIE_WATCHER_ENABLED", False):
                    logger.warning(
                        "HEALTH: cookie watcher enabled but not running"
                    )
                
                # Proxy health — quick TCP check
                proxy = self._youtube_proxy
                if proxy:
                    try:
                        from config import _tcp_open
                        from urllib.parse import urlparse
                        p = urlparse(proxy)
                        host = p.hostname or "127.0.0.1"
                        port = int(p.port or 0)
                        if port and not _tcp_open(host, port, timeout=0.3):
                            logger.warning(
                                "HEALTH: proxy %s is not listening — "
                                "switching to direct until proxy returns",
                                proxy.split("@")[-1],
                            )
                    except Exception:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                logger.warning("HEALTH: periodic check failed: %s", ex)

    def _log_proxy_state(self, *, initial: bool = False) -> None:
        proxy = self._youtube_proxy
        mode = getattr(config, "YOUTUBE_PROXY_MODE", "?")
        if proxy:
            logger.info(
                "youtube_proxy %smode=%s url=%s",
                "boot " if initial else "",
                mode,
                proxy.split("@")[-1],
            )
        else:
            logger.info(
                "youtube_proxy %smode=%s url=none (direct)",
                "boot " if initial else "",
                mode,
            )

    def _note_proxy_failure(self) -> None:
        """After repeated network fails, force re-scan local ports and fall back to direct."""
        self._proxy_fail_streak = int(getattr(self, "_proxy_fail_streak", 0) or 0) + 1
        if self._proxy_fail_streak < 2:
            return
        if getattr(config, "YOUTUBE_PROXY_MODE", "auto") == "explicit":
            return
        prev = self._youtube_proxy
        new = self._live_proxy(force_refresh=True)
        self._proxy_fail_streak = 0
        if new != prev:
            logger.info(
                "youtube_proxy rescan prev=%s new=%s",
                (prev or "none"),
                (new or "none"),
            )
            self._log_proxy_state()
        # If rescanned proxy is same dead one or empty, force direct for next calls
        if new == prev and prev is not None:
            logger.warning(
                "youtube_proxy rescan found same dead proxy=%s — switching to direct",
                prev,
            )
            self._youtube_proxy = None
            try:
                config.YOUTUBE_PROXY = ""
            except Exception:
                pass

    def _note_proxy_success(self) -> None:
        self._proxy_fail_streak = 0

    def _is_socks_proxy(self) -> bool:
        proxy = self._live_proxy()
        if not proxy:
            return False
        return urlparse(proxy).scheme.lower() in {
            "socks4",
            "socks5",
            "socks5h",
        }

    def _aiohttp_proxy_url(self) -> str | None:
        proxy = self._live_proxy()
        if not proxy:
            return None
        parsed = urlparse(proxy)
        if parsed.scheme.lower() == "socks5h":
            return parsed._replace(scheme="socks5").geturl()
        return proxy

    async def _youtube_client_session(self) -> aiohttp.ClientSession:
        """Reuse the YouTube API connection pool across searches."""
        socks_proxy = self._aiohttp_proxy_url() if self._is_socks_proxy() else None
        proxy_key = (
            f"socks:{socks_proxy}"
            if socks_proxy and ProxyConnector is not None
            else "direct"
        )
        async with self._api_session_lock:
            session = self._api_session
            if (
                session is not None
                and not session.closed
                and self._api_session_proxy_key == proxy_key
            ):
                return session
            if session is not None and not session.closed:
                await session.close()

            timeout = aiohttp.ClientTimeout(total=10.0, connect=3.0, sock_read=7.0)
            connector = None
            if socks_proxy:
                if ProxyConnector is None:
                    logger.warning(
                        "YOUTUBE_PROXY uses SOCKS but aiohttp_socks is not installed; "
                        "YouTube API requests will bypass the proxy."
                    )
                    proxy_key = "direct"
                else:
                    connector = ProxyConnector.from_url(socks_proxy)
            self._api_session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
            )
            self._api_session_proxy_key = proxy_key
            return self._api_session

    async def close(self) -> None:
        """Close persistent API transport and direct-resolver workers."""
        for task in tuple(getattr(self, "_direct_race_tasks", ())):
            if not task.done():
                task.cancel()
        self._direct_race_tasks.clear()
        async with self._api_session_lock:
            session = self._api_session
            self._api_session = None
            self._api_session_proxy_key = ""
            if session is not None and not session.closed:
                await session.close()
        for executor in (
            *getattr(self, "_direct_resolver_executors", ()),
            *getattr(self, "_direct_micro_executors", ()),
            *getattr(self, "_direct_background140_executors", ()),
        ):
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

    def _aiohttp_proxy_kwargs(self) -> dict:
        proxy = self._live_proxy()
        if not proxy or self._is_socks_proxy():
            return {}
        return {"proxy": proxy}

    async def _api_search_videos(self, query: str, limit: int = 1) -> tuple[list[Track], str | None]:
        key = self._refresh_api_key_if_due()
        if not key:
            return [], None
        base = "https://www.googleapis.com/youtube/v3/search"
        params = {
            "part": "snippet",
            "type": "video",
            "maxResults": max(1, min(limit, 10)),
            "q": query,
            "key": key,
        }
        try:
            session = await self._youtube_client_session()
            async with session.get(
                base,
                params=params,
                **self._aiohttp_proxy_kwargs(),
            ) as resp:
                payload = await resp.json(content_type=None)
                if resp.status >= 400:
                    cat = self._api_error_category(resp.status, str(payload))
                    logger.info("youtube_path=api_fallback reason=%s endpoint=search", cat)
                    return [], cat
                items = payload.get("items") or []
        except aiohttp.ClientError:
            logger.info("youtube_path=api_fallback reason=network_error endpoint=search")
            return [], "network_error"
        except Exception as ex:
            logger.info("youtube_path=api_fallback reason=unexpected endpoint=search error=%s", ex)
            return [], "unexpected"

        tracks: list[Track] = []
        for item in items:
            vid = (
                item.get("id", {}).get("videoId")
                if isinstance(item.get("id"), dict)
                else item.get("id")
            )
            if not vid:
                continue
            snippet = item.get("snippet", {}) or {}
            thumbs = list((snippet.get("thumbnails") or {}).values())
            tracks.append(
                Track(
                    id=str(vid),
                    channel_name=snippet.get("channelTitle"),
                    duration=None,
                    duration_sec=0,
                    message_id=0,
                    title=(snippet.get("title") or "")[:80],
                    thumbnail=self._pick_best_thumb(thumbs),
                    url=f"{self.base}{vid}",
                    view_count="",
                    video=False,
                )
            )
        if tracks:
            logger.info("youtube_path=api_first endpoint=search count=%s", len(tracks))
        return tracks, None

    async def _api_playlist_items(self, playlist_id: str, limit: int = 20) -> tuple[list[Track], str | None]:
        key = self._refresh_api_key_if_due()
        if not key:
            return [], None
        base = "https://www.googleapis.com/youtube/v3/playlistItems"
        params = {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": max(1, min(limit, 50)),
            "key": key,
        }
        try:
            session = await self._youtube_client_session()
            async with session.get(
                base,
                params=params,
                **self._aiohttp_proxy_kwargs(),
            ) as resp:
                payload = await resp.json(content_type=None)
                if resp.status >= 400:
                    cat = self._api_error_category(resp.status, str(payload))
                    logger.info("youtube_path=api_fallback reason=%s endpoint=playlist", cat)
                    return [], cat
                items = payload.get("items") or []
        except aiohttp.ClientError:
            logger.info("youtube_path=api_fallback reason=network_error endpoint=playlist")
            return [], "network_error"
        except Exception as ex:
            logger.info("youtube_path=api_fallback reason=unexpected endpoint=playlist error=%s", ex)
            return [], "unexpected"

        ids = [
            str(item.get("contentDetails", {}).get("videoId") or "")
            for item in items
            if item.get("contentDetails", {}).get("videoId")
        ]
        dur_by_id: dict[str, tuple[str, int]] = {}
        if ids:
            detail_base = "https://www.googleapis.com/youtube/v3/videos"
            detail_params = {
                "part": "contentDetails",
                "id": ",".join(ids),
                "maxResults": len(ids),
                "key": key,
            }
            try:
                session = await self._youtube_client_session()
                async with session.get(
                    detail_base,
                    params=detail_params,
                    **self._aiohttp_proxy_kwargs(),
                ) as resp:
                    if resp.status < 400:
                        details = await resp.json(content_type=None)
                        for vid in details.get("items") or []:
                            vid_id = str(vid.get("id") or "")
                            seconds = self._iso8601_to_seconds(
                                vid.get("contentDetails", {}).get("duration")
                            )
                            if vid_id:
                                dur_by_id[vid_id] = (self._seconds_to_hms(seconds), seconds)
            except Exception:
                pass

        tracks: list[Track] = []
        for item in items:
            snippet = item.get("snippet", {}) or {}
            content = item.get("contentDetails", {}) or {}
            vid = str(content.get("videoId") or "")
            if not vid:
                continue
            thumbs = list((snippet.get("thumbnails") or {}).values())
            duration, duration_sec = dur_by_id.get(vid, ("", 0))
            tracks.append(
                Track(
                    id=vid,
                    channel_name=snippet.get("videoOwnerChannelTitle") or snippet.get("channelTitle", ""),
                    duration=duration,
                    duration_sec=duration_sec,
                    title=(snippet.get("title") or "")[:80],
                    thumbnail=self._pick_best_thumb(thumbs),
                    url=f"{self.base}{vid}",
                    user="",
                    view_count="",
                    video=False,
                )
            )
        if tracks:
            logger.info("youtube_path=api_first endpoint=playlist count=%s", len(tracks))
        return tracks, None

    def resolve_cached_source(
        self,
        value: str,
        message_id: int,
        *,
        video: bool = False,
    ) -> Track | None:
        """Resolve an exact local/CDN cache hit before any search/extraction.

        This covers both direct YouTube IDs and durable normalized title/query
        lookups.  It deliberately returns ``None`` on an invalid/stale record
        so the regular provider path remains the only miss path.
        """
        needle = str(value or "").strip()
        if not needle:
            return None
        direct_match = self.regex.match(needle)
        direct_id = direct_match.group(5) if direct_match else None
        if direct_id and len(direct_id) != 11:
            direct_id = None
        try:
            from AnonX_3.core.cache.hub import cache_hub

            entry = None
            local_path = None
            if direct_id:
                local_path = self._local_ready_path(str(direct_id), video=video)
                if not local_path:
                    probe = Track(
                        id=str(direct_id),
                        message_id=message_id,
                        video=video,
                        url=f"{self.base}{direct_id}",
                    )
                    entry = cache_hub.lookup_media(probe)
            else:
                entry = cache_hub.lookup_text(needle, video=video)
                if entry:
                    local_path = entry.local_path
            if entry and not local_path:
                local_path = entry.local_path
        except Exception as ex:
            logger.debug("cache-first source lookup skipped value=%r: %s", needle, ex)
            return None
        if not local_path:
            return None

        media_id = str(
            (getattr(entry, "media_id", "") if entry else "") or direct_id or ""
        )
        if len(media_id) != 11:
            return None
        metadata_track = self._track_from_direct_metadata(
            media_id, message_id, video=video
        )
        duration_sec = int(getattr(entry, "duration", 0) or 0) if entry else 0
        if metadata_track is None:
            metadata_track = Track(
                id=media_id,
                channel_name=(getattr(entry, "artist", "") if entry else "") or "YouTube",
                duration=(
                    self._seconds_to_hms(duration_sec) if duration_sec else ""
                ),
                duration_sec=duration_sec,
                message_id=message_id,
                title=(getattr(entry, "title", "") if entry else "") or needle[:80],
                thumbnail=(
                    getattr(entry, "thumbnail", "") if entry else ""
                )
                or f"https://i.ytimg.com/vi/{media_id}/hqdefault.jpg",
                url=f"{self.base}{media_id}",
                view_count="",
                video=video,
            )
        metadata_track.file_path = str(local_path)
        metadata_track.source = "cdn_local" if str(
            getattr(entry, "source", "") or ""
        ).startswith("cdn") else "youtube_local"
        metadata_track.cache_key = getattr(entry, "key", None) if entry else None
        setattr(metadata_track, "local_path", str(local_path))
        logger.info(
            "cache-first source hit media_id=%s video=%s path=%s",
            media_id,
            int(bool(video)),
            local_path,
        )
        return metadata_track

    async def resolve_source(
        self,
        *,
        query: str | None = None,
        url: str | None = None,
        m3u8: bool = False,
        message_id: int = 0,
        video: bool = False,
        user: str | None = None,
        playlist_limit: int = 0,
    ) -> tuple[Media | Track | None, list[Track], str | None]:
        tracks: list[Track] = []
        clean_url = (url or "").strip()
        clean_query = (query or "").strip()

        if m3u8 and clean_url:
            return (
                Media(
                    id=str(message_id),
                    file_path=clean_url,
                    message_id=message_id,
                    url=clean_url,
                    title="M3U8 Stream",
                    video=video,
                    user=user,
                ),
                tracks,
                None,
            )

        # Direct SoundCloud URL
        try:
            from AnonX_3.core.resolver.soundcloud import is_soundcloud_url, soundcloud

            if clean_url and is_soundcloud_url(clean_url):
                sc = await soundcloud.resolve_url(
                    clean_url, message_id=message_id, video=video
                )
                if sc:
                    sc.user = user
                    return sc, tracks, None
        except Exception as ex:
            logger.debug("soundcloud direct url resolve skipped: %s", ex)

        if clean_url and "playlist" in clean_url:
            tracks = await self.playlist(playlist_limit, user or "", clean_url, video)
            if not tracks:
                return None, [], "playlist_error"
            first = tracks.pop(0)
            first.message_id = message_id
            return first, tracks, None

        needle = clean_url or clean_query
        if not needle:
            return None, [], "play_usage"

        # Never pass Telegram file IDs to YouTube / yt-dlp extraction.
        try:
            from AnonX_3.core.telegram import Telegram as _Tg
        except Exception:
            _Tg = None
        if _Tg is not None and _Tg.is_telegram_file_id(needle):
            return None, [], "play_not_found"

        cached = self.resolve_cached_source(needle, message_id, video=video)
        if cached is not None:
            return cached, tracks, None

        file = await self.search(needle, message_id, video=video)
        if not file:
            # Keep the normal retry provider-only; a bounded yt-dlp metadata
            # fallback below is reserved for a complete provider miss.
            results = await self.deep_search(
                needle, message_id, video=video, allow_ytdlp=False
            )
            if results:
                file = results[0]
        if not file:
            # If the lightweight providers are unavailable, use yt-dlp's
            # metadata search as a last YouTube search path. This is bounded
            # and runs only after provider search misses, so the normal path
            # still reserves the selected track's acquisition for playback.
            ytdlp_deadline = 6.0 if self._live_proxy() else 5.0
            try:
                fallback_tracks = await asyncio.wait_for(
                    self._ytdlp_search_tracks(
                        needle,
                        message_id,
                        video=video,
                        limit=1,
                    ),
                    timeout=ytdlp_deadline,
                )
            except Exception:
                fallback_tracks = []
            if fallback_tracks:
                file = fallback_tracks[0]
        if not file:
            # YouTube miss → scored SoundCloud fallback (audio preferred)
            fallback_reason = "no_match"
            try:
                from AnonX_3.core.resolver.fallback import find_fallback_track

                fb, meta = await find_fallback_track(
                    query=needle,
                    message_id=message_id,
                    video=video,
                    user=user,
                )
                if fb:
                    logger.info(
                        "resolve_source fallback used query=%r score=%s",
                        needle[:80],
                        (meta or {}).get("score"),
                    )
                    return fb, tracks, None
                fallback_reason = str((meta or {}).get("reason") or "no_match")
            except Exception as ex:
                logger.warning("resolve_source fallback failed: %s", ex)
                fallback_reason = "source_unavailable"
            if fallback_reason in {"source_unavailable", "source_circuit_open"}:
                logger.warning(
                    "resolve_source provider unavailable query=%r reason=%s",
                    needle[:80],
                    fallback_reason,
                )
                return None, [], "play_source_unavailable"
            return None, [], "play_not_found"
        # Kick direct URL resolution as soon as search identifies the video.
        # The voice-join/UI path can proceed while the persistent resolver is
        # already doing the mweb/POT work. resolve_direct_stream_source joins
        # the same singleflight task later, so this never duplicates extraction.
        # Do not require an exact Track class here. Some search providers return
        # a Track-compatible object/subclass, which previously skipped prewarm
        # entirely. Any YouTube-shaped 11-char id is sufficient.
        warm_id = str(getattr(file, "id", "") or "").strip()
        if len(warm_id) == 11:
            self.warm_direct_stream_source(
                warm_id, video=bool(video), quality_tier=None
            )
        return file, tracks, None

    # Public cookie helpers for plugin-level compatibility.
    def decode_cookie_bytes(self, raw: bytes) -> str:
        return self._decode_cookie_bytes(raw)

    def json_cookie_to_netscape(self, data: str) -> str:
        return self._json_cookie_to_netscape(data)

    def strip_cookie_bom(self, cookie_path: str) -> None:
        self._strip_cookie_bom(cookie_path)

    def protect_cookie_file(self, cookie_path: str) -> None:
        self._protect_cookie_file(cookie_path)

    @staticmethod
    def _normalize_quality_tier(quality_tier: str | None) -> str | None:
        tier = (quality_tier or "").strip().lower()
        return tier if tier in {"poor", "normal", "good"} else None

    def resolve_download_quality_tier(
        self,
        quality_tier: str | None,
        *,
        video: bool,
    ) -> str | None:
        """Freeze the load-aware tier for one coordinated acquisition.

        Callers that need to prepare a stream event before ``download()`` use
        this method first.  That keeps event, in-flight, filename, and cache
        catalog keys aligned even when a busy host downgrades a requested
        video tier.
        """
        if not video:
            return None
        requested = self._normalize_quality_tier(quality_tier)
        try:
            from AnonX_3.core.resource_budget import effective_quality_tier

            resolved = effective_quality_tier(requested)
        except Exception:
            resolved = requested
        return self._normalize_quality_tier(resolved) or requested

    def _download_stream_key(
        self,
        video_id: str,
        *,
        video: bool,
        quality_tier: str | None,
    ) -> tuple[str, bool, str | None]:
        # Direct handoff and physical download ownership are media-scoped,
        # not tier-scoped.  The first owner records its resolved tier on the
        # media object; all concurrent waiters join that owner instead of
        # extracting the same video again for ``poor``/``normal``/``good``.
        return (str(video_id), bool(video), None)

    def prepare_download_stream_source(
        self,
        video_id: str,
        *,
        video: bool,
        quality_tier: str | None = None,
    ) -> None:
        """Reserve the event used by the single yt-dlp playback owner."""
        key = self._download_stream_key(
            video_id, video=video, quality_tier=quality_tier
        )
        self._download_stream_events.setdefault(key, asyncio.Event())

    def _release_download_stream_source(
        self,
        stream_key: tuple[str, bool, str | None],
    ) -> None:
        """Wake and discard request-lifetime direct-source waiters safely.

        The download owner owns this cleanup.  Early exits use the same helper
        so an unavailable/ready request cannot leave an inert event behind.
        """
        source_event = self._download_stream_events.pop(stream_key, None)
        self._download_stream_sources.pop(stream_key, None)
        if source_event is not None:
            source_event.set()

    @staticmethod
    def _playable_download_url(info: dict | None, *, video: bool) -> str | None:
        """Extract the selected progressive media URL from a yt-dlp hook."""
        if not isinstance(info, dict):
            return None
        candidates: list[dict] = [info]
        for field in ("requested_downloads", "requested_formats", "formats"):
            values = info.get(field) or []
            if isinstance(values, list):
                candidates.extend(item for item in values if isinstance(item, dict))
        for candidate in candidates:
            url = candidate.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                continue
            has_audio = candidate.get("acodec") not in (None, "none")
            has_video = candidate.get("vcodec") not in (None, "none")
            if has_audio and (not video or has_video):
                return url
        return None

    def _publish_download_stream_source(
        self,
        video_id: str,
        *,
        video: bool,
        quality_tier: str | None,
        info: dict | None,
        local_path: str,
    ) -> None:
        """Publish a direct path from the *same* yt-dlp download operation."""
        key = self._download_stream_key(
            video_id, video=video, quality_tier=quality_tier
        )
        if key in self._download_stream_sources:
            return
        remote = self._playable_download_url(info, video=video)
        if not remote:
            return
        self._download_stream_sources[key] = (remote, local_path)
        metadata = self._metadata_from_direct_info(info or {}, str(video_id))
        if metadata:
            now = time.monotonic()
            self._prune_ttl_cache(self._direct_metadata_cache, now, 1024)
            self._direct_metadata_cache[str(video_id)] = (
                now + self._search_cache_ttl,
                metadata,
            )
        self._download_stream_events.setdefault(key, asyncio.Event()).set()

    async def await_download_stream_source(
        self,
        video_id: str,
        *,
        video: bool,
        quality_tier: str | None = None,
        owner_task: asyncio.Task | None = None,
    ) -> tuple[str | None, str]:
        """Join a current acquisition until it has a direct URL or finishes.

        The method never starts yt-dlp itself.  A caller can therefore use it
        immediately after ``start_current_cache`` without risking a second
        extraction when the download owner has not reached its first hook yet.
        """
        ready = self._local_ready_path(
            video_id, video=video, quality_tier=quality_tier
        )
        local_path = ready or self.get_download_filename(
            video_id, video=video, quality_tier=quality_tier
        )
        if ready:
            return None, ready
        key = self._download_stream_key(
            video_id, video=video, quality_tier=quality_tier
        )
        source = self._download_stream_sources.get(key)
        if source:
            return source
        event = self._download_stream_events.setdefault(key, asyncio.Event())
        inflight = (self._inflight_downloads.get(str(video_id)) or {}).get(
            (bool(video), None)
        )
        waiters: set[asyncio.Future] = {asyncio.create_task(event.wait())}
        if inflight is not None and not inflight.done():
            waiters.add(asyncio.shield(inflight))
        if owner_task is not None and not owner_task.done():
            waiters.add(asyncio.shield(owner_task))
        owner_live = owner_task is not None and not owner_task.done()
        inflight_live = inflight is not None and not inflight.done()
        if len(waiters) == 1 and event.is_set():
            waiters.pop().cancel()
            return None, local_path
        if len(waiters) == 1 and not owner_live and not inflight_live:
            waiters.pop().cancel()
            return None, local_path
        try:
            wait_timeout = config.resolve_cache_timeout(
                getattr(config, "YOUTUBE_DIRECT_CACHE_TIMEOUT_SEC", 8.0)
            )
        except Exception:
            wait_timeout = 8.0
        wait_timeout = max(1.0, min(float(wait_timeout), 30.0))
        try:
            done, _pending = await asyncio.wait(
                waiters,
                timeout=wait_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                logger.info(
                    "download stream handoff timed out video_id=%s video=%s tier=%s timeout=%.1fs",
                    video_id,
                    int(bool(video)),
                    quality_tier or "audio",
                    wait_timeout,
                )
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
        source = self._download_stream_sources.get(key)
        if source:
            return source
        ready = self._local_ready_path(
            video_id, video=video, quality_tier=quality_tier
        )
        return None, ready or local_path

    def get_download_filename(
        self,
        video_id: str,
        video: bool = False,
        quality_tier: str | None = None,
    ) -> str:
        if not video:
            # Prefer already-complete audio on disk (m4a preferred, then webm/opus).
            for ext in ("m4a", "webm", "mp3", "opus", "m4b"):
                cand = f"downloads/{video_id}.{ext}"
                if self.is_complete_media_file(cand, min_bytes=64 * 1024):
                    return cand
            # Default expected name for in-flight yt-dlp (ext may still change).
            return f"downloads/{video_id}.m4a"
        quality_mode = (config.VIDEO_QUALITY or "").lower()
        tier = self._normalize_quality_tier(quality_tier)
        if quality_mode in {"auto", ""} and tier:
            return f"downloads/{video_id}.{tier}.mp4"
        if quality_mode not in {"auto", ""}:
            quality_tag = re.sub(r"[^a-z0-9_]+", "_", quality_mode).strip("_") or "manual"
            return f"downloads/{video_id}.{quality_tag}.mp4"
        return f"downloads/{video_id}.mp4"

    @staticmethod
    def is_complete_media_file(path: str | None, *, min_bytes: int = 256 * 1024) -> bool:
        """True only when a local media file is finished (not yt-dlp partial).

        Partial files cause VC join / 'Download failed' after direct-stream fallback
        (log: 9% downloaded then auto-play path=downloads/xxx.mp4).
        """
        if not path:
            return False
        p = Path(path)
        try:
            if not p.is_file():
                return False
            size = p.stat().st_size
        except OSError:
            return False
        if size < max(1024, int(min_bytes)):
            return False
        # yt-dlp / ffmpeg incomplete siblings
        for sibling in (
            Path(str(p) + ".part"),
            Path(str(p) + ".ytdl"),
            Path(str(p) + ".temp"),
            p.with_suffix(p.suffix + ".part"),
            p.with_name(p.name + ".part"),
        ):
            try:
                if sibling.exists():
                    return False
            except OSError:
                continue
        return True

    @classmethod
    async def wait_media_file_ready(
        cls,
        path: str | None,
        *,
        video: bool = False,
        timeout: float = 45.0,
    ) -> str | None:
        """Wait until download/merge settles (size stable, no .part)."""
        if not path:
            return None
        min_b = 128 * 1024 if video else 16 * 1024
        deadline = time.time() + max(5.0, float(timeout))
        last_size = -1
        stable_hits = 0
        while time.time() < deadline:
            if cls.is_complete_media_file(path, min_bytes=min_b):
                try:
                    size = Path(path).stat().st_size
                except OSError:
                    size = -1
                if size > 0 and size == last_size:
                    stable_hits += 1
                    if stable_hits >= 2:
                        return path
                else:
                    stable_hits = 0
                    last_size = size
            await asyncio.sleep(0.4)
        if cls.is_complete_media_file(path, min_bytes=min_b):
            return path
        return None

    def _direct_stream_format(
        self,
        video: bool,
        quality_tier: str | None = None,
    ) -> str:
        if not video:
            # Prefer progressive audio containers that ntgcalls/ffmpeg accept.
            # Plain audio/webm URLs often raise "No audio source found" on VC join.
            return (
                # itag 140 is AAC-LC in an M4A container and is the preferred
                # Telegram voice-chat audio source. Keep graceful fallbacks for
                # videos where 140 is not exposed by the selected mweb response.
                "140"
                "/bestaudio[ext=m4a][acodec!=none]"
                "/bestaudio[acodec^=mp4a][ext=m4a]"
                "/bestaudio[acodec=opus][ext=webm]"
                "/bestaudio[acodec!=none]"
                "/best[acodec!=none]"
            )
        caps = resolve_video_caps(quality_tier)
        max_height = int(caps["height"])
        max_width = int(caps["width"])
        max_fps = int(caps["fps"])
        # Progressive muxed streams only (single URL with audio+video).
        # Prefer AVC+AAC mp4 — ntgcalls often errors "No audio source found"
        # on some progressive URLs without usable audio track metadata.
        return (
            f"best[ext=mp4][acodec^=mp4a][vcodec*=avc1][height<=?{max_height}]"
            f"[width<=?{max_width}][fps<=?{max_fps}]"
            f"/best[ext=mp4][acodec!=none][vcodec*=avc1][height<=?{max_height}]"
            f"/best[ext=mp4][acodec!=none][vcodec!=none][height<=?{max_height}]"
            f"/best[ext=mp4][acodec!=none][vcodec!=none]"
            f"/best[acodec!=none][vcodec!=none][protocol^=http]"
        )

    def _local_ready_path(
        self,
        video_id: str,
        video: bool = False,
        quality_tier: str | None = None,
    ) -> str | None:
        """Return a complete local/CDN file if present (skip re-download / re-extract)."""
        local_path = self.get_download_filename(
            video_id, video=video, quality_tier=quality_tier
        )
        min_b = 512 * 1024 if video else 64 * 1024
        if self.is_complete_media_file(local_path, min_bytes=min_b):
            return local_path
        # A prior owner may have selected a different load-aware video tier.
        # A complete local file is still strictly better than a second yt-dlp
        # invocation, so check known tiered names and finally any safe media
        # extension for this exact YouTube ID.
        if video:
            checked = {str(local_path)}
            for tier in ("good", "normal", "poor", None):
                candidate = self.get_download_filename(
                    video_id, video=True, quality_tier=tier
                )
                if candidate in checked:
                    continue
                checked.add(candidate)
                if self.is_complete_media_file(candidate, min_bytes=min_b):
                    return candidate
            try:
                for candidate in Path("downloads").glob(f"{video_id}.*"):
                    if candidate.suffix.lower() not in {".mp4", ".webm", ".mkv"}:
                        continue
                    if self.is_complete_media_file(str(candidate), min_bytes=min_b):
                        return str(candidate)
            except Exception:
                pass
        # Generic cache artifacts are also playable local media.  They are
        # intentionally checked before CDN/yt-dlp so a restored cache can
        # start without any YouTube network work.
        cache_candidates: list[str] = []
        if video:
            tier = self._normalize_quality_tier(quality_tier)
            if tier:
                cache_candidates.append(f"cache/{video_id}.{tier}.mp4")
            cache_candidates.extend(
                [f"cache/{video_id}.mp4", f"cache/{video_id}.webm"]
            )
        else:
            cache_candidates.extend(
                [
                    f"cache/{video_id}.m4a",
                    f"cache/{video_id}.webm",
                    f"cache/{video_id}.mp3",
                    f"cache/{video_id}.opus",
                ]
            )
        for candidate in cache_candidates:
            if self.is_complete_media_file(candidate, min_bytes=min_b):
                return candidate
        # CDN ready tree
        try:
            from AnonX_3.core.cdn.manager import cdn

            if getattr(cdn, "enabled", False):
                ready = cdn._find_ready_file(video_id, video, quality_tier)
                if ready and self.is_complete_media_file(str(ready), min_bytes=min_b):
                    return str(ready)
        except Exception:
            pass
        return None

    def _record_local_cache_asset(
        self,
        media: Media | Track | None,
        local_path: str | None,
        *,
        video: bool,
        quality_tier: str | None,
    ) -> None:
        """Persist a verified local result even when CDN publishing is off.

        The local file is the durable cache source.  Registering it here makes
        text/title and direct-ID cache admission independent of whether a
        later optional CDN publisher runs or succeeds.
        """
        if media is None or not local_path:
            return
        media_id = str(getattr(media, "id", "") or "")
        min_b = 512 * 1024 if video else 64 * 1024
        if len(media_id) != 11 or not self.is_complete_media_file(
            local_path, min_bytes=min_b
        ):
            return
        try:
            from AnonX_3.core.cache.hub import cache_hub
            from AnonX_3.core.cache.keys import detect_source, normalize_lookup_text

            duration = getattr(media, "duration_sec", 0) or 0
            try:
                duration = float(duration)
            except (TypeError, ValueError):
                duration = 0.0
            if duration <= 0:
                try:
                    duration = float(utils.to_seconds(getattr(media, "duration", "")) or 0)
                except Exception:
                    duration = 0.0
            query = str(
                getattr(media, "original_query", None)
                or getattr(media, "normalized_query", None)
                or ""
            )
            title = str(getattr(media, "title", "") or "")
            key = cache_hub.key_for(
                media,
                source=detect_source(media),
                quality_tier=quality_tier,
            )
            path = Path(local_path)
            cache_hub.store().upsert_ready(
                key=key,
                media_id=media_id,
                video=bool(video),
                quality_tier=quality_tier or "",
                filename=path.name,
                ready_path=str(path),
                size_bytes=path.stat().st_size,
                source=detect_source(media),
                query=query,
                lookup_key=normalize_lookup_text(query) or normalize_lookup_text(title),
                title=title,
                artist=str(
                    getattr(media, "channel_name", None)
                    or getattr(media, "artist", None)
                    or ""
                ),
                duration=duration,
                thumbnail=str(getattr(media, "thumbnail", "") or ""),
                quality=quality_tier or "",
                local_durable=True,
            )
        except Exception as ex:
            # A cache-catalog fault must never turn a successful download into
            # a playback failure.
            logger.debug(
                "local cache catalog registration skipped media_id=%s: %s",
                media_id,
                ex,
            )

    @staticmethod
    def _direct_stream_host(url: str | None) -> str:
        if not url:
            return ""
        try:
            return urlparse(str(url)).netloc.lower()
        except Exception:
            return ""

    @staticmethod
    def _is_direct_media_url(url: str | None) -> bool:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return False
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        host = (parsed.netloc or "").lower()
        path = parsed.path or ""
        youtube_hosts = {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
            "youtu.be",
        }
        if host in youtube_hosts and "/videoplayback" not in path:
            return False
        return True

    @staticmethod
    def _direct_stream_headers(info: dict, fmt_item: dict | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        for source in (info.get("http_headers"), (fmt_item or {}).get("http_headers")):
            if not isinstance(source, dict):
                continue
            for key, value in source.items():
                if key is None or value is None:
                    continue
                clean_key = str(key).strip()
                clean_value = str(value).strip()
                if clean_key and clean_value:
                    headers[clean_key] = clean_value
        if not any(key.lower() == "user-agent" for key in headers):
            headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        if not any(key.lower() == "accept" for key in headers):
            headers["Accept"] = "*/*"
        return headers

    def _direct_source_from_info(
        self,
        *,
        info: dict,
        fmt_item: dict | None,
        local_path: str,
        video: bool,
        proxy: str | None,
    ) -> DirectStreamSource | None:
        item = fmt_item or info
        stream_url = item.get("url")
        if not self._is_direct_media_url(stream_url):
            return None
        acodec = str(item.get("acodec") or "").strip()
        vcodec = str(item.get("vcodec") or "").strip()
        if not acodec or acodec == "none":
            return None
        if video and (not vcodec or vcodec == "none"):
            return None
        ext = str(item.get("ext") or info.get("ext") or "").strip()
        protocol = str(item.get("protocol") or info.get("protocol") or "").strip()
        format_id = str(item.get("format_id") or info.get("format_id") or "").strip()
        abr = item.get("abr") or info.get("abr") or ""
        audio_format = "/".join(part for part in (ext, acodec) if part) or "unknown"
        # ``_video.py`` sets ``fmt['_client']`` on every format and
        # ``process_video_result`` merges the winning format into the top-level
        # info dict, so this survives whichever of the two the selector picked.
        client = str(item.get("_client") or info.get("_client") or "").strip()
        return DirectStreamSource(
            url=str(stream_url),
            local_path=local_path,
            headers=self._direct_stream_headers(info, fmt_item),
            proxy=str(proxy or ""),
            format_id=format_id,
            ext=ext,
            acodec=acodec,
            vcodec=vcodec,
            protocol=protocol,
            abr=abr,
            audio_format=audio_format,
            host=self._direct_stream_host(str(stream_url)),
            video=bool(video),
            video_url=str(item.get("_video_url") or info.get("_video_url") or ""),
            video_format_id=str(
                item.get("_video_format_id") or info.get("_video_format_id") or ""
            ),
            video_host=self._direct_stream_host(
                str(item.get("_video_url") or info.get("_video_url") or "")
            ),
            client=client,
        )

    def _select_direct_source_from_info(
        self,
        *,
        info: dict,
        local_path: str,
        video: bool,
        proxy: str | None,
    ) -> DirectStreamSource | None:
        """Select the direct URL deterministically from an yt-dlp info dict.

        yt-dlp may merge a progressive ``best`` format (often itag 18) into
        the top-level info dict even when the requested audio selector starts
        with itag 140.  On /play that made the fast path accept the top-level
        muxed URL before looking at ``formats``.  Prefer the exact audio-only
        140 candidate first, then other audio-only formats, and only then fall
        back to the merged/progressive item. /vplay keeps yt-dlp's selected
        top-level progressive video semantics.
        """
        if not isinstance(info, dict):
            return None
        formats = [item for item in (info.get("formats") or []) if isinstance(item, dict)]

        if not video:
            def _audio_only(item: dict) -> bool:
                acodec = str(item.get("acodec") or "").strip()
                vcodec = str(item.get("vcodec") or "").strip()
                return bool(acodec and acodec != "none" and (not vcodec or vcodec == "none"))

            buckets = (
                [item for item in formats if str(item.get("format_id") or "") == "140"],
                [
                    item for item in formats
                    if _audio_only(item)
                    and str(item.get("ext") or "").lower() == "m4a"
                    and str(item.get("acodec") or "").lower().startswith("mp4a")
                    and str(item.get("format_id") or "") != "140"
                ],
                [
                    item for item in formats
                    if _audio_only(item)
                    and str(item.get("format_id") or "") != "140"
                ],
            )
            def _abr_value(item: dict) -> float:
                try:
                    return float(item.get("abr") or 0.0)
                except (TypeError, ValueError):
                    return 0.0

            seen: set[int] = set()
            for bucket in buckets:
                # Higher ABR first within the same fallback class.
                bucket.sort(key=_abr_value, reverse=True)
                for item in bucket:
                    marker = id(item)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    source = self._direct_source_from_info(
                        info=info, fmt_item=item, local_path=local_path,
                        video=False, proxy=proxy,
                    )
                    if source is not None:
                        return source

            # If mweb did not expose a usable audio-only format, retain the
            # proven progressive fallback rather than failing playback.
            source = self._direct_source_from_info(
                info=info, fmt_item=None, local_path=local_path,
                video=False, proxy=proxy,
            )
            if source is not None:
                return source
            for item in formats:
                source = self._direct_source_from_info(
                    info=info, fmt_item=item, local_path=local_path,
                    video=False, proxy=proxy,
                )
                if source is not None:
                    return source
            return None

        source = self._direct_source_from_info(
            info=info, fmt_item=None, local_path=local_path, video=True, proxy=proxy
        )
        if source is not None:
            return source
        for item in formats:
            source = self._direct_source_from_info(
                info=info, fmt_item=item, local_path=local_path, video=True, proxy=proxy
            )
            if source is not None:
                return source
        return None

    def _empty_direct_source(
        self,
        *,
        local_path: str,
        video: bool,
        reason: str,
    ) -> DirectStreamSource:
        return DirectStreamSource(
            url=None,
            local_path=local_path,
            headers={},
            proxy="",
            format_id="",
            ext="",
            acodec="",
            vcodec="",
            protocol="",
            abr="",
            audio_format="",
            host="",
            video=bool(video),
            reason=reason,
        )

    def invalidate_direct_stream(
        self,
        video_id: str,
        *,
        reason: str = "probe_403",
    ) -> int:
        """Drop every cached direct URL for a video across all tier keys.

        resolve_direct_stream_source caches a resolved URL for 900s. A
        googlevideo 403 makes that entry permanently dead, but nothing expired
        it, so every later play replayed the same dead URL until the TTL
        lapsed -- the source of the intermittency.
        """
        dropped = 0
        for bucket in (
            self._direct_stream_source_cache,
            self._direct_stream_cache,
        ):
            for cache_key in [k for k in bucket if k[0] == video_id]:
                bucket.pop(cache_key, None)
                dropped += 1
        for cache_key in [k for k in self._direct_stream_inflight if k[0] == video_id]:
            task = self._direct_stream_inflight.pop(cache_key, None)
            if task is not None and not task.done():
                task.cancel()
        promoted = self._direct_audio140_cache.pop(video_id, None)
        if promoted is not None:
            dropped += 1
        promoted_task = self._direct_audio140_tasks.pop(video_id, None)
        if promoted_task is not None and not promoted_task.done():
            promoted_task.cancel()
        self._direct_metadata_cache.pop(video_id, None)
        self._direct_url_minted_at.pop(video_id, None)
        logger.info(
            "direct_stream invalidated video_id=%s reason=%s entries=%s",
            video_id,
            reason,
            dropped,
        )
        return dropped

    async def _probe_direct_url_status(
        self,
        source: DirectStreamSource,
        *,
        timeout: float = 4.0,
    ) -> int:
        """Fetch one byte to learn the CDN's verdict. 0 == inconclusive.

        Deliberately cheaper than ffprobe: a 1-byte Range GET reveals a 403
        without decoding, and reuses the exact headers ffmpeg will send.
        """
        if not source.url:
            return 0
        headers = dict(source.headers or {})
        headers["Range"] = "bytes=0-0"
        connector = None
        http_proxy = None
        try:
            if source.proxy and ProxyConnector is not None and "socks" in source.proxy:
                connector = ProxyConnector.from_url(source.proxy)
            elif source.proxy:
                http_proxy = source.proxy
            else:
                # No proxy: bind the same address yt-dlp minted the URL from, so
                # the probe reports the verdict ffmpeg will get instead of a 403
                # the probe itself caused by leaving via another interface.
                connector = netbind.aiohttp_connector()
        except Exception:
            connector = None
        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    source.url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    proxy=http_proxy,
                    allow_redirects=True,
                ) as resp:
                    await resp.content.read(1)
                    return int(resp.status)
        except asyncio.TimeoutError:
            return 0
        except Exception:
            return 0

    async def _probe_direct_source_status(
        self,
        source: DirectStreamSource,
        *,
        timeout: float = 4.0,
    ) -> int:
        """Prove every URL required by a direct source in one network RTT."""
        if not source.url:
            return 0
        video_url = str(getattr(source, "video_url", "") or "")
        if not video_url or video_url == source.url:
            return await self._probe_direct_url_status(source, timeout=timeout)
        video_source = replace(
            source,
            url=video_url,
            host=(
                getattr(source, "video_host", "")
                or self._direct_stream_host(video_url)
            ),
        )
        audio_status, video_status = await asyncio.gather(
            self._probe_direct_url_status(source, timeout=timeout),
            self._probe_direct_url_status(video_source, timeout=timeout),
        )
        statuses = (int(audio_status or 0), int(video_status or 0))
        if 403 in statuses:
            return 403
        if all(status in (200, 206) for status in statuses):
            return 206 if 206 in statuses else 200
        return 0

    def _log_direct_binding_diag(
        self,
        source: DirectStreamSource,
        *,
        video_id: str,
        retry_index: int,
        status: int,
        cache_hit: int,
    ) -> None:
        query = parse_qs(urlparse(source.url or "").query)
        pot = (query.get("pot") or [""])[0]
        visitor = (query.get("vd") or query.get("visitor_data") or [""])[0]
        now = time.monotonic()
        minted = self._direct_url_minted_at.get(video_id)
        url_age_ms = int(max(0.0, now - minted) * 1000) if minted else -1
        pot_age_ms = -1
        if pot:
            pot_key = _safe_hash_prefix(pot, 16)
            first_seen = self._pot_first_seen.setdefault(pot_key, now)
            pot_age_ms = int(max(0.0, now - first_seen) * 1000)
            if len(self._pot_first_seen) > 256:
                self._pot_first_seen.pop(next(iter(self._pot_first_seen)), None)
        player_client = source.client or (
            getattr(config, "PO_TOKEN_CLIENT", "mweb") or "mweb"
        ).strip()
        lane = "authoritative_mweb_pot"
        logger.info(
            "direct_binding_diag video_id=%s retry_index=%s lane=%s player_client=%s "
            "visitor_hash=%s pot_hash=%s pot_age_ms=%s url_age_ms=%s "
            "cache_hit=%s http_status=%s format_id=%s host=%s pot_bound=%s pot_provenance=%s",
            video_id,
            retry_index,
            lane,
            player_client,
            _safe_hash_prefix(visitor),
            _safe_hash_prefix(pot),
            pot_age_ms,
            url_age_ms,
            cache_hit,
            status or "n/a",
            source.format_id or "?",
            source.host or "?",
            int(bool(source.pot_bound)),
            source.pot_provenance or "none",
        )

    async def resolve_direct_stream_source(
        self,
        video_id: str,
        video: bool = False,
        quality_tier: str | None = None,
        prefer_remote: bool = False,
    ) -> DirectStreamSource:
        try:
            from AnonX_3.core.resource_budget import effective_quality_tier

            quality_tier = effective_quality_tier(quality_tier)
        except Exception:
            pass

        tier_key = self._normalize_quality_tier(quality_tier) if video else None
        key = (video_id, bool(video), tier_key)

        # If the previous/current queue window already minted exact 140 in the
        # background, promote it immediately. A fresh one-byte probe is cheap
        # (~0.1-0.4s in production) and keeps stale googlevideo URLs out.
        if not video:
            promoted = self._direct_audio140_cache.get(video_id)
            if promoted is not None:
                expiry, promoted_source = promoted
                if expiry > time.monotonic() and promoted_source.url:
                    logger.info(
                        "direct_dual_stage promoted_140_cache_hit video_id=%s",
                        video_id,
                    )
                    validated = await self._validated_direct_source(
                        promoted_source,
                        video_id=video_id,
                        video=False,
                        quality_tier=quality_tier,
                        prefer_remote=prefer_remote,
                        cache_hit=1,
                    )
                    if validated.url:
                        return replace(validated, reason="promoted_140_cache")
                    self._direct_audio140_cache.pop(video_id, None)
                else:
                    self._direct_audio140_cache.pop(video_id, None)

        # Search prewarm owns the whole resolve lifecycle, not just extract_info().
        # Joining it here prevents the foreground path from repeating validation,
        # cache publication and binding diagnostics after the same extraction.
        now = time.monotonic()
        warm_result = self._direct_warm_results.get(key)
        if warm_result is not None:
            expiry, warmed_source = warm_result
            if expiry > now and warmed_source.url:
                self._direct_warm_results.pop(key, None)
                logger.info(
                    "direct_resolver prewarm_result_reused video_id=%s video=%s fully_resolved=1",
                    video_id, int(bool(video)),
                )
                return warmed_source
            self._direct_warm_results.pop(key, None)

        # Video quality tier is load-adaptive and may change between search
        # prewarm and playback. Treat every in-flight source for the same video
        # as compatible for cold startup; the fast escape lane is progressive
        # and background cache can still upshift quality later. This restores
        # the production cross-tier singleflight contract and prevents a second
        # 2-5s resolver from starting merely because normal/good/poor changed.
        if video:
            for warm_key, (expiry, warmed_source) in list(
                self._direct_warm_results.items()
            ):
                if warm_key == key or warm_key[0] != video_id or not warm_key[1]:
                    continue
                if expiry > now and warmed_source.url:
                    self._direct_warm_results.pop(warm_key, None)
                    logger.info(
                        "direct_resolver prewarm_cross_tier_result_reused "
                        "video_id=%s requested_tier=%s warm_tier=%s "
                        "duplicate_extract_avoided=1",
                        video_id, tier_key or "none", warm_key[2] or "none",
                    )
                    return warmed_source
                if expiry <= now:
                    self._direct_warm_results.pop(warm_key, None)

        warm_task = self._direct_warm_tasks.get(key)
        warm_task_key = key
        current_task = asyncio.current_task()
        if video and (warm_task is None or warm_task is current_task):
            compatible = [
                (candidate_key, candidate)
                for candidate_key, candidate in self._direct_warm_tasks.items()
                if candidate_key != key
                and candidate_key[0] == video_id
                and candidate_key[1]
                and candidate is not current_task
            ]
            compatible.sort(key=lambda item: (item[1].done(), item[0][2] or ""))
            if compatible:
                warm_task_key, warm_task = compatible[0]
                logger.info(
                    "direct_resolver prewarm_cross_tier_join video_id=%s "
                    "requested_tier=%s warm_tier=%s duplicate_extract_avoided=1",
                    video_id, tier_key or "none", warm_task_key[2] or "none",
                )
        if warm_task is not None and warm_task is not current_task:
            if not warm_task.done():
                age_ms = int(
                    max(
                        0.0,
                        time.monotonic()
                        - self._direct_warm_started_at.get(
                            warm_task_key, time.monotonic()
                        ),
                    )
                    * 1000
                )
                logger.info(
                    "direct_resolver prewarm_joined video_id=%s video=%s age_ms=%s fully_resolved=1",
                    video_id, int(bool(video)), age_ms,
                )
                try:
                    warmed = await asyncio.shield(warm_task)
                except (asyncio.CancelledError, Exception):
                    warmed = None
                if isinstance(warmed, DirectStreamSource) and warmed.url:
                    return warmed
                logger.info(
                    "direct_resolver prewarm_join_miss video_id=%s video=%s "
                    "requested_tier=%s warm_tier=%s action=foreground_retry",
                    video_id, int(bool(video)), tier_key or "none",
                    warm_task_key[2] or "none",
                )
            else:
                try:
                    warmed = warm_task.result()
                except (asyncio.CancelledError, Exception):
                    warmed = None
                if isinstance(warmed, DirectStreamSource) and warmed.url:
                    logger.info(
                        "direct_resolver prewarm_result_reused video_id=%s video=%s fully_resolved=1",
                        video_id, int(bool(video)),
                    )
                    return warmed

        local_ready = (
            None
            if prefer_remote
            else self._local_ready_path(
                video_id, video=video, quality_tier=quality_tier
            )
        )
        local_path = local_ready or self.get_download_filename(
            video_id, video=video, quality_tier=quality_tier
        )
        if local_ready:
            logger.info(
                "direct_stream local READY skip extract video_id=%s path=%s",
                video_id,
                local_ready,
            )
            return self._empty_direct_source(
                local_path=local_ready,
                video=video,
                reason="local_ready",
            )

        cache_hit = 0
        source = None
        for (
            (vid, vflag, _tk),
            (expiry, cached_source),
        ) in list(self._direct_stream_source_cache.items()):
            if (
                vid == video_id
                and vflag == bool(video)
                and expiry > time.monotonic()
                and cached_source.url
            ):
                source = cached_source
                cache_hit = 1
                break

        if source is None:
            cached = self._direct_stream_source_cache.get(key)
            if cached and cached[0] > time.monotonic():
                source = cached[1]
                cache_hit = 1

        if source is not None:
            return await self._validated_direct_source(
                source,
                video_id=video_id,
                video=video,
                quality_tier=quality_tier,
                prefer_remote=prefer_remote,
                cache_hit=cache_hit,
            )

        task = self._direct_stream_inflight.get(key)
        task_key = key
        if video and (task is None or task.done()):
            for candidate_key, candidate in self._direct_stream_inflight.items():
                if (
                    candidate_key != key
                    and candidate_key[0] == video_id
                    and candidate_key[1]
                    and not candidate.done()
                ):
                    task_key, task = candidate_key, candidate
                    logger.info(
                        "direct_resolver inflight_cross_tier_join video_id=%s "
                        "requested_tier=%s inflight_tier=%s duplicate_extract_avoided=1",
                        video_id, tier_key or "none", candidate_key[2] or "none",
                    )
                    break
        if task is None or task.done():
            task = asyncio.create_task(
                self._resolve_direct_stream_source_uncached(
                    video_id,
                    video=video,
                    quality_tier=quality_tier,
                    allow_local_ready=not prefer_remote,
                ),
                name=f"direct-stream:{video_id}:{int(bool(video))}",
            )
            task_key = key
            self._direct_stream_inflight[key] = task
        try:
            source = await asyncio.shield(task)
        finally:
            if self._direct_stream_inflight.get(task_key) is task and task.done():
                self._direct_stream_inflight.pop(task_key, None)

        local_ready = (
            None
            if prefer_remote
            else self._local_ready_path(
                video_id, video=video, quality_tier=quality_tier
            )
        )
        out_local = local_ready or source.local_path or local_path
        if out_local != source.local_path:
            # Preserve authoritative minting-client metadata for diagnostics.
            source = replace(source, local_path=out_local)
        if source.url:
            cache_entry = (time.monotonic() + 900.0, source)
            self._direct_stream_source_cache[key] = cache_entry
            self._direct_stream_cache[key] = (
                cache_entry[0],
                source.url,
                source.local_path,
            )
            self._direct_url_minted_at[video_id] = time.monotonic()
            if tier_key is not None:
                self._direct_stream_source_cache[(video_id, bool(video), None)] = cache_entry
                self._direct_stream_cache[(video_id, bool(video), None)] = (
                    cache_entry[0],
                    source.url,
                    source.local_path,
                )
        return await self._validated_direct_source(
            source,
            video_id=video_id,
            video=video,
            quality_tier=quality_tier,
            prefer_remote=prefer_remote,
            cache_hit=0,
        )

    async def _validated_direct_source(
        self,
        source: DirectStreamSource,
        *,
        video_id: str,
        video: bool,
        quality_tier: str | None,
        prefer_remote: bool,
        cache_hit: int,
    ) -> DirectStreamSource:
        """Prove the URL is fetchable; on 403 retry once from a fresh context.

        A googlevideo 403 means the URL's PO token / visitor binding was
        rejected. Re-probing the same URL or reusing the cached token can never
        recover it, so both are dropped before the single retry.
        """
        if not source.url or not bool(
            getattr(config, "DIRECT_403_RETRY_ENABLED", True)
        ):
            return source

        fallback_local = source.local_path or self.get_download_filename(
            video_id, video=video, quality_tier=quality_tier
        )

        if cache_hit == 0 and int(getattr(source, "preflight_status", 0) or 0):
            status = int(source.preflight_status)
            logger.info(
                "direct_stream preflight_reused video_id=%s status=%s "
                "reason=fresh_authoritative_mweb_pot",
                video_id,
                status,
            )
        else:
            status = await self._probe_direct_url_status(source)
        self._log_direct_binding_diag(
            source,
            video_id=video_id,
            retry_index=0,
            status=status,
            cache_hit=cache_hit,
        )
        if status != 403:
            # 0 (inconclusive) is treated as pass: the ffmpeg open and the
            # existing startup gate remain the real proof of playability.
            return source

        self.invalidate_direct_stream(video_id, reason="probe_403")
        purged = _purge_ytdlp_pot_cache()
        logger.info(
            "direct_stream pot cache purged video_id=%s entries=%s "
            "reason=probe_403",
            video_id,
            purged,
        )

        try:
            retry_source = await self._resolve_direct_stream_source_uncached(
                video_id,
                video=video,
                quality_tier=quality_tier,
                allow_local_ready=not prefer_remote,
                # The outer validator already purged the PO-token cache.
                # Perform one fresh authoritative remint without nesting the
                # internal 403 retry again.
                allow_authoritative_retry=False,
            )
        except Exception as ex:
            logger.info(
                "direct_stream 403 retry extract failed video_id=%s err=%s",
                video_id,
                type(ex).__name__,
            )
            return self._empty_direct_source(
                local_path=fallback_local,
                video=video,
                reason="direct_403_retry_failed",
            )

        if not retry_source.url:
            return self._empty_direct_source(
                local_path=retry_source.local_path or fallback_local,
                video=video,
                reason="direct_403_retry_unresolved",
            )

        self._direct_url_minted_at[video_id] = time.monotonic()
        retry_status = await self._probe_direct_url_status(retry_source)
        self._log_direct_binding_diag(
            retry_source,
            video_id=video_id,
            retry_index=1,
            status=retry_status,
            cache_hit=0,
        )
        if retry_status == 403:
            # One retry only, as specified -- hand off to local download.
            self.invalidate_direct_stream(video_id, reason="probe_403_retry")
            return self._empty_direct_source(
                local_path=retry_source.local_path or fallback_local,
                video=video,
                reason="direct_403_after_retry",
            )

        tier_key = self._normalize_quality_tier(quality_tier) if video else None
        cache_entry = (time.monotonic() + 900.0, retry_source)
        for cache_key in {
            (video_id, bool(video), tier_key),
            (video_id, bool(video), None),
        }:
            self._direct_stream_source_cache[cache_key] = cache_entry
            self._direct_stream_cache[cache_key] = (
                cache_entry[0],
                retry_source.url,
                retry_source.local_path,
            )
        return retry_source

    async def resolve_direct_stream(
        self,
        video_id: str,
        video: bool = False,
        quality_tier: str | None = None,
        prefer_remote: bool = False,
    ) -> tuple[str | None, str]:
        source = await self.resolve_direct_stream_source(
            video_id,
            video=video,
            quality_tier=quality_tier,
            prefer_remote=prefer_remote,
        )
        return source.url, source.local_path

    async def _resolve_direct_stream_uncached(
        self,
        video_id: str,
        video: bool = False,
        quality_tier: str | None = None,
        allow_local_ready: bool = True,
    ) -> tuple[str | None, str]:
        source = await self._resolve_direct_stream_source_uncached(
            video_id,
            video=video,
            quality_tier=quality_tier,
            allow_local_ready=allow_local_ready,
        )
        return source.url, source.local_path





    def _authoritative_pot_opts(
        self,
        base_opts: dict,
        *,
        lightweight: bool = False,
        fast_progressive: bool = False,
    ) -> dict:
        """Build the provider-bound mweb + PO-token resolver profile.

        ``lightweight`` avoids the watch webpage and deliberate retries.
        ``fast_progressive`` is the foreground /play latency profile: it also
        skips the mweb config discovery request so yt-dlp can return the POT-
        exempt progressive AAC path (normally itag 18) without paying the
        8-10 second authenticated adaptive-format setup observed for itag 140.
        Exact 140 + visible POT is resolved separately after playback starts.
        """
        opts = deepcopy(base_opts)
        extractor_args = dict(opts.get("extractor_args") or {})
        youtube_args = dict(extractor_args.get("youtube") or {})
        client = (getattr(config, "PO_TOKEN_CLIENT", "mweb") or "mweb").strip() or "mweb"
        youtube_args["player_client"] = [client]
        existing_skip = list(youtube_args.get("skip") or [])
        for item in ("translated_subs", "hls", "dash"):
            if item not in existing_skip:
                existing_skip.append(item)
        youtube_args["skip"] = existing_skip

        if bool(getattr(config, "DIRECT_MWEB_USE_AD_PLAYBACK_CONTEXT", True)):
            youtube_args["use_ad_playback_context"] = ["true"]

        player_skip = list(youtube_args.get("player_skip") or [])
        if bool(getattr(config, "DIRECT_MWEB_SKIP_INITIAL_DATA", True)):
            if "initial_data" not in player_skip:
                player_skip.append("initial_data")

        if lightweight:
            # Keep the mweb client-config request when authenticated. yt-dlp uses
            # that response to recover account Data Sync ID/session information
            # needed by GVS PO-token providers. Skipping both ``webpage`` and
            # ``configs`` made adaptive audio (notably itag 140) disappear while
            # progressive itag 18 survived because yt-dlp exempts it from the
            # mweb GVS-POT requirement. We still skip the watch webpage itself.
            if "webpage" not in player_skip:
                player_skip.append("webpage")
            if fast_progressive:
                if "configs" not in player_skip:
                    player_skip.append("configs")
            elif "configs" in player_skip:
                player_skip.remove("configs")
            # The foreground resolver must never add deliberate extraction
            # sleeps. A rejected fast result is safer/faster to hand to the
            # robust profile than to sleep/retry inside the fast profile.
            opts["sleep_interval_requests"] = 0.0
            opts["extractor_retries"] = 0
            opts["retries"] = 0

        if player_skip:
            youtube_args["player_skip"] = player_skip
        extractor_args["youtube"] = youtube_args
        opts["extractor_args"] = extractor_args
        return opts

    @staticmethod
    def _direct_cookie_semantic_fingerprint(cookie: str) -> str:
        """Hash stable cookie identity, ignoring rewrite-only timestamps/expiry.

        yt-dlp may save the Netscape cookie jar after an extraction, changing
        the file mtime (and sometimes expiry/order) even when authentication
        values are unchanged. Using raw mtime in the resolver fingerprint made
        the same sticky worker report ``ydl_warm=0`` again on the next song.
        Domain/path/name/value/secure are enough to detect an actual credential
        rotation while keeping harmless jar rewrites warm.
        """
        if not cookie:
            return "none"
        try:
            rows = []
            with open(cookie, "r", encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line:
                        continue
                    # Netscape marks HttpOnly cookies as ``#HttpOnly_domain``;
                    # they are real auth rows, not comments.
                    if line.startswith("#HttpOnly_"):
                        line = line[len("#HttpOnly_"):]
                    elif line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) < 7:
                        continue
                    domain, _flag, path, secure, _expiry, name, value = parts[:7]
                    rows.append((domain, path, secure, name, value))
            if not rows:
                return "empty"
            rows.sort()
            return hashlib.sha256(repr(rows).encode("utf-8", "replace")).hexdigest()[:16]
        except OSError:
            return "missing"

    @classmethod
    def _direct_resolver_opts_fingerprint(cls, opts: dict) -> str:
        """Stable key for a persistent YoutubeDL instance.

        Logger objects and other callables are intentionally excluded. Cookie
        identity is semantic rather than mtime-based so yt-dlp's own cookie-jar
        save does not invalidate the worker between consecutive tracks.
        """
        cookie = str(opts.get("cookiefile") or "")
        # Keep the persistent object keyed by *resolver profile*, not mutable
        # cookie values. YouTube/yt-dlp can rotate session cookies after a
        # successful request; rebuilding YoutubeDL on that harmless mutation is
        # exactly what produced ydl_warm=0 on the next song. Cookie changes are
        # synchronized into the existing cookiejar in _get_persistent_direct_ydl.
        material = (
            opts.get("format"),
            opts.get("source_address"),
            opts.get("proxy"),
            cookie,
            repr(opts.get("extractor_args") or {}),
            repr(opts.get("js_runtimes") or {}),
            repr(opts.get("remote_components") or []),
            opts.get("sleep_interval_requests"),
            opts.get("extractor_retries"),
            opts.get("retries"),
        )
        return hashlib.sha256(repr(material).encode("utf-8", "replace")).hexdigest()[:20]

    def _get_persistent_direct_ydl(self, opts: dict):
        """Return the sticky profile YoutubeDL and keep its cookiejar current.

        The object identity is intentionally stable across session-cookie
        rewrites.  When cookie contents change, reload them into the existing
        jar instead of constructing a new YoutubeDL/request-director stack.
        """
        fingerprint = self._direct_resolver_opts_fingerprint(opts)
        state = getattr(self._direct_resolver_tls, "ydl_by_key", None)
        if state is None:
            state = {}
            self._direct_resolver_tls.ydl_by_key = state
        cookie_state = getattr(self._direct_resolver_tls, "cookie_identity_by_key", None)
        if cookie_state is None:
            cookie_state = {}
            self._direct_resolver_tls.cookie_identity_by_key = cookie_state

        cookie = str(opts.get("cookiefile") or "")
        cookie_identity = self._direct_cookie_semantic_fingerprint(cookie)
        ydl = state.get(fingerprint)
        warm = ydl is not None

        if ydl is not None and cookie_state.get(fingerprint) != cookie_identity:
            try:
                jar = getattr(ydl, "cookiejar", None)
                load = getattr(jar, "load", None)
                if not callable(load):
                    raise RuntimeError("cookiejar_load_unavailable")
                load(ignore_discard=True, ignore_expires=True)
                cookie_state[fingerprint] = cookie_identity
                logger.debug(
                    "direct_ydl_cookie_reload profile=%s cookie_identity=%s",
                    fingerprint[:8], cookie_identity[:8],
                )
            except Exception as ex:
                # An actual incompatible cookie-jar change gets a fresh runtime,
                # but a normal yt-dlp jar save no longer changes the profile key.
                logger.info(
                    "direct_ydl_rebuild reason=cookie_reload_failed profile=%s err=%s",
                    fingerprint[:8], type(ex).__name__,
                )
                try:
                    ydl.__exit__(None, None, None)
                except Exception:
                    pass
                state.pop(fingerprint, None)
                cookie_state.pop(fingerprint, None)
                ydl = None
                warm = False

        if ydl is None:
            # Audio micro/fast/140/robust plus one normal video pair fit inside
            # this bound. Profile identity stays stable while cookiejar contents
            # can be refreshed in place.
            if len(state) >= 6:
                old_key = next(iter(state))
                old_ydl = state.pop(old_key)
                cookie_state.pop(old_key, None)
                try:
                    old_ydl.__exit__(None, None, None)
                except Exception:
                    pass
            ydl = create_youtube_dl(opts, yt_dlp.YoutubeDL)
            try:
                ydl.__enter__()
            except Exception:
                pass
            state[fingerprint] = ydl
            cookie_state[fingerprint] = cookie_identity
        return ydl, warm

    def _persistent_direct_prepare(self, opts: dict):
        """Construct the sticky YoutubeDL runtime without a network extraction."""
        started = time.monotonic()
        _ydl, warm = self._get_persistent_direct_ydl(opts)
        return warm, int((time.monotonic() - started) * 1000)

    def _persistent_direct_extract(self, url: str, opts: dict):
        """Run extraction inside a warm per-worker YoutubeDL instance."""
        ydl, warm = self._get_persistent_direct_ydl(opts)
        started = time.monotonic()
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as ex:
            raise RuntimeError(str(ex)) from ex
        return info, warm, int((time.monotonic() - started) * 1000)

    @staticmethod
    def _direct_progressive_info(
        player_response: dict,
        video_id: str,
        opts: dict,
        *,
        client: str,
        video: bool = False,
        quality_tier: str | None = None,
    ) -> dict | None:
        """Normalize a direct player response without invoking player JS.

        Only HTTP(S) URLs that need no player-JS signature solving are eligible
        for the micro lane. Audio prefers muxed itag18, then adaptive audio.
        Video accepts only a muxed progressive A/V source here; adaptive A/V
        pairing is handled by ``_direct_adaptive_av_info`` so both signed URLs
        can be proved together.
        """
        if not isinstance(player_response, dict):
            return None
        streaming = player_response.get("streamingData") or {}

        def _normalized(item: object) -> dict | None:
            normalized, _recovered = normalize_unciphered_player_format(item)
            return normalized

        def _codec_pair(item: dict) -> tuple[str, str]:
            mime = str(item.get("mimeType") or "")
            match = re.search(r'codecs="([^"]+)"', mime)
            codecs = [
                part.strip()
                for part in (match.group(1).split(",") if match else [])
            ]
            if mime.lower().startswith("audio/"):
                return (codecs[-1] if codecs else "mp4a.40.2"), "none"
            return (
                codecs[-1] if len(codecs) > 1 else "none",
                codecs[0] if codecs else "avc1.42001E",
            )

        progressive = []
        for raw_item in streaming.get("formats") or []:
            if item := _normalized(raw_item):
                progressive.append(item)
        fmt = None
        if not video:
            fmt = next(
                (
                    item
                    for item in progressive
                    if str(item.get("itag") or item.get("format_id") or "") == "18"
                ),
                None,
            )
            if fmt is None:
                adaptive = []
                for raw_item in streaming.get("adaptiveFormats") or []:
                    item = _normalized(raw_item)
                    if item and str(item.get("mimeType") or "").lower().startswith(
                        "audio/"
                    ):
                        adaptive.append(item)

                def _audio_score(item: dict):
                    fid = str(item.get("itag") or item.get("format_id") or "")
                    mime = str(item.get("mimeType") or "").lower()
                    preferred = 3 if fid == "140" else 2 if "mp4a" in mime else 1
                    try:
                        bitrate = float(item.get("bitrate") or 0.0)
                    except (TypeError, ValueError):
                        bitrate = 0.0
                    return preferred, bitrate

                fmt = max(adaptive, key=_audio_score, default=None)
        else:
            caps = resolve_video_caps(quality_tier)
            max_height = int(caps["height"])
            max_width = int(caps["width"])
            max_fps = int(caps["fps"])
            candidates = []
            for item in progressive:
                acodec, vcodec = _codec_pair(item)
                if acodec in {"", "none"} or vcodec in {"", "none"}:
                    continue
                try:
                    height = int(item.get("height") or 0)
                    width = int(item.get("width") or 0)
                    fps = int(item.get("fps") or 0)
                except (TypeError, ValueError):
                    continue
                if height and height > max_height:
                    continue
                if width and width > max_width:
                    continue
                if fps and fps > max_fps:
                    continue
                fid = str(item.get("itag") or item.get("format_id") or "")
                try:
                    bitrate = float(item.get("bitrate") or 0.0)
                except (TypeError, ValueError):
                    bitrate = 0.0
                candidates.append((item, fid, height, bitrate))
            if candidates:
                target_height = min(max_height, 360)
                candidates.sort(
                    key=lambda row: (
                        0 if row[1] == "18" else 1,
                        abs((row[2] or target_height) - target_height),
                        -(row[3] or 0.0),
                    )
                )
                fmt = candidates[0][0]

        if fmt is None:
            return None
        acodec, vcodec = _codec_pair(fmt)
        if video and (acodec in {"", "none"} or vcodec in {"", "none"}):
            return None
        if not video and acodec in {"", "none"}:
            return None

        mime = str(fmt.get("mimeType") or "")
        details = player_response.get("videoDetails") or {}
        micro = player_response.get("microformat") or {}
        micro_renderer = micro.get("playerMicroformatRenderer") or {}
        try:
            duration = int(float(details.get("lengthSeconds") or 0))
        except (TypeError, ValueError):
            duration = 0
        bitrate = fmt.get("bitrate") or 0
        try:
            abr = round(float(bitrate) / 1000.0, 1) if bitrate else ""
        except (TypeError, ValueError):
            abr = ""
        thumbs = (details.get("thumbnail") or {}).get("thumbnails") or []
        thumbnail = str((thumbs[-1] if thumbs else {}).get("url") or "")
        format_id = str(fmt.get("itag") or fmt.get("format_id") or "")
        ext = "webm" if "webm" in mime.lower() else "mp4"
        return {
            "id": video_id,
            "title": str(details.get("title") or ""),
            "duration": duration,
            "channel": str(details.get("author") or "YouTube"),
            "view_count": details.get("viewCount"),
            "thumbnail": thumbnail,
            "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
            "url": str(fmt["url"]),
            "format_id": format_id,
            "ext": ext,
            "acodec": acodec,
            "vcodec": vcodec,
            "protocol": str(urlparse(str(fmt["url"])).scheme or "https"),
            "abr": abr,
            "height": fmt.get("height"),
            "width": fmt.get("width"),
            "fps": fmt.get("fps"),
            "_client": client,
            "_micro_cipher_recovered": bool(
                fmt.get("_micro_cipher_recovered")
            ),
            "http_headers": dict(opts.get("http_headers") or {}),
            "availability": micro_renderer.get("isUnlisted") is not True,
        }

    @staticmethod
    def _direct_adaptive_av_info(
        player_response: dict,
        video_id: str,
        opts: dict,
        *,
        client: str,
        quality_tier: str | None = None,
    ) -> dict | None:
        """Pair plain-URL AAC/MP4 + AVC/MP4 for raw /vplay startup."""
        if not isinstance(player_response, dict):
            return None
        streaming = player_response.get("streamingData") or {}
        adaptive = []
        for raw_item in streaming.get("adaptiveFormats") or []:
            item, _recovered = normalize_unciphered_player_format(raw_item)
            if item is not None:
                adaptive.append(item)
        if not adaptive:
            return None

        def _codecs(item: dict) -> tuple[str, str]:
            mime = str(item.get("mimeType") or "")
            match = re.search(r'codecs="([^"]+)"', mime)
            codecs = [
                part.strip()
                for part in (match.group(1).split(",") if match else [])
            ]
            if mime.lower().startswith("audio/"):
                return (codecs[-1] if codecs else "none"), "none"
            return "none", (codecs[0] if codecs else "none")

        caps = resolve_video_caps(quality_tier)
        max_height = int(caps["height"])
        max_width = int(caps["width"])
        max_fps = int(caps["fps"])
        audio_candidates = []
        video_candidates = []
        for item in adaptive:
            mime = str(item.get("mimeType") or "").lower()
            acodec, vcodec = _codecs(item)
            fid = str(item.get("itag") or item.get("format_id") or "")
            try:
                bitrate = float(item.get("bitrate") or 0.0)
            except (TypeError, ValueError):
                bitrate = 0.0
            if mime.startswith("audio/mp4") and acodec not in {"", "none"}:
                audio_candidates.append((item, fid, acodec, bitrate))
                continue
            if not mime.startswith("video/mp4") or vcodec in {"", "none"}:
                continue
            try:
                height = int(item.get("height") or 0)
                width = int(item.get("width") or 0)
                fps = int(item.get("fps") or 0)
            except (TypeError, ValueError):
                continue
            if height and height > max_height:
                continue
            if width and width > max_width:
                continue
            if fps and fps > max_fps:
                continue
            video_candidates.append((item, fid, vcodec, height, width, fps, bitrate))

        if not audio_candidates or not video_candidates:
            return None
        audio = next((item for item in audio_candidates if item[1] == "140"), None)
        if audio is None:
            audio_candidates.sort(key=lambda item: item[3], reverse=True)
            audio = audio_candidates[0]
        avc = [item for item in video_candidates if item[2].lower().startswith("avc1")]
        candidates = avc or video_candidates
        target_height = min(max_height, 360)
        candidates.sort(
            key=lambda item: (
                abs((item[3] or target_height) - target_height),
                0 if (item[3] or 0) <= target_height else 1,
                -(item[6] or 0.0),
            )
        )
        video_item = candidates[0]
        audio_raw, audio_id, acodec, audio_bitrate = audio
        video_raw, video_id_fmt, vcodec, height, width, fps, _ = video_item
        details = player_response.get("videoDetails") or {}
        micro = player_response.get("microformat") or {}
        micro_renderer = micro.get("playerMicroformatRenderer") or {}
        try:
            duration = int(float(details.get("lengthSeconds") or 0))
        except (TypeError, ValueError):
            duration = 0
        thumbs = (details.get("thumbnail") or {}).get("thumbnails") or []
        thumbnail = str((thumbs[-1] if thumbs else {}).get("url") or "")
        abr = round(audio_bitrate / 1000.0, 1) if audio_bitrate else ""
        return {
            "id": video_id,
            "title": str(details.get("title") or ""),
            "duration": duration,
            "channel": str(details.get("author") or "YouTube"),
            "view_count": details.get("viewCount"),
            "thumbnail": thumbnail,
            "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
            "url": str(audio_raw["url"]),
            "_video_url": str(video_raw["url"]),
            "format_id": f"{audio_id}+{video_id_fmt}",
            "_video_format_id": video_id_fmt,
            "ext": "mp4",
            "acodec": acodec,
            "vcodec": vcodec,
            "protocol": str(urlparse(str(audio_raw["url"])).scheme or "https"),
            "abr": abr,
            "height": height or None,
            "width": width or None,
            "fps": fps or None,
            "_client": client,
            "_micro_cipher_recovered": bool(
                audio_raw.get("_micro_cipher_recovered")
                or video_raw.get("_micro_cipher_recovered")
            ),
            "http_headers": dict(opts.get("http_headers") or {}),
            "availability": micro_renderer.get("isUnlisted") is not True,
        }

    @staticmethod
    def _direct_player18_info(
        player_response: dict, video_id: str, opts: dict
    ) -> dict | None:
        """Backward-compatible single-client audio normalizer."""
        return YouTube._direct_progressive_info(
            player_response,
            video_id,
            opts,
            client="mweb",
            video=False,
            quality_tier=None,
        )

    def _persistent_direct_player_candidate(
        self,
        video_id: str,
        opts: dict,
        client: str,
        video: bool,
        quality_tier: str | None,
    ):
        ydl, warm = self._get_persistent_direct_ydl(opts)
        started = time.monotonic()
        info = None
        diagnostic = "uninitialized"
        try:
            get_ie = getattr(ydl, "get_info_extractor", None)
            if not callable(get_ie):
                raise RuntimeError("yt_dlp_get_info_extractor_unavailable")
            ie = get_ie("Youtube")
            initialize = getattr(ie, "initialize", None)
            if callable(initialize):
                initialize()
            authenticated = bool(getattr(ie, "is_authenticated", False))
            get_default_ytcfg = getattr(ie, "_get_default_ytcfg", None)
            if not callable(get_default_ytcfg):
                raise RuntimeError("youtube_default_ytcfg_unavailable")
            player_ytcfg = get_default_ytcfg(client)
            if not isinstance(player_ytcfg, dict):
                raise RuntimeError("youtube_default_ytcfg_invalid")
            if authenticated and not bool(
                player_ytcfg.get("SUPPORTS_COOKIES", False)
            ):
                diagnostic = "auth_client_rejects_cookies"
                return (
                    None,
                    warm,
                    int((time.monotonic() - started) * 1000),
                    diagnostic,
                )
            if not authenticated and bool(player_ytcfg.get("REQUIRE_AUTH", False)):
                diagnostic = "client_requires_auth"
                return (
                    None,
                    warm,
                    int((time.monotonic() - started) * 1000),
                    diagnostic,
                )
            extract_player = getattr(ie, "_extract_player_response", None)
            if not callable(extract_player):
                raise RuntimeError("youtube_private_player_api_unavailable")
            response = extract_player(
                client,
                video_id,
                # Match yt-dlp's maintained no-webpage path: the client's
                # default Innertube context must feed both API headers and the
                # request context. Empty dicts caused authenticated production
                # lanes to return no usable formats.
                webpage_ytcfg=player_ytcfg,
                player_ytcfg=player_ytcfg,
                player_url=None,
                initial_pr=None,
                visitor_data=None,
                data_sync_id=None,
                po_token=None,
            )
            summary = summarize_player_response(response)
            diagnostic = (
                f"status={summary['status']};formats={summary['formats']};"
                f"adaptive={summary['adaptive']};usable={summary['usable']};"
                f"safe_cipher={summary['safe_cipher']};"
                f"encrypted_cipher={summary['encrypted_cipher']};"
                f"authenticated={int(authenticated)}"
            )
            info = self._direct_progressive_info(
                response,
                video_id,
                opts,
                client=client,
                video=bool(video),
                quality_tier=quality_tier,
            )
            if info is None and video and bool(
                getattr(config, "DIRECT_VIDEO_ADAPTIVE_PAIR", True)
            ):
                info = self._direct_adaptive_av_info(
                    response,
                    video_id,
                    opts,
                    client=client,
                    quality_tier=quality_tier,
                )
        except Exception as ex:
            diagnostic = f"exception={type(ex).__name__}"
            logger.debug(
                "direct_micro_player miss video_id=%s client=%s video=%s err=%s",
                video_id,
                client,
                int(bool(video)),
                type(ex).__name__,
            )
        return info, warm, int((time.monotonic() - started) * 1000), diagnostic

    async def _run_persistent_direct_player_candidate(
        self,
        video_id: str,
        opts: dict,
        *,
        client: str,
        video: bool,
        quality_tier: str | None,
        slot_hint: int,
    ):
        loop = asyncio.get_running_loop()
        slot = int(slot_hint) % len(self._direct_micro_executors)
        executor = self._direct_micro_executors[slot]
        info, warm, elapsed_ms, diagnostic = await loop.run_in_executor(
            executor,
            self._persistent_direct_player_candidate,
            video_id,
            opts,
            client,
            bool(video),
            quality_tier,
        )
        return info, warm, elapsed_ms, slot, diagnostic

    # Compatibility for existing tests/callers; current resolver uses the
    # multi-client candidate API above.
    def _persistent_direct_player18(self, video_id: str, opts: dict):
        return self._persistent_direct_player_candidate(
            video_id, opts, "mweb", False, None
        )

    async def _run_persistent_direct_player18(self, video_id: str, opts: dict):
        return await self._run_persistent_direct_player_candidate(
            video_id,
            opts,
            client="mweb",
            video=False,
            quality_tier=None,
            slot_hint=0,
        )

    async def _run_persistent_direct_micro_prepare(
        self, opts: dict, *, slot_hint: int | None = None
    ):
        loop = asyncio.get_running_loop()
        fingerprint = self._direct_resolver_opts_fingerprint(opts)
        slot = (
            int(slot_hint) % len(self._direct_micro_executors)
            if slot_hint is not None
            else int(fingerprint[:8], 16) % len(self._direct_micro_executors)
        )
        executor = self._direct_micro_executors[slot]
        warm, elapsed_ms = await loop.run_in_executor(
            executor, self._persistent_direct_prepare, opts
        )
        return warm, elapsed_ms, slot

    async def _run_persistent_direct_prepare(
        self, opts: dict, *, background140: bool = False
    ):
        loop = asyncio.get_running_loop()
        fingerprint = self._direct_resolver_opts_fingerprint(opts)
        executors = (
            self._direct_background140_executors
            if background140
            else self._direct_resolver_executors
        )
        slot = int(fingerprint[:8], 16) % len(executors)
        executor = executors[slot]
        warm, elapsed_ms = await loop.run_in_executor(
            executor, self._persistent_direct_prepare, opts
        )
        return warm, elapsed_ms, slot

    async def _run_persistent_direct_extract(
        self, url: str, opts: dict, *, background140: bool = False,
        resolver_slot_hint: int | None = None,
    ):
        loop = asyncio.get_running_loop()
        fingerprint = self._direct_resolver_opts_fingerprint(opts)
        # Exact-140 background work has a separate pool so it cannot block the
        # foreground fast profile. Foreground races may pin independent sticky
        # workers so one slow network/extractor flow cannot own the whole tail.
        executors = (
            self._direct_background140_executors
            if background140
            else self._direct_resolver_executors
        )
        slot = (
            int(resolver_slot_hint) % len(executors)
            if resolver_slot_hint is not None
            else int(fingerprint[:8], 16) % len(executors)
        )
        executor = executors[slot]
        info, warm, elapsed_ms = await loop.run_in_executor(
            executor, self._persistent_direct_extract, url, opts
        )
        return info, warm, elapsed_ms, slot

    async def warm_direct_resolver_runtime(self) -> None:
        """Pre-create sticky resolver runtimes during app startup.

        The latency-critical audio profile is the fast progressive mweb path.
        A second exact-140 profile is also constructed so background quality
        promotion does not pay constructor/plugin cost after playback starts.
        No real video is resolved during startup.
        """
        if not bool(getattr(config, "DIRECT_RESOLVER_STARTUP_WARM", True)):
            logger.info("direct_resolver startup_warm disabled")
            return

        profiles: list[tuple[str, dict, bool]] = []
        try:
            audio_base = self.build_ytdlp_api_opts(
                action="direct", video_id=None, socket_timeout=4
            )
            audio_base["format"] = "18/bestaudio[ext=m4a]/bestaudio/best"
            audio_fast = self._authoritative_pot_opts(
                audio_base, lightweight=True, fast_progressive=True
            )
            profiles.append(("audio-fast", audio_fast, False))

            audio140_base = self.build_ytdlp_api_opts(
                action="direct", video_id=None, socket_timeout=4
            )
            audio140_base["format"] = "140"
            audio140 = self._authoritative_pot_opts(
                audio140_base, lightweight=True, fast_progressive=False
            )
            profiles.append(("audio-140-background", audio140, True))
        except Exception as ex:
            logger.warning(
                "direct_resolver startup_warm skipped audio profiles err=%s",
                type(ex).__name__,
            )

        try:
            video_base = self.build_ytdlp_api_opts(
                action="direct", video_id=None, socket_timeout=4
            )
            video_base["format"] = self._direct_stream_format(True, quality_tier="normal")
            video_opts = self._authoritative_pot_opts(
                video_base, lightweight=True, fast_progressive=False
            )
            profiles.append(("video-normal-lightweight", video_opts, False))
        except Exception as ex:
            logger.warning(
                "direct_resolver startup_warm skipped profile=video-normal err=%s",
                type(ex).__name__,
            )

        if not profiles:
            return
        started = time.monotonic()
        results = await asyncio.gather(
            *(
                self._run_persistent_direct_prepare(opts, background140=background140)
                for _label, opts, background140 in profiles
            ),
            return_exceptions=True,
        )
        ready = []
        for (label, _opts, background140), result in zip(profiles, results):
            if isinstance(result, Exception):
                logger.warning(
                    "direct_resolver startup_warm failed profile=%s err=%s",
                    label, type(result).__name__,
                )
                continue
            was_warm, elapsed_ms, slot = result
            ready.append(
                f"{label}:slot{slot}:preexisting={int(bool(was_warm))}:ms={elapsed_ms}"
            )
        # Prime the dedicated player-response micro worker with the exact same
        # foreground profile so first uncached /play does not pay YoutubeDL/IE
        # construction before its single player API request.
        micro_warm_opts = next(
            (opts for label, opts, _background in profiles if label == "audio-fast"),
            None,
        )
        if (
            micro_warm_opts is not None
            and bool(getattr(config, "DIRECT_MWEB_MICRO_PLAYER", True))
        ):
            micro_clients = tuple(
                getattr(
                    config,
                    "DIRECT_MICRO_PLAYER_CLIENTS",
                    ("tv_downgraded", "web_safari", "android_vr"),
                )
                or ("android_vr",)
            )[: len(self._direct_micro_executors)]
            micro_results = await asyncio.gather(
                *(
                    self._run_persistent_direct_micro_prepare(
                        micro_warm_opts, slot_hint=idx
                    )
                    for idx, _client in enumerate(micro_clients)
                ),
                return_exceptions=True,
            )
            for client_name, result in zip(micro_clients, micro_results):
                if isinstance(result, Exception):
                    logger.warning(
                        "direct_resolver startup_warm failed profile=micro-%s err=%s",
                        client_name,
                        type(result).__name__,
                    )
                    continue
                micro_warm, micro_ms, micro_slot = result
                ready.append(
                    f"micro-{client_name}:slot{micro_slot}:"
                    f"preexisting={int(bool(micro_warm))}:ms={micro_ms}"
                )
        logger.info(
            "direct_resolver startup_warm ready=%s elapsed_ms=%s profiles=%s",
            len(ready),
            int((time.monotonic() - started) * 1000),
            ",".join(ready) or "none",
        )

    def warm_audio140_source(self, video_id: str) -> asyncio.Task | None:
        """Mint exact itag 140 + visible POT outside the audible critical path."""
        clean_id = str(video_id or "").strip()
        if len(clean_id) != 11 or not bool(
            getattr(config, "DIRECT_BACKGROUND_140_ENABLED", True)
        ):
            return None
        # Reserve direct-extract capacity for foreground. With only one live
        # yt-dlp permit, a slow background 140 job would necessarily block the
        # next /play, so skip promotion until capacity recovers.
        if resource_manager.max_ytdlp() < 2 or not resource_manager.allow_background_cache():
            logger.info(
                "direct_dual_stage background_140_skipped video_id=%s reason=capacity",
                clean_id,
            )
            return None
        now = time.monotonic()
        cached = self._direct_audio140_cache.get(clean_id)
        if cached is not None and cached[0] > now and cached[1].url:
            return None
        existing = self._direct_audio140_tasks.get(clean_id)
        if existing is not None and not existing.done():
            return existing

        async def _runner() -> DirectStreamSource:
            started = time.monotonic()
            with background_scope():
                async with self._direct_background140_semaphore:
                    source = await self._resolve_direct_stream_source_uncached(
                        clean_id,
                        video=False,
                        quality_tier=None,
                        allow_local_ready=False,
                        allow_authoritative_retry=True,
                        exact_audio140=True,
                    )
            if source.url and source.format_id == "140" and source.pot_bound:
                ttl = max(60.0, float(getattr(config, "DIRECT_BACKGROUND_140_TTL_SEC", 600.0) or 600.0))
                self._direct_audio140_cache[clean_id] = (time.monotonic() + ttl, source)
                logger.info(
                    "direct_dual_stage background_140_ready video_id=%s elapsed_ms=%s "
                    "format_id=%s pot_provenance=%s ttl_sec=%s",
                    clean_id, int((time.monotonic() - started) * 1000),
                    source.format_id, source.pot_provenance or "none", int(ttl),
                )
            else:
                logger.info(
                    "direct_dual_stage background_140_unavailable video_id=%s elapsed_ms=%s "
                    "reason=%s format_id=%s",
                    clean_id, int((time.monotonic() - started) * 1000),
                    source.reason or "unresolved", source.format_id or "none",
                )
            return source

        try:
            task = asyncio.create_task(
                _runner(), name=f"direct-audio140:{clean_id}"
            )
        except RuntimeError:
            return None
        self._direct_audio140_tasks[clean_id] = task

        def _done(done: asyncio.Task, *, _id=clean_id) -> None:
            if self._direct_audio140_tasks.get(_id) is done:
                self._direct_audio140_tasks.pop(_id, None)
            try:
                done.result()
            except (asyncio.CancelledError, Exception):
                return

        task.add_done_callback(_done)
        logger.info("direct_dual_stage background_140_started video_id=%s", clean_id)
        return task

    def warm_direct_stream_source(
        self, video_id: str, *, video: bool = False, quality_tier: str | None = None
    ) -> asyncio.Task | None:
        """Start direct resolution before the playback layer asks for it.

        Search/queue code can call this fire-and-forget. resolve_direct_stream_source
        already singleflights by media key, so foreground playback simply joins
        this task instead of launching a second extraction.
        """
        clean_id = str(video_id or "").strip()
        if len(clean_id) != 11 or not bool(getattr(config, "DIRECT_RESOLVER_PREWARM", True)):
            return None
        if not video:
            promoted = self._direct_audio140_cache.get(clean_id)
            if promoted is not None and promoted[0] > time.monotonic() and promoted[1].url:
                return None
        resolved_quality_tier = quality_tier
        if video:
            try:
                from AnonX_3.core.resource_budget import effective_quality_tier
                resolved_quality_tier = effective_quality_tier(quality_tier)
            except Exception:
                pass
        tier_key = self._normalize_quality_tier(resolved_quality_tier) if video else None
        key = (clean_id, bool(video), tier_key)
        cached = self._direct_stream_source_cache.get(key)
        if cached and cached[0] > time.monotonic() and cached[1].url:
            return None
        existing_warm = self._direct_warm_tasks.get(key)
        if existing_warm is not None and not existing_warm.done():
            return existing_warm
        if video:
            for warm_key, candidate in self._direct_warm_tasks.items():
                if (
                    warm_key != key
                    and warm_key[0] == clean_id
                    and warm_key[1]
                    and not candidate.done()
                ):
                    logger.info(
                        "direct_resolver prewarm_cross_tier_reused video_id=%s "
                        "requested_tier=%s warm_tier=%s duplicate_extract_avoided=1",
                        clean_id, tier_key or "none", warm_key[2] or "none",
                    )
                    return candidate
        task = self._direct_stream_inflight.get(key)
        if task is not None and not task.done():
            return task
        if video:
            for inflight_key, candidate in self._direct_stream_inflight.items():
                if (
                    inflight_key != key
                    and inflight_key[0] == clean_id
                    and inflight_key[1]
                    and not candidate.done()
                ):
                    logger.info(
                        "direct_resolver prewarm_inflight_cross_tier_reused "
                        "video_id=%s requested_tier=%s inflight_tier=%s "
                        "duplicate_extract_avoided=1",
                        clean_id, tier_key or "none", inflight_key[2] or "none",
                    )
                    return candidate
        try:
            task = asyncio.create_task(
                self.resolve_direct_stream_source(
                    clean_id, video=video, quality_tier=resolved_quality_tier, prefer_remote=True
                ),
                name=f"direct-prewarm:{clean_id}:{int(bool(video))}",
            )
        except RuntimeError:
            return None
        self._direct_warm_tasks[key] = task
        self._direct_warm_started_at[key] = time.monotonic()
        def _done(done: asyncio.Task, *, _key=key):
            if self._direct_warm_tasks.get(_key) is done:
                self._direct_warm_tasks.pop(_key, None)
                self._direct_warm_started_at.pop(_key, None)
            try:
                result = done.result()
            except (asyncio.CancelledError, Exception):
                return
            if isinstance(result, DirectStreamSource) and result.url:
                now = time.monotonic()
                if len(self._direct_warm_results) >= 256:
                    self._direct_warm_results = {
                        cache_key: value
                        for cache_key, value in self._direct_warm_results.items()
                        if value[0] > now
                    }
                self._direct_warm_results[_key] = (now + 15.0, result)
        task.add_done_callback(_done)
        logger.info(
            "direct_resolver prewarm_started video_id=%s video=%s",
            clean_id, int(bool(video)),
        )
        return task





    async def _resolve_direct_stream_source_uncached(
        self,
        video_id: str,
        video: bool = False,
        quality_tier: str | None = None,
        allow_local_ready: bool = True,
        allow_authoritative_retry: bool = True,
        exact_audio140: bool = False,
    ) -> DirectStreamSource:
        """Resolve a direct source using the dual-stage mweb strategy.

        Audio foreground (``exact_audio140=False``) is intentionally optimized
        for time-to-audible: request the progressive AAC path, skip webpage and
        mweb config discovery, prove it with the one-byte GVS probe, and return.
        Exact adaptive itag 140 + visible POT is a separate background stage.
        /vplay retains the conservative mweb lightweight -> robust ladder.
        """
        url = self.base + video_id
        local_path = self.get_download_filename(
            video_id, video=video, quality_tier=quality_tier
        )
        if allow_local_ready:
            ready = self._local_ready_path(
                video_id, video=video, quality_tier=quality_tier
            )
            if ready:
                return self._empty_direct_source(
                    local_path=ready, video=video, reason="local_ready"
                )

        if self.auth_challenge_for(video_id) and not self.cookie_free_mode():
            logger.info(
                "direct_stream auth challenge circuit skip video_id=%s", video_id
            )
            return self._empty_direct_source(
                local_path=local_path, video=video, reason="auth_challenge_circuit"
            )

        try:
            base_opts = self.build_ytdlp_api_opts(
                action="direct", video_id=video_id, socket_timeout=4
            )
        except YouTubeRuntimeConfigError:
            return self._empty_direct_source(
                local_path=local_path, video=video, reason="invalid_cookie_file"
            )

        extractor_args = base_opts.get("extractor_args") or {}
        if "youtubepot-bgutilhttp" not in extractor_args:
            logger.warning(
                "direct_stream authoritative_mweb_pot unavailable video_id=%s "
                "reason=po_provider_missing action=local_fallback",
                video_id,
            )
            return self._empty_direct_source(
                local_path=local_path, video=video, reason="po_provider_missing"
            )

        player_client = (
            getattr(config, "PO_TOKEN_CLIENT", "mweb") or "mweb"
        ).strip() or "mweb"

        profiles: list[tuple[str, dict, bool]] = []
        if not video and not exact_audio140:
            # Fast stage observed at ~1.6-2.5s in production. itag 18 is AAC and
            # survives the no-config mweb path; exact 140 is promoted later.
            fast_base = deepcopy(base_opts)
            fast_base["format"] = "18/bestaudio[ext=m4a]/bestaudio/best"
            fast_opts = self._authoritative_pot_opts(
                fast_base, lightweight=True, fast_progressive=True
            )
            profiles.append(("foreground_fast", fast_opts, False))
            if bool(getattr(config, "DIRECT_AUDIO_ESCAPE_RACE", True)):
                escape_base = deepcopy(base_opts)
                escape_base["format"] = (
                    "18/bestaudio[ext=m4a][acodec!=none]/"
                    "bestaudio[acodec^=mp4a]/bestaudio[acodec!=none]"
                )
                escape_opts = self._authoritative_pot_opts(
                    escape_base, lightweight=True, fast_progressive=True
                )
                profiles.append(("audio_escape_fast", escape_opts, False))
            if allow_authoritative_retry:
                robust_audio_base = deepcopy(base_opts)
                robust_audio_base["format"] = (
                    "bestaudio[ext=m4a][acodec!=none]/"
                    "bestaudio[acodec^=mp4a]/bestaudio[acodec!=none]/"
                    "bestaudio/best[acodec!=none]/best"
                )
                robust_audio = self._authoritative_pot_opts(
                    robust_audio_base, lightweight=False, fast_progressive=False
                )
                profiles.append(("foreground_audio_robust", robust_audio, False))
            logger.info(
                "direct_resolver mode=dual_stage_foreground_fast video_id=%s "
                "player_client=%s robust_audio_fallback=enabled background_140=enabled",
                video_id, player_client,
            )
        elif not video and exact_audio140:
            exact_base = deepcopy(base_opts)
            exact_base["format"] = "140"
            exact_light = self._authoritative_pot_opts(
                exact_base, lightweight=True, fast_progressive=False
            )
            profiles.append(("background_140", exact_light, True))
            if allow_authoritative_retry:
                exact_robust = self._authoritative_pot_opts(
                    exact_base, lightweight=False
                )
                profiles.append(("background_140_robust", exact_robust, True))
            logger.info(
                "direct_resolver mode=dual_stage_background_140 video_id=%s "
                "player_client=%s",
                video_id, player_client,
            )
        else:
            video_base = deepcopy(base_opts)
            video_base["format"] = self._direct_stream_format(
                True, quality_tier=quality_tier
            )
            light = self._authoritative_pot_opts(
                video_base, lightweight=True, fast_progressive=False
            )
            profiles.append(("video_lightweight", light, False))
            escape_base = deepcopy(base_opts)
            escape_base["format"] = (
                "18/best[ext=mp4][acodec!=none][vcodec!=none][height<=?360]/"
                "best[acodec!=none][vcodec!=none][height<=?360]"
            )
            escape = self._authoritative_pot_opts(
                escape_base, lightweight=True, fast_progressive=True
            )
            profiles.append(("video_escape_fast", escape, False))
            robust = self._authoritative_pot_opts(video_base, lightweight=False)
            profiles.append(("video_robust", robust, False))
            logger.info(
                "direct_resolver mode=authoritative_mweb_pot_video video_id=%s "
                "player_client=%s",
                video_id, player_client,
            )

        def _source_from_info(info: dict, extract_opts: dict, profile: str):
            if not isinstance(info, dict):
                return None, None
            metadata = self._metadata_from_direct_info(info, video_id)
            if profile == "foreground_fast":
                # Preserve yt-dlp's selected progressive top-level item first.
                source = self._direct_source_from_info(
                    info=info, fmt_item=None, local_path=local_path,
                    video=False, proxy=extract_opts.get("proxy"),
                )
                if source is None:
                    source = self._select_direct_source_from_info(
                        info=info, local_path=local_path, video=False,
                        proxy=extract_opts.get("proxy"),
                    )
            else:
                source = self._select_direct_source_from_info(
                    info=info, local_path=local_path, video=video,
                    proxy=extract_opts.get("proxy"),
                )
            return source, metadata

        def _cache_metadata(metadata: dict | None) -> None:
            if not metadata:
                return
            now = time.monotonic()
            self._prune_ttl_cache(self._direct_metadata_cache, now, 1024)
            self._direct_metadata_cache[video_id] = (
                now + self._search_cache_ttl, metadata
            )

        # Start the two fast authoritative full-resolver lanes immediately on
        # distinct sticky workers. The tiny player-response lane races beside
        # them rather than running serially in front of them.
        prestarted: dict[str, asyncio.Task] = {}
        fast_race_labels: list[str] = []
        if not exact_audio140 and profiles:
            fast_race_labels.append(profiles[0][0])
            if len(profiles) > 1 and profiles[1][0] in {
                "audio_escape_fast", "video_escape_fast"
            }:
                fast_race_labels.append(profiles[1][0])

        async def _run_prestarted_extract(
            profile: str, extract_opts: dict, require_140: bool, slot_hint: int
        ):
            resource_manager.note_extract(+1)
            extract_started = time.monotonic()
            try:
                async with self._direct_foreground_resolver_semaphore:
                    info, ydl_warm, extract_inner_ms, resolver_slot = (
                        await self._run_persistent_direct_extract(
                            url,
                            extract_opts,
                            background140=bool(require_140),
                            resolver_slot_hint=slot_hint,
                        )
                    )
            finally:
                resource_manager.note_extract(-1)
            return (
                info,
                ydl_warm,
                extract_inner_ms,
                resolver_slot,
                int((time.monotonic() - extract_started) * 1000),
            )

        for idx, (profile, extract_opts, require_140) in enumerate(profiles[:2]):
            if profile not in fast_race_labels:
                continue
            task = asyncio.create_task(
                _run_prestarted_extract(
                    profile, extract_opts, require_140, 0 if idx == 0 else 1
                ),
                name=f"direct-authoritative-race:{video_id}:{profile}",
            )
            prestarted[profile] = task

        micro_total_budget = max(
            0.50,
            min(
                1.45,
                float(
                    getattr(config, "DIRECT_MICRO_TOTAL_BUDGET_SEC", 1.45)
                    or 1.45
                ),
            ),
        )
        micro_enabled = bool(
            not exact_audio140
            and bool(getattr(config, "DIRECT_MWEB_MICRO_PLAYER", True))
            and bool(getattr(config, "DIRECT_RESOLVER_PARALLEL_MICRO", True))
            and profiles
        )

        async def _validate_prestarted_candidate(
            profile: str,
            extract_opts: dict,
            require_140: bool,
            extract_task: asyncio.Task,
        ) -> DirectStreamSource | None:
            """Validate a full-extractor hedge without serializing its probe."""
            try:
                (
                    info,
                    ydl_warm,
                    extract_inner_ms,
                    resolver_slot,
                    extract_ms,
                ) = await extract_task
                source, metadata = _source_from_info(info, extract_opts, profile)
                _cache_metadata(metadata)
                if not (source and source.url):
                    logger.info(
                        "direct_dual_stage miss video_id=%s profile=%s extract_ms=%s",
                        video_id,
                        profile,
                        extract_ms,
                    )
                    return None
                if require_140 and source.format_id != "140":
                    logger.info(
                        "direct_dual_stage format_rejected video_id=%s profile=%s "
                        "requested=140 actual=%s",
                        video_id,
                        profile,
                        source.format_id or "none",
                    )
                    return None

                probe_timeout = float(
                    getattr(
                        config,
                        "DIRECT_AUTHORITATIVE_POT_PREFLIGHT_TIMEOUT_SEC",
                        1.5,
                    )
                    or 1.5
                )
                probe_started = time.monotonic()
                status = await self._probe_direct_source_status(
                    source, timeout=probe_timeout
                )
                preflight_ms = int((time.monotonic() - probe_started) * 1000)
                logger.info(
                    "direct_dual_stage timing video_id=%s stage=%s extract_ms=%s "
                    "inner_ms=%s ydl_warm=%s worker_slot=%s preflight_ms=%s "
                    "requested_format=%s status=%s validation_race=1",
                    video_id,
                    profile,
                    extract_ms,
                    extract_inner_ms,
                    int(bool(ydl_warm)),
                    resolver_slot,
                    preflight_ms,
                    extract_opts.get("format", "auto"),
                    status,
                )
                if status not in (200, 206):
                    return None

                actual_client = (source.client or "").strip()
                if actual_client and actual_client != player_client:
                    logger.warning(
                        "direct_dual_stage client_binding_rejected video_id=%s "
                        "stage=%s expected=%s actual=%s",
                        video_id,
                        profile,
                        player_client,
                        actual_client,
                    )
                    return None

                pot_query = parse_qs(urlparse(source.url or "").query).get("pot") or []
                pot_visible = bool(pot_query and str(pot_query[0]).strip())
                if require_140 and not pot_visible:
                    logger.info(
                        "direct_dual_stage background_140_missing_visible_pot "
                        "video_id=%s action=discard",
                        video_id,
                    )
                    return None
                provenance = (
                    "visible_url"
                    if pot_visible
                    else (
                        "fast_gvs_206"
                        if profile == "foreground_fast"
                        else "gvs_206_no_visible_pot"
                    )
                )
                self._clear_auth_challenge()
                source = replace(
                    source,
                    reason=profile,
                    preflight_status=status,
                    client=actual_client or player_client,
                    pot_bound=pot_visible,
                    pot_provenance=provenance,
                )
                logger.info(
                    "direct_stream resolved video_id=%s video=%s stage=%s "
                    "url_host=%s audio_format=%s format_id=%s "
                    "preflight_status=%s pot_bound=%s pot_provenance=%s",
                    video_id,
                    int(bool(video)),
                    profile,
                    source.host,
                    source.audio_format,
                    source.format_id or "?",
                    status,
                    int(bool(pot_visible)),
                    provenance,
                )
                return source
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                cls = classify_error(ex)
                logger.info(
                    "direct_dual_stage extract_fail video_id=%s stage=%s "
                    "class=%s msg=%s validation_race=1",
                    video_id,
                    profile,
                    cls.name,
                    cls.message[:160],
                )
                return None

        def _keep_direct_race_task(task: asyncio.Task) -> None:
            """Own and consume a detached executor-backed race task."""
            if task in self._direct_race_tasks:
                return
            self._direct_race_tasks.add(task)

            def _consume(done: asyncio.Task) -> None:
                self._direct_race_tasks.discard(done)
                try:
                    done.result()
                except (asyncio.CancelledError, Exception):
                    return

            task.add_done_callback(_consume)

        if prestarted:
            logger.info(
                "direct_resolver_race_started video_id=%s video=%s lanes=%s "
                "micro_budget_ms=%s fastest_valid_206=1 foreground_slots=%s "
                "dynamic_ytdlp=%s",
                video_id,
                int(bool(video)),
                ",".join(
                    list(prestarted) + (["micro:bounded"] if micro_enabled else [])
                ),
                int(micro_total_budget * 1000),
                self._direct_foreground_resolver_slots,
                resource_manager.max_ytdlp(),
            )

        micro_tasks: dict[str, asyncio.Task] = {}
        if micro_enabled:
            micro_opts = profiles[0][1]
            micro_profile = profiles[0][0]
            micro_clients = tuple(
                getattr(
                    config,
                    "DIRECT_MICRO_PLAYER_CLIENTS",
                    ("tv_downgraded", "web_safari", "android_vr"),
                )
                or ("android_vr",)
            )[: len(self._direct_micro_executors)]
            lane_timeout = max(
                0.35,
                float(getattr(config, "DIRECT_MICRO_LANE_TIMEOUT_SEC", 1.35) or 1.35),
            )
            probe_timeout = max(
                0.20,
                float(getattr(config, "DIRECT_MICRO_PROBE_TIMEOUT_SEC", 0.65) or 0.65),
            )

            async def _resolve_micro_candidate(client_name: str, slot_hint: int):
                micro_started = time.monotonic()
                micro_deadline = micro_started + micro_total_budget
                micro_source = None
                try:
                    resource_manager.note_extract(+1)
                    try:
                        (
                            micro_info,
                            micro_warm,
                            micro_inner_ms,
                            micro_slot,
                            micro_diagnostic,
                        ) = (
                            await asyncio.wait_for(
                                self._run_persistent_direct_player_candidate(
                                    video_id,
                                    micro_opts,
                                    client=client_name,
                                    video=bool(video),
                                    quality_tier=quality_tier,
                                    slot_hint=slot_hint,
                                ),
                                timeout=min(
                                    lane_timeout,
                                    max(0.25, micro_total_budget - 0.20),
                                ),
                            )
                        )
                    finally:
                        resource_manager.note_extract(-1)
                    micro_source, micro_metadata = _source_from_info(
                        micro_info or {}, micro_opts, micro_profile
                    )
                    _cache_metadata(micro_metadata)
                    if video and micro_source is not None:
                        has_progressive_video = str(
                            getattr(micro_source, "vcodec", "") or ""
                        ).lower() not in {"", "none"}
                        has_adaptive_pair = bool(
                            str(getattr(micro_source, "video_url", "") or "")
                        )
                        if not (has_progressive_video or has_adaptive_pair):
                            micro_source = None
                    if not (micro_source and micro_source.url):
                        logger.info(
                            "direct_micro_player fallback video_id=%s client=%s video=%s "
                            "elapsed_ms=%s reason=no_direct_progressive detail=%s race=1 "
                            "authoritative_already_running=1",
                            video_id, client_name, int(bool(video)),
                            int((time.monotonic() - micro_started) * 1000),
                            micro_diagnostic,
                        )
                        return None
                    remaining = micro_deadline - time.monotonic()
                    if remaining <= 0.05:
                        logger.info(
                            "direct_micro_player fallback video_id=%s client=%s "
                            "video=%s elapsed_ms=%s reason=total_budget_exhausted "
                            "race=1 authoritative_already_running=1",
                            video_id,
                            client_name,
                            int(bool(video)),
                            int((time.monotonic() - micro_started) * 1000),
                        )
                        return None
                    effective_probe_timeout = min(
                        probe_timeout,
                        max(0.05, remaining - 0.02),
                    )
                    probe_started = time.monotonic()
                    micro_status = await asyncio.wait_for(
                        self._probe_direct_source_status(
                            micro_source, timeout=effective_probe_timeout
                        ),
                        timeout=max(0.05, remaining),
                    )
                    preflight_ms = int((time.monotonic() - probe_started) * 1000)
                    logger.info(
                        "direct_micro_player timing video_id=%s client=%s video=%s "
                        "api_ms=%s ydl_warm=%s worker_slot=%s preflight_ms=%s "
                        "status=%s format_id=%s cipher_recovered=%s total_ms=%s",
                        video_id, client_name, int(bool(video)), micro_inner_ms,
                        int(bool(micro_warm)), micro_slot, preflight_ms, micro_status,
                        micro_source.format_id or "?",
                        int(bool((micro_info or {}).get("_micro_cipher_recovered"))),
                        int((time.monotonic() - micro_started) * 1000),
                    )
                    if micro_status not in (200, 206):
                        logger.info(
                            "direct_micro_player fallback video_id=%s client=%s video=%s "
                            "elapsed_ms=%s reason=preflight_rejected race=1 "
                            "authoritative_already_running=1",
                            video_id, client_name, int(bool(video)),
                            int((time.monotonic() - micro_started) * 1000),
                        )
                        return None
                    self._clear_auth_challenge()
                    has_pair = bool(str(getattr(micro_source, "video_url", "") or ""))
                    if video:
                        reason = (
                            "video_micro_adaptive_pair" if has_pair
                            else "foreground_micro_progressive_video"
                        )
                    else:
                        reason = (
                            "foreground_micro18"
                            if str(micro_source.format_id or "") == "18"
                            else "foreground_micro_adaptive_audio"
                        )
                    return replace(
                        micro_source,
                        reason=reason,
                        preflight_status=micro_status,
                        client=client_name,
                        pot_bound=False,
                        pot_provenance="fast_gvs_206",
                    )
                except asyncio.TimeoutError:
                    logger.info(
                        "direct_micro_player timeout video_id=%s client=%s video=%s "
                        "elapsed_ms=%s budget_ms=%s race=1 authoritative_already_running=1",
                        video_id, client_name, int(bool(video)),
                        int((time.monotonic() - micro_started) * 1000),
                        int(micro_total_budget * 1000),
                    )
                    return None
                except asyncio.CancelledError:
                    raise
                except Exception as ex:
                    logger.info(
                        "direct_micro_player fallback video_id=%s client=%s video=%s "
                        "elapsed_ms=%s reason=%s race=1 authoritative_already_running=1",
                        video_id, client_name, int(bool(video)),
                        int((time.monotonic() - micro_started) * 1000),
                        type(ex).__name__,
                    )
                    return None

            for idx, client_name in enumerate(micro_clients):
                task = asyncio.create_task(
                    _resolve_micro_candidate(client_name, idx),
                    name=f"direct-micro-race:{video_id}:{client_name}:{int(bool(video))}",
                )
                micro_tasks[client_name] = task

            if micro_tasks:
                logger.info(
                    "direct_resolver_micro_race_started video_id=%s video=%s clients=%s "
                    "budget_ms=%s fastest_valid_206=1",
                    video_id, int(bool(video)), ",".join(micro_tasks),
                    int(micro_total_budget * 1000),
                )

        # A full lane has not won until its signed URL passes the same 200/206
        # proof as a micro candidate. Validate both full hedges concurrently and
        # keep waiting for a valid micro response while those probes run. This
        # removes the old extract-complete -> serial-preflight tail.
        validated_fast_tasks: dict[str, asyncio.Task] = {}
        profile_by_name = {item[0]: item for item in profiles}
        for profile, extract_task in prestarted.items():
            _label, extract_opts, require_140 = profile_by_name[profile]
            validated_fast_tasks[profile] = asyncio.create_task(
                _validate_prestarted_candidate(
                    profile,
                    extract_opts,
                    require_140,
                    extract_task,
                ),
                name=f"direct-validated-race:{video_id}:{profile}",
            )

        race_labels: dict[asyncio.Task, str] = {
            task: f"micro:{client}" for client, task in micro_tasks.items()
        }
        race_labels.update(
            {task: profile for profile, task in validated_fast_tasks.items()}
        )
        pending = set(race_labels)
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    source = await task
                    if source is None:
                        continue
                    lane = race_labels[task]
                    for loser in race_labels:
                        if loser is not task:
                            _keep_direct_race_task(loser)
                    if lane.startswith("micro:"):
                        logger.info(
                            "direct_stream resolved video_id=%s video=%s stage=%s "
                            "url_host=%s audio_format=%s format_id=%s "
                            "preflight_status=%s pot_bound=0 "
                            "pot_provenance=fast_gvs_206",
                            video_id,
                            int(bool(video)),
                            source.reason,
                            source.host,
                            source.audio_format,
                            source.format_id or "?",
                            source.preflight_status,
                        )
                    logger.info(
                        "direct_resolver_race_winner video_id=%s video=%s lane=%s "
                        "status=%s losers_detached=%s validation_complete=1",
                        video_id,
                        int(bool(video)),
                        lane,
                        source.preflight_status,
                        max(0, len(race_labels) - 1),
                    )
                    return source
        except asyncio.CancelledError:
            # Thread-pool work cannot be force-cancelled safely. Retain strong
            # ownership and consume every result while request cancellation
            # propagates immediately to the caller.
            for task in race_labels:
                if not task.done():
                    _keep_direct_race_task(task)
            raise

        # Both prestarted fast lanes were fully consumed by the validated race.
        # Continue only with the robust profiles; never extract or probe them a
        # second time after a miss.
        profiles = [item for item in profiles if item[0] not in prestarted]

        for attempt, (profile, extract_opts, require_140) in enumerate(profiles, 1):
            if profile not in prestarted and attempt > 1:
                await asyncio.sleep(0.05)
            try:
                if profile in prestarted:
                    (
                        info, ydl_warm, extract_inner_ms, resolver_slot, extract_ms
                    ) = await prestarted[profile]
                    source, metadata = _source_from_info(info, extract_opts, profile)
                else:
                    resource_manager.note_extract(+1)
                    extract_started = time.monotonic()
                    try:
                        async with resource_manager.extract_semaphore():
                            info, ydl_warm, extract_inner_ms, resolver_slot = (
                                await self._run_persistent_direct_extract(
                                    url,
                                    extract_opts,
                                    background140=bool(require_140),
                                )
                            )
                            source, metadata = _source_from_info(info, extract_opts, profile)
                    finally:
                        resource_manager.note_extract(-1)
                    extract_ms = int((time.monotonic() - extract_started) * 1000)
                _cache_metadata(metadata)

                if not (source and source.url):
                    logger.info(
                        "direct_dual_stage miss video_id=%s profile=%s extract_ms=%s",
                        video_id, profile, extract_ms,
                    )
                    continue
                if require_140 and source.format_id != "140":
                    logger.info(
                        "direct_dual_stage format_rejected video_id=%s profile=%s "
                        "requested=140 actual=%s",
                        video_id, profile, source.format_id or "none",
                    )
                    continue

                self._clear_auth_challenge()
                probe_timeout = float(
                    getattr(config, "DIRECT_AUTHORITATIVE_POT_PREFLIGHT_TIMEOUT_SEC", 1.5)
                    or 1.5
                )
                probe_started = time.monotonic()
                status = await self._probe_direct_source_status(source, timeout=probe_timeout)
                preflight_ms = int((time.monotonic() - probe_started) * 1000)
                logger.info(
                    "direct_dual_stage timing video_id=%s stage=%s extract_ms=%s "
                    "inner_ms=%s ydl_warm=%s worker_slot=%s preflight_ms=%s "
                    "requested_format=%s status=%s",
                    video_id, profile, extract_ms, extract_inner_ms,
                    int(bool(ydl_warm)), resolver_slot, preflight_ms,
                    extract_opts.get("format", "auto"), status,
                )
                if status not in (200, 206):
                    if status == 403 and require_140:
                        purged = _purge_ytdlp_pot_cache()
                        logger.info(
                            "direct_dual_stage background_140_403 video_id=%s "
                            "pot_cache_purged=%s",
                            video_id, purged,
                        )
                    continue

                actual_client = (source.client or "").strip()
                if actual_client and actual_client != player_client:
                    logger.warning(
                        "direct_dual_stage client_binding_rejected video_id=%s "
                        "stage=%s expected=%s actual=%s",
                        video_id, profile, player_client, actual_client,
                    )
                    continue

                pot_query = parse_qs(urlparse(source.url or "").query).get("pot") or []
                pot_visible = bool(pot_query and str(pot_query[0]).strip())
                if require_140 and not pot_visible:
                    # Exact adaptive 140 is only promoted to cache when the POT
                    # binding is explicit. The foreground fast source does not
                    # require a visible token as long as its GVS URL proves 206.
                    logger.info(
                        "direct_dual_stage background_140_missing_visible_pot "
                        "video_id=%s action=discard",
                        video_id,
                    )
                    continue
                provenance = (
                    "visible_url" if pot_visible
                    else ("fast_gvs_206" if profile == "foreground_fast" else "gvs_206_no_visible_pot")
                )
                source = replace(
                    source,
                    reason=profile,
                    preflight_status=status,
                    client=actual_client or player_client,
                    pot_bound=pot_visible,
                    pot_provenance=provenance,
                )
                logger.info(
                    "direct_stream resolved video_id=%s video=%s stage=%s "
                    "url_host=%s audio_format=%s format_id=%s preflight_status=%s "
                    "pot_bound=%s pot_provenance=%s",
                    video_id, int(bool(video)), profile, source.host,
                    source.audio_format, source.format_id or "?", status,
                    int(bool(pot_visible)), provenance,
                )
                if profile in prestarted:
                    detached = 0
                    for label, task in prestarted.items():
                        if label == profile or task.done():
                            continue
                        self._direct_race_tasks.add(task)
                        task.add_done_callback(self._direct_race_tasks.discard)
                        detached += 1
                    logger.info(
                        "direct_resolver_race_winner video_id=%s video=%s "
                        "lane=%s status=%s losers_detached=%s",
                        video_id, int(bool(video)), profile, status, detached,
                    )
                return source
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                cls = classify_error(ex)
                logger.info(
                    "direct_dual_stage extract_fail video_id=%s stage=%s class=%s msg=%s",
                    video_id, profile, cls.name, cls.message[:160],
                )
                if profile != "foreground_fast" and cls.cls == ErrorClass.AUTH_CHALLENGE:
                    self._remember_auth_challenge(cls.message, video_id=video_id)
                    break

        return self._empty_direct_source(
            local_path=local_path,
            video=video,
            reason="no_direct_audio_source" if not video else "no_direct_video_source",
        )

    def _metadata_from_direct_info(self, info: dict, video_id: str) -> dict | None:
        """Normalize yt-dlp's direct extract into safe Track display fields."""
        if not isinstance(info, dict):
            return None
        title = str(info.get("title") or "").strip()
        try:
            duration_sec = max(0, int(float(info.get("duration") or 0)))
        except (TypeError, ValueError):
            duration_sec = 0
        if not title and duration_sec <= 0:
            return None
        thumbnail = str(info.get("thumbnail") or "").strip()
        webpage_url = str(info.get("webpage_url") or "").strip()
        if not webpage_url.startswith(("http://", "https://")):
            webpage_url = f"{self.base}{video_id}"
        return {
            "title": title[:80],
            "duration": self._seconds_to_hms(duration_sec) if duration_sec else "",
            "duration_sec": duration_sec,
            "channel_name": str(
                info.get("channel") or info.get("uploader") or "YouTube"
            ).strip(),
            "thumbnail": thumbnail
            if thumbnail.startswith(("http://", "https://"))
            else f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "url": webpage_url,
            "view_count": str(info.get("view_count") or ""),
        }

    def _track_from_direct_metadata(
        self,
        video_id: str,
        message_id: int,
        *,
        video: bool,
    ) -> Track | None:
        cached = self._direct_metadata_cache.get(video_id)
        if not cached:
            return None
        expiry, metadata = cached
        if expiry <= time.monotonic():
            self._direct_metadata_cache.pop(video_id, None)
            return None
        title = str(metadata.get("title") or "").strip()
        duration = str(metadata.get("duration") or "").strip()
        duration_sec = int(metadata.get("duration_sec") or 0)
        if not title or title == "YouTube Video":
            return None
        return Track(
            id=video_id,
            channel_name=metadata.get("channel_name") or "YouTube",
            duration=duration,
            duration_sec=duration_sec,
            message_id=message_id,
            title=title,
            thumbnail=metadata.get("thumbnail")
            or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            url=metadata.get("url") or f"{self.base}{video_id}",
            view_count=metadata.get("view_count") or "",
            video=video,
        )

    @staticmethod
    def _safe_thumb_url(thumbnails) -> str:
        if not isinstance(thumbnails, list) or not thumbnails:
            return ""
        thumb = thumbnails[-1] or {}
        url = thumb.get("url")
        if not isinstance(url, str):
            return ""
        return url.split("?")[0]

    def _track_from_result(self, data: dict, m_id: int, video: bool = False) -> Track | None:
        if not isinstance(data, dict):
            return None
        video_id = data.get("id")
        if isinstance(video_id, dict):
            video_id = video_id.get("videoId")
        video_id = str(video_id or "").strip()
        if not video_id:
            return None
        # Reject invalid video IDs (ytsearch:query, etc) - must be 11 chars alphanumeric/-/_
        if len(video_id) != 11 or not re.match(r'^[A-Za-z0-9_-]+$', video_id):
            return None
        link = (data.get("link") or "").strip()
        # Reject invalid URLs (ytsearch:, etc) - use YouTube base URL instead
        if not link.startswith(("http://", "https://")):
            link = f"{self.base}{video_id}"
        title = (data.get("title") or "").strip()
        duration = (data.get("duration") or "").strip()
        return Track(
            id=video_id,
            channel_name=(data.get("channel") or {}).get("name"),
            duration=duration,
            duration_sec=utils.to_seconds(duration),
            message_id=m_id,
            title=title[:80],
            thumbnail=self._safe_thumb_url(data.get("thumbnails")),
            url=link,
            view_count=(data.get("viewCount") or {}).get("short"),
            video=video,
        )

    async def _pyyt_search_tracks(
        self,
        query: str,
        m_id: int,
        video: bool = False,
        limit: int = 1,
    ) -> list[Track]:
        try:
            search_kwargs = {
                "limit": limit,
                "with_live": False,
                "timeout": 4,
                "max_retries": 1,
            }
            proxy = self._live_proxy()
            # py-yt-search builds can advertise **kwargs but still reject proxy
            # at runtime.  Try with proxy first, then without on TypeError.
            if proxy:
                search_kwargs["proxy"] = proxy
            try:
                searcher = VideosSearch(query, **search_kwargs)
            except TypeError:
                if "proxy" in search_kwargs:
                    search_kwargs.pop("proxy", None)
                    searcher = VideosSearch(query, **search_kwargs)
                else:
                    raise
            results = await searcher.next()
        except Exception as ex:
            logger.debug("youtube_path=pyyt_search_failed query=%r limit=%s error=%s", query, limit, ex)
            return []

        tracks = []
        for data in (results or {}).get("result") or []:
            track = self._track_from_result(data, m_id, video=video)
            if track is not None:
                tracks.append(track)
        if tracks:
            logger.info("youtube_path=pyyt_search count=%s query=%r", len(tracks), query)
        return tracks

    def _ytdlp_proxy_attempts(self) -> list[str | None]:
        """Prefer live auto-detected proxy, then direct (covers broken auto-proxy)."""
        attempts: list[str | None] = []
        proxy = self._live_proxy()
        if proxy:
            attempts.append(proxy)
        attempts.append(None)
        return attempts

    async def _ytdlp_search_tracks(
        self,
        query: str,
        m_id: int,
        video: bool = False,
        limit: int = 1,
    ) -> list[Track]:
        n = max(1, int(limit or 1))
        # Explicit ytsearchN: is more reliable than default_search alone.
        search_term = f"ytsearch{n}:{query}"
        last_error: Exception | None = None

        for proxy in self._ytdlp_proxy_attempts():
            def _extract(proxy_url: str | None = proxy):
                opts = self.build_ytdlp_api_opts(
                    action="search",
                    socket_timeout=10,
                    skip_download=True,
                    include_proxy=False,
                )
                opts.update(
                    {
                        "extract_flat": "in_playlist",
                        "default_search": f"ytsearch{n}",
                    }
                )
                if proxy_url:
                    opts["proxy"] = proxy_url
                with create_youtube_dl(opts, yt_dlp.YoutubeDL) as ydl:
                    return ydl.extract_info(search_term, download=False)

            try:
                payload = await asyncio.to_thread(_extract)
            except Exception as ex:
                last_error = ex
                desc = str(ex).lower()
                if "sign in to confirm" in desc and "not a bot" in desc:
                    logger.warning(
                        "youtube_path=ytdlp_search_auth_required query=%r limit=%s cookie=%s proxy=%s",
                        query,
                        limit,
                        self._configured_cookie_file()
                        if not self.cookie_free_mode()
                        else "disabled",
                        "yes" if proxy else "direct",
                    )
                    # Auth failures won't fix by dropping proxy — stop.
                    return []
                logger.warning(
                    "youtube_path=ytdlp_search_failed query=%r limit=%s proxy=%s error=%s",
                    query,
                    limit,
                    "yes" if proxy else "direct",
                    ex,
                )
                continue

            entries = []
            if isinstance(payload, dict):
                entries = payload.get("entries") or []
                # Single-result edge case
                if not entries and payload.get("id"):
                    entries = [payload]
            tracks = []
            for entry in entries[:n]:
                if not isinstance(entry, dict):
                    continue
                thumb = entry.get("thumbnail")
                thumbnails = [{"url": thumb}] if isinstance(thumb, str) and thumb else []
                track = self._track_from_result(
                    {
                        "id": entry.get("id"),
                        "title": entry.get("title"),
                        "duration": self._seconds_to_hms(entry.get("duration") or 0)
                        if entry.get("duration")
                        else "",
                        "thumbnails": thumbnails,
                        "link": entry.get("url")
                        or entry.get("webpage_url")
                        or f"{self.base}{entry.get('id', '')}",
                        "channel": {
                            "name": entry.get("channel") or entry.get("uploader")
                        },
                        "viewCount": {"short": ""},
                    },
                    m_id,
                    video=video,
                )
                if track is not None:
                    tracks.append(track)
            tracks = [
                track
                for track in tracks
                if not self.is_permanently_unavailable(getattr(track, "id", None))
            ]
            if tracks:
                self._note_proxy_success()
                logger.info(
                    "youtube_path=ytdlp_search count=%s query=%r proxy=%s",
                    len(tracks),
                    query,
                    "yes" if proxy else "direct",
                )
                return tracks

        if last_error is not None:
            self._note_proxy_failure()
            logger.warning(
                "youtube_path=ytdlp_search_exhausted query=%r limit=%s last=%s",
                query,
                limit,
                last_error,
            )
        return []

    @staticmethod
    def _clone_search_track(track: Track, m_id: int, video: bool) -> Track:
        cloned = copy(track)
        cloned.message_id = m_id
        cloned.video = video
        return cloned

    @staticmethod
    def _prune_ttl_cache(cache: dict, now: float, max_entries: int) -> None:
        """Bound lazy TTL caches without adding a maintenance task."""
        if len(cache) <= max_entries:
            return
        for key, value in list(cache.items()):
            try:
                expires = float(value[0])
            except (TypeError, ValueError, IndexError):
                expires = 0.0
            if expires <= now:
                cache.pop(key, None)
        while len(cache) > max_entries:
            cache.pop(next(iter(cache)), None)

    def _clone_search_tracks(
        self,
        tracks: list[Track],
        m_id: int,
        video: bool,
    ) -> list[Track]:
        return [
            self._clone_search_track(track, m_id, video)
            for track in tracks
            if not self.is_permanently_unavailable(getattr(track, "id", None))
        ]

    async def _search_uncached(
        self, query: str, m_id: int, video: bool
    ) -> Track | None:
        # ── Direct YouTube URL fast path ──
        # Skip search entirely for direct URLs — py_yt title lookup is optional.
        direct_match = self.regex.match(query)
        direct_id = direct_match.group(5) if direct_match else None
        if direct_id and len(direct_id) == 11:
            # Keep the lightweight provider budget tiny.  Do not launch a
            # direct yt-dlp extract here: the selected download owner will
            # publish the same metadata/source when this is a cold miss.
            try:
                tracks = await asyncio.wait_for(
                    self._pyyt_search_tracks(query, m_id, video=video, limit=1),
                    timeout=0.35,
                )
            except Exception:
                tracks = []
            if tracks:
                self.warm_direct_stream_source(
                    direct_id, video=bool(video), quality_tier=None
                )
                return tracks[0]

            resolved = self._track_from_direct_metadata(
                direct_id,
                m_id,
                video=video,
            )
            if resolved is not None:
                self.warm_direct_stream_source(
                    direct_id, video=bool(video), quality_tier=None
                )
                logger.info(
                    "youtube_path=direct_metadata video_id=%s duration_sec=%s",
                    direct_id,
                    resolved.duration_sec,
                )
                return resolved

            # Fail-soft only when every metadata source is unavailable.
            return Track(
                id=direct_id,
                channel_name="YouTube",
                duration="",
                duration_sec=0,
                message_id=m_id,
                title="YouTube Video",
                thumbnail=f"https://i.ytimg.com/vi/{direct_id}/hqdefault.jpg",
                url=f"{self.base}{direct_id}",
                view_count="",
                video=video,
            )

        # ── TEXT SEARCH — provider-only race, first valid wins ──
        # Proxy cuts per-hop latency; with proxy we can afford slightly longer
        # per-provider deadlines for reliability without hurting UX.
        has_proxy = bool(self._live_proxy())

        # Per-provider deadlines (inside asyncio.wait_for).  VideosSearch is
        # configured with a four-second socket budget below; an outer deadline
        # shorter than that turns healthy, slightly slow searches into misses.
        pyyt_deadline = 5.5 if has_proxy else 5.0
        api_deadline = 1.5 if has_proxy else 1.0
        race_deadline  = max(pyyt_deadline, api_deadline)

        async def _api_provider() -> list[Track]:
            try:
                tracks, err = await asyncio.wait_for(
                    self._api_search_videos(query, limit=1),
                    timeout=api_deadline,
                )
            except Exception:
                tracks, err = [], "network_error"
            if tracks:
                self._api_failure_count = 0
                return tracks
            if err:
                self._api_failure_count += 1
                if self._api_failure_count >= 3:
                    self._api_circuit_until = time.monotonic() + 30.0  # shorter cooldown
            return []

        async def _pyyt_provider() -> list[Track]:
            try:
                return await asyncio.wait_for(
                    self._pyyt_search_tracks(query, m_id, video=video, limit=1),
                    timeout=pyyt_deadline,
                )
            except Exception:
                return []

        # Launch only metadata providers here. The playback acquisition below
        # owns the one allowed yt-dlp invocation for a cold requested track.
        providers: list[asyncio.Task] = []
        if self._refresh_api_key_if_due() and time.monotonic() >= self._api_circuit_until:
            providers.append(asyncio.create_task(_api_provider(), name="search-api"))
        providers.append(asyncio.create_task(_pyyt_provider(), name="search-pyyt"))

        best: Track | None = None
        try:
            for completed in asyncio.as_completed(providers, timeout=race_deadline):
                try:
                    tracks = await completed
                except Exception:
                    continue
                if tracks:
                    best = self._clone_search_track(tracks[0], m_id, video)
                    warm_id = str(getattr(best, "id", "") or "").strip()
                    if len(warm_id) == 11:
                        # Earliest safe overlap point: the winning metadata
                        # provider has produced the ID, but search cleanup/cache
                        # publication has not completed yet.
                        self.warm_direct_stream_source(
                            warm_id, video=bool(video), quality_tier=None
                        )
                    # Found a result — cancel remaining providers.
                    for t in providers:
                        if not t.done():
                            t.cancel()
                    return best
        except TimeoutError:
            pass
        finally:
            for t in providers:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*providers, return_exceptions=True)

        # All providers failed or timed out — no result.
        return None

    async def search(self, query: str, m_id: int, video: bool = False) -> Track | None:
        normalized = re.sub(r"\s+", " ", str(query or "").strip()).casefold()
        key = (normalized, bool(video))
        now = time.monotonic()
        self._prune_ttl_cache(self._search_cache, now, 1024)
        if len(self._search_negative_cache) > 1024:
            self._search_negative_cache = {
                cache_key: expires
                for cache_key, expires in self._search_negative_cache.items()
                if expires > now
            }
        if self._search_negative_cache.get(key, 0.0) > now:
            return None
        cached = self._search_cache.get(key)
        if cached and cached[0] > now:
            if not self.is_permanently_unavailable(getattr(cached[1], "id", None)):
                cached_track = self._clone_search_track(cached[1], m_id, video)
                warm_id = str(getattr(cached_track, "id", "") or "").strip()
                if len(warm_id) == 11:
                    self.warm_direct_stream_source(
                        warm_id, video=bool(video), quality_tier=None
                    )
                return cached_track
            self._search_cache.pop(key, None)

        task = self._search_inflight.get(key)
        if task is None or task.done():
            task = asyncio.create_task(
                self._search_uncached(query, m_id, video),
                name=f"youtube-search:{hash(key)}",
            )
            self._search_inflight[key] = task
        try:
            result = await asyncio.shield(task)
        finally:
            if self._search_inflight.get(key) is task and task.done():
                self._search_inflight.pop(key, None)
        if result is None:
            self._search_negative_cache[key] = (
                time.monotonic() + self._search_negative_cache_ttl
            )
            return None
        self._search_negative_cache.pop(key, None)
        warm_id = str(getattr(result, "id", "") or "").strip()
        if len(warm_id) == 11:
            self.warm_direct_stream_source(
                warm_id, video=bool(video), quality_tier=None
            )
        self._search_cache[key] = (time.monotonic() + self._search_cache_ttl, copy(result))
        return self._clone_search_track(result, m_id, video)

    async def deep_search(
        self,
        query: str,
        m_id: int,
        video: bool = False,
        limit: int = 5,
        *,
        allow_ytdlp: bool = True,
    ) -> list[Track]:
        """Cached, single-flight provider race for result-list searches."""
        normalized = re.sub(r"\s+", " ", str(query or "").strip()).casefold()
        safe_limit = max(1, min(int(limit or 1), 20))
        key = (normalized, bool(video), safe_limit, bool(allow_ytdlp))
        now = time.monotonic()
        self._prune_ttl_cache(self._deep_search_cache, now, 512)
        cached = self._deep_search_cache.get(key)
        if cached and cached[0] > now:
            return self._clone_search_tracks(cached[1], m_id, video)

        task = self._deep_search_inflight.get(key)
        if task is None or task.done():
            task = asyncio.create_task(
                self._deep_search_uncached(
                    query,
                    0,
                    video=video,
                    limit=safe_limit,
                    allow_ytdlp=allow_ytdlp,
                ),
                name=f"youtube-deep-search:{hash(key)}",
            )
            self._deep_search_inflight[key] = task
        try:
            result = await asyncio.shield(task)
        finally:
            if self._deep_search_inflight.get(key) is task and task.done():
                self._deep_search_inflight.pop(key, None)
        filtered = self._clone_search_tracks(result or [], m_id, video)
        if filtered:
            self._deep_search_cache[key] = (
                time.monotonic() + self._deep_search_cache_ttl,
                [copy(track) for track in filtered],
            )
        return filtered

    async def _deep_search_uncached(
        self,
        query: str,
        m_id: int,
        video: bool = False,
        limit: int = 5,
        *,
        allow_ytdlp: bool = True,
    ) -> list[Track]:
        # ── DEEP SEARCH — metadata provider race ──
        # Callers on a first /play miss disable yt-dlp so its one acquisition
        # operation is reserved for the actual selected media file.
        has_proxy = bool(self._live_proxy())
        # Keep this race long enough for the py_yt four-second provider
        # timeout, while retaining a bounded fallback path.
        race_deadline = 6.0 if has_proxy else 5.0

        async def _api_worker() -> list | None:
            for attempt in range(2):
                try:
                    api_tracks, err = await asyncio.wait_for(
                        self._api_search_videos(query, limit=limit),
                        timeout=2.5,
                    )
                except Exception:
                    return None
                if api_tracks:
                    for tr in api_tracks:
                        tr.message_id = m_id
                        tr.video = video
                    return api_tracks
                if err in ("quota_exceeded", "invalid_key", "network_error"):
                    return None
                if attempt == 0:
                    await asyncio.sleep(0.5)
            return None

        async def _pyyt_worker() -> list | None:
            try:
                tracks = await asyncio.wait_for(
                    self._pyyt_search_tracks(query, m_id, video=video, limit=limit),
                    timeout=5.5 if has_proxy else 5.0,
                )
                return tracks or None
            except Exception:
                return None

        async def _ytdlp_worker() -> list | None:
            try:
                tracks = await asyncio.wait_for(
                    self._ytdlp_search_tracks(query, m_id, video=video, limit=limit),
                    timeout=5.0,
                )
                return tracks or None
            except Exception:
                return None

        # Parallel wave: all three providers race.
        tasks: list[asyncio.Task] = []
        if self._refresh_api_key_if_due() and time.monotonic() >= self._api_circuit_until:
            tasks.append(asyncio.create_task(_api_worker(), name=f"ds-api:{m_id}"))
        tasks.append(asyncio.create_task(_pyyt_worker(), name=f"ds-pyyt:{m_id}"))
        if allow_ytdlp:
            tasks.append(asyncio.create_task(_ytdlp_worker(), name=f"ds-ytdlp:{m_id}"))

        try:
            for completed in asyncio.as_completed(tasks, timeout=race_deadline):
                try:
                    result = await completed
                except Exception:
                    continue
                if result:
                    result = [
                        track
                        for track in result
                        if not self.is_permanently_unavailable(
                            getattr(track, "id", None)
                        )
                    ]
                if result:
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    return result
        except TimeoutError:
            pass
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        return []

    async def alternate_track(
        self,
        seed: Track | Media,
        *,
        m_id: int = 0,
        video: bool = False,
    ) -> Track | None:
        """Find another YouTube upload after the selected upload is unusable."""
        failed_id = str(getattr(seed, "id", "") or "")
        title = str(getattr(seed, "title", "") or "").strip()
        artist = str(
            getattr(seed, "channel_name", None)
            or getattr(seed, "artist", None)
            or ""
        ).strip()
        if not title or title.casefold() == "youtube video":
            return None
        # Try title+artist first, then title-only as fallback.
        queries: list[str] = []
        if artist:
            queries.append(f"{title} {artist}".strip())
        queries.append(title)
        exclude: set[str] = {failed_id}
        for query in queries:
            candidates = await self.deep_search(
                query,
                m_id or int(getattr(seed, "message_id", 0) or 0),
                video=video,
                limit=12,
            )
            for candidate in candidates:
                candidate_id = str(getattr(candidate, "id", "") or "")
                if (
                    candidate_id
                    and candidate_id not in exclude
                    and not self.is_permanently_unavailable(candidate_id)
                ):
                    candidate.user = getattr(seed, "user", None)
                    candidate.video = bool(video)
                    return candidate
                if candidate_id:
                    exclude.add(candidate_id)
        return None

    async def autoplay_track(
        self,
        seed: Track,
        m_id: int = 0,
        exclude_ids: set[str] | None = None,
        recent_title_keys: set[str] | None = None,
        recent_artist_keys: list[str] | tuple[str, ...] | None = None,
        current_artist_streak: int = 0,
        max_same_artist_streak: int = 2,
        required_overlap_min: int = 2,
        same_artist_penalty: float = 2.2,
        repeat_artist_streak_penalty: float = 12.0,
        recent_title_penalty: float = 8.0,
        seed_exact_title_penalty: float = 9.0,
        intent: str = "similar",
    ) -> Track | None:
        selected = await self.autoplay_selector.select(
            seed,
            self.deep_search,
            m_id=m_id,
            exclude_ids=exclude_ids,
            recent_title_keys=recent_title_keys,
            recent_artist_keys=recent_artist_keys,
            current_artist_streak=current_artist_streak,
            max_same_artist_streak=max_same_artist_streak,
            required_overlap_min=required_overlap_min,
            same_artist_penalty=same_artist_penalty,
            repeat_artist_streak_penalty=repeat_artist_streak_penalty,
            recent_title_penalty=recent_title_penalty,
            seed_exact_title_penalty=seed_exact_title_penalty,
            intent=intent,
        )
        if selected is not None:
            selected.video = bool(getattr(seed, "video", False))
        return selected

    async def playlist(self, limit: int, user: str, url: str, video: bool) -> list[Track | None]:
        playlist_id = self._parse_playlist_id(url)
        if playlist_id:
            api_tracks, _ = await self._api_playlist_items(playlist_id, limit=limit or 20)
            if api_tracks:
                for tr in api_tracks:
                    tr.user = user
                    tr.video = video
                return api_tracks
        tracks = []
        try:
            plist = await Playlist.get(url)
            for data in plist["videos"][:limit]:
                vid = data.get("id") or ""
                raw_link = (data.get("link") or "").split("&list=")[0]
                # Validate URL - use YouTube base URL if invalid
                link = raw_link if raw_link.startswith(("http://", "https://")) else f"{self.base}{vid}"
                track = Track(
                    id=vid,
                    channel_name=data.get("channel", {}).get("name", ""),
                    duration=data.get("duration"),
                    duration_sec=utils.to_seconds(data.get("duration")),
                    title=(data.get("title") or "")[:80],
                    thumbnail=data.get("thumbnails")[-1].get("url").split("?")[0],
                    url=link,
                    user=user,
                    view_count="",
                    video=video,
                )
                tracks.append(track)
        except Exception:
            pass
        return tracks

    async def _edit_download_progress_card(
        self,
        *,
        progress_message,
        progress_lang: dict | None,
        progress_media: Media | Track | None,
        current: int,
        total: int,
        speed: float = 0.0,
        eta_seconds: int = 0,
    ):
        """Render one truthful byte-progress state on the existing status card."""
        if progress_message is None:
            return None
        lang_map = (
            progress_lang
            or getattr(progress_message, "lang", None)
            or {}
        )
        custom_downloading = getattr(
            progress_media, "download_progress_template", None
        )
        if custom_downloading is None:
            custom_downloading = await db.get_custom_text_for_chat(
                getattr(getattr(progress_message, "chat", None), "id", 0),
                "play_downloading",
                lang_map.get("play_downloading", "Downloading..."),
            )
        cancel_label = getattr(
            progress_media,
            "download_progress_cancel_label",
            lang_map.get("cancel", "Cancel"),
        )
        rendered = utils.render_download_progress(
            custom_downloading,
            current=current,
            total=total,
            speed=speed,
            eta_seconds=eta_seconds,
        )
        result = await utils.edit_download_progress(
            progress_message,
            rendered,
            reply_markup=buttons.cancel_dl(cancel_label),
            media=progress_media,
            ignore_stale=True,
        )
        return result

    async def render_completed_download_progress(
        self,
        path: str | None,
        *,
        progress_message=None,
        progress_lang: dict | None = None,
        progress_media: Media | Track | None = None,
    ) -> bool:
        """Finalize a visible cache/READY hit at a truthful 100%."""
        if progress_message is None or not path:
            return False
        try:
            size = max(1, Path(path).stat().st_size)
            edited = await self._edit_download_progress_card(
                progress_message=progress_message,
                progress_lang=progress_lang,
                progress_media=progress_media,
                current=size,
                total=size,
            )
            return edited is not None
        except Exception as ex:
            logger.debug(
                "completed download progress edit skipped path=%s error=%s",
                path,
                type(ex).__name__,
            )
            return False

    async def download(
        self,
        video_id: str,
        video: bool = False,
        quality_tier: str | None = None,
        message_id: int | None = None,
        progress_message=None,
        progress_lang: dict | None = None,
        progress_throttle: float = 5.0,
        progress_media: Media | Track | None = None,
        stream_for_playback: bool = False,
        one_shot: bool = False,
        quality_tier_resolved: bool = False,
    ) -> str | None:
        url = self.base + video_id
        if not quality_tier_resolved:
            quality_tier = self.resolve_download_quality_tier(
                quality_tier, video=video
            )
        else:
            quality_tier = self._normalize_quality_tier(quality_tier) if video else None
        initial_stream_key = self._download_stream_key(
            video_id, video=video, quality_tier=quality_tier
        )
        try:
            from AnonX_3.core.security import validate_http_url

            ok, reason = validate_http_url(url)
            if not ok:
                logger.warning("youtube download blocked url reason=%s id=%s", reason, video_id)
                if stream_for_playback:
                    self._release_download_stream_source(initial_stream_key)
                return None
        except Exception:
            pass
        # Max duration gate (when known on media later callers still check)
        try:
            max_sec = int(getattr(config, "MAX_MEDIA_DURATION_SEC", 3600) or 3600)
            if max_sec > 0:
                pass  # enforced at play entry when duration known
        except Exception:
            pass

        filename = self.get_download_filename(
            video_id,
            video=video,
            quality_tier=quality_tier,
        )
        stream_key = self._download_stream_key(
            video_id, video=video, quality_tier=quality_tier
        )
        if stream_for_playback:
            self._download_stream_events.setdefault(stream_key, asyncio.Event())

        # Never treat partial yt-dlp output as ready.
        # Also reuse CDN READY without re-downloading.
        ready = self._local_ready_path(video_id, video=video, quality_tier=quality_tier)
        if ready:
            self._record_local_cache_asset(
                progress_media,
                ready,
                video=video,
                quality_tier=quality_tier,
            )
            await self.render_completed_download_progress(
                ready,
                progress_message=progress_message,
                progress_lang=progress_lang,
                progress_media=progress_media,
            )
            if stream_for_playback:
                self._release_download_stream_source(stream_key)
            return ready
        if self.is_complete_media_file(filename, min_bytes=(512 * 1024 if video else 64 * 1024)):
            self._record_local_cache_asset(
                progress_media,
                filename,
                video=video,
                quality_tier=quality_tier,
            )
            await self.render_completed_download_progress(
                filename,
                progress_message=progress_message,
                progress_lang=progress_lang,
                progress_media=progress_media,
            )
            if stream_for_playback:
                self._release_download_stream_source(stream_key)
            return filename
        if self.is_permanently_unavailable(video_id):
            logger.info(
                "youtube permanent cache skip video_id=%s video=%s",
                video_id,
                video,
            )
            if stream_for_playback:
                self._release_download_stream_source(stream_key)
            return None
        if self.auth_challenge_for(video_id) and not self.cookie_free_mode():
            skip_key = ("download", video_id, bool(video))
            if skip_key not in self._auth_circuit_skip_logged:
                self._auth_circuit_skip_logged.add(skip_key)
                logger.info(
                    "youtube auth challenge circuit skip video_id=%s video=%s",
                    video_id,
                    video,
                )
            else:
                logger.debug(
                    "youtube auth challenge circuit repeat-skip video_id=%s video=%s",
                    video_id,
                    video,
                )
            if stream_for_playback:
                self._release_download_stream_source(stream_key)
            return None
        if self.auth_challenge_for(video_id) and self.cookie_free_mode():
            logger.info(
                "youtube auth challenge cookie-free retry video_id=%s video=%s",
                video_id,
                video,
            )
            # Don't skip — cookie-free mode rotates clients on retry
        # Stale empty/partial leftovers block retries if we only check exists().
        try:
            p = Path(filename)
            if p.exists() and not self.is_complete_media_file(filename, min_bytes=1):
                p.unlink(missing_ok=True)
        except Exception:
            pass

        inflight_bucket = self._inflight_downloads.setdefault(video_id, {})
        # The physical worker is media-scoped.  The first owner freezes its
        # selected tier and every concurrent request joins that worker.
        inflight_key = (bool(video), None)
        existing = inflight_bucket.get(inflight_key)
        if existing and not existing.done():
            # This worker may be owned by a background prefetch. A foreground
            # /play joining it must not inherit background admission, or the
            # background pause meant to protect playback would stall playback.
            # Promotion only reprioritises queued permits; nothing is cancelled.
            dynamic_capacity.promote_if_foreground(existing)
            if message_id and (
                progress_message is None
                or not utils.is_download_progress_closed(progress_message)
            ):
                self._active_tasks[message_id] = existing
                if progress_message is not None:
                    self._download_watchers.setdefault(existing, {})[message_id] = {
                        "message": progress_message,
                        "lang": progress_lang or getattr(progress_message, "lang", {}) or {},
                        "throttle": max(1.0, float(progress_throttle)),
                        "media": progress_media,
                    }
            logger.info(
                "Waiting for existing download task (video_id=%s, video=%s, quality_tier=%s)",
                video_id,
                video,
                quality_tier,
            )
            # A caller cancelling its status card must detach only that
            # request.  The shared yt-dlp owner continues for other chats and
            # cache waiters, preserving the one-acquisition invariant.
            result = await asyncio.shield(existing)
            if self.is_complete_media_file(
                result or filename, min_bytes=(512 * 1024 if video else 64 * 1024)
            ):
                resolved = result or filename
                self._record_local_cache_asset(
                    progress_media,
                    resolved,
                    video=video,
                    quality_tier=quality_tier,
                )
                return resolved
            return None

        # Backpressure: do not start a new heavy job when load is high.
        if not resource_manager.allow_new_heavy_job(video=video):
            logger.warning(
                "download deferred by resource manager video_id=%s video=%s band=%s",
                video_id,
                video,
                resource_manager.snapshot().band,
            )
            if stream_for_playback:
                self._release_download_stream_source(stream_key)
            return None

        try:
            base_opts = self.build_ytdlp_api_opts(
                action="download",
                video_id=video_id,
            )
        except YouTubeRuntimeConfigError:
            if stream_for_playback:
                self._release_download_stream_source(stream_key)
            return None
        base_opts.update({
            "outtmpl": (
                filename.rsplit(".", 1)[0] + ".%(ext)s"
                if video
                else f"downloads/{video_id}.%(ext)s"
            ),
            "overwrites": False,
        })
        po_token_provider = None
        try:
            from AnonX_3.core.provider.po_token import (
                po_token_provider as configured_po_provider,
            )

            if configured_po_provider.enabled():
                po_token_provider = configured_po_provider
        except Exception as ex:
            logger.debug("po_token provider unavailable (download): %s", ex)

        if stream_for_playback:
            # The initial playback owner downloads one progressive source
            # usable by both VC startup and the durable local cache. Its
            # progress hook publishes that exact source, so this path never
            # needs a companion extract_info request.
            ydl_opts = {
                **base_opts,
                "format": self._direct_stream_format(
                    bool(video), quality_tier=quality_tier
                ),
            }
            if video:
                ydl_opts["merge_output_format"] = "mp4"
                caps = resolve_video_caps(quality_tier)
                max_height = int(caps["height"])
                max_width = int(caps["width"])
                max_fps = int(caps["fps"])
            else:
                max_height = max_width = max_fps = None
        elif video:
            quality_mode = (config.VIDEO_QUALITY or "").lower()
            caps = resolve_video_caps(quality_tier)
            max_height = int(caps["height"])
            max_width = int(caps["width"])
            max_fps = int(caps["fps"])
            if quality_mode in {"auto", ""}:
                fallback_mid_fps = max(15, min(max_fps, 30))
                fallback_low_fps = max(15, min(max_fps, 24))
                # Auto ladder: keep normal quality on good lines and gracefully
                # fallback to lower tiers when higher variants are unavailable/slow.
                #
                # We explicitly filter to mp4 for the initial tiers because PyTgCalls
                # currently only supports H.264/AVC streams.  However, for the final
                # fallback we allow any container since yt-dlp will merge the streams
                # into an mp4 container via ffmpeg.
                format_expr = (
                    f"(bestvideo[height<=?{max_height}][width<=?{max_width}][fps<=?{max_fps}][ext=mp4]+bestaudio)"
                    f"/(bestvideo[height<=?480][fps<=?{fallback_mid_fps}][ext=mp4]+bestaudio)"
                    f"/(bestvideo[height<=?360][fps<=?{fallback_low_fps}][ext=mp4]+bestaudio)"
                    f"/(bestvideo[height<=?{max_height}][width<=?{max_width}][fps<=?{max_fps}]+bestaudio)"
                )
            else:
                # Manual quality mode: use the configured height/width/fps but avoid overly
                # restrictive container filters.  Many videos no longer ship a 360p MP4
                # variant and would silently fall back to an audio-only stream if we
                # insisted on `[ext=mp4]`.  Prefer AVC/H.264 (+ AAC when available).
                # For mobile stability, strict AVC mode avoids fallback to VP9/AV1.
                if config.VIDEO_STRICT_AVC:
                    format_expr = (
                        f"(bestvideo[vcodec~='^(avc1|h264)'][height<=?{max_height}]"
                        f"[width<=?{max_width}][fps<=?{max_fps}]"
                        f"+bestaudio[acodec~='^(mp4a|aac)'])"
                        f"/(bestvideo[vcodec~='^(avc1|h264)'][height<=?{max_height}]"
                        f"[width<=?{max_width}][fps<=?{max_fps}]+bestaudio)"
                        f"/(best[ext=mp4][vcodec~='^(avc1|h264)'])"
                    )
                else:
                    format_expr = (
                        f"(bestvideo[vcodec~='^(avc1|h264)'][height<=?{max_height}]"
                        f"[width<=?{max_width}][fps<=?{max_fps}]"
                        f"+bestaudio[acodec~='^(mp4a|aac)'])"
                        f"/(bestvideo[vcodec~='^(avc1|h264)'][height<=?{max_height}]"
                        f"[width<=?{max_width}][fps<=?{max_fps}]+bestaudio)"
                        f"/(bestvideo[height<=?{max_height}][width<=?{max_width}]"
                        f"[fps<=?{max_fps}]+bestaudio)"
                        f"/(bestvideo[height<=?{max_height}][width<=?{max_width}]"
                        f"[fps<=?{max_fps}])"
                    )
            ydl_opts = {
                **base_opts,
                "format": format_expr,
                "merge_output_format": "mp4",
            }
        else:
            # Prefer m4a/AAC first: ntgcalls often fails "No audio source" on
            # plain webm/opus, and progressive m4a usually finishes faster than
            # remux paths — critical for <10s /play cold starts.
            #
            ydl_opts = {
                **base_opts,
                "format": (
                    "bestaudio[ext=m4a][acodec!=none]"
                    "/bestaudio[acodec^=mp4a]"
                    "/bestaudio[ext=webm][acodec=opus]"
                    "/bestaudio[ext=webm]"
                    "/bestaudio"
                    "/best[acodec!=none]"
                ),
            }

        def _resolve_downloaded_path(expected: str) -> str | None:
            if Path(expected).exists():
                return expected
            candidates = sorted(
                Path("downloads").glob(f"{video_id}.*"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not candidates:
                return None
            return str(candidates[0]).replace("\\", "/")

        def _recovery_client_opts(opts: dict, recovery_round: int) -> dict:
            """Return configured-cookie options using a distinct client set."""
            recovered = dict(opts)
            extractor_args = dict(recovered.get("extractor_args") or {})
            youtube_args = dict(extractor_args.get("youtube") or {})
            if recovery_round <= 1:
                youtube_args["player_client"] = [
                    "android",
                    "ios",
                    "web_safari",
                    "-android_vr",
                    "-android_sdkless",
                ]
            else:
                youtube_args["player_client"] = [
                    "android",
                    "ios",
                    "tv",
                    "web_embedded",
                    "-android_vr",
                    "-android_sdkless",
                ]
            extractor_args["youtube"] = youtube_args
            recovered["extractor_args"] = extractor_args
            return recovered

        def _permissive_download_format() -> str:
            """Return the least restrictive playable selector for this request."""
            if video:
                return (
                    "best[ext=mp4][acodec!=none][vcodec!=none]"
                    "/best[acodec!=none][vcodec!=none]"
                    "/bestvideo[height<=?720][vcodec!=none]+bestaudio[acodec!=none]"
                )
            return (
                "bestaudio[acodec!=none]"
                "/bestaudio"
                "/best[acodec!=none]"
            )

        def _purge_partial_outputs() -> None:
            """Remove only this media's incomplete yt-dlp artifacts."""
            download_root = Path("downloads").resolve()
            for candidate in download_root.glob(f"{video_id}*"):
                try:
                    resolved = candidate.resolve()
                    if resolved.parent != download_root:
                        continue
                    name = candidate.name.lower()
                    if ".part" in name or name.endswith(".ytdl"):
                        candidate.unlink(missing_ok=True)
                except Exception:
                    continue

        async def _render_progress_for_watchers(task, data: dict) -> None:
            watchers = self._download_watchers.get(task) or {}
            if not watchers:
                return
            status = str(data.get("status", "")).lower()
            if status not in {"downloading", "finished"}:
                return

            previous = self._download_progress_state.get(task) or {}
            downloaded = max(
                int(previous.get("downloaded") or 0),
                int(
                data.get("downloaded_bytes")
                or 0
                ),
            )
            total = max(
                int(previous.get("total") or 0),
                int(
                data.get("total_bytes")
                or data.get("total_bytes_estimate")
                or 0
                ),
            )
            speed = float(data.get("speed") or previous.get("speed") or 0.0)
            raw_percent = str(data.get("_percent_str") or "")
            percent_match = re.search(r"(\d+(?:\.\d+)?)\s*%", raw_percent)
            if total <= 0 and downloaded > 0 and percent_match:
                try:
                    percent_hint = float(percent_match.group(1))
                    if percent_hint > 0:
                        total = max(downloaded, round(downloaded * 100 / percent_hint))
                except (TypeError, ValueError):
                    pass
            eta_raw = data.get("eta")
            if eta_raw is None:
                eta_raw = previous.get("eta") or 0
            try:
                eta = max(0, int(float(eta_raw)))
            except Exception:
                eta = 0
            if status == "finished":
                total = max(total, downloaded)
                if total <= 0:
                    path_hint = (
                        data.get("filename")
                        or data.get("tmpfilename")
                        or (data.get("info_dict") or {}).get("filepath")
                    )
                    try:
                        total = downloaded = Path(str(path_hint)).stat().st_size
                    except (OSError, TypeError, ValueError):
                        pass
                if total > 0:
                    downloaded = total
                eta = 0
            self._download_progress_state[task] = {
                "downloaded": downloaded,
                "total": total,
                "speed": speed,
                "eta": eta,
            }
            percent = (
                100.0
                if status == "finished"
                else min(100.0, (downloaded * 100.0 / total))
                if total > 0
                else 0.0
            )

            for msg_id, ctx in list(watchers.items()):
                msg = ctx.get("message")
                lang_map = ctx.get("lang") or {}
                last_percent = float(ctx.get("last_percent", -1.0))
                if status == "downloading" and percent <= last_percent:
                    continue

                try:
                    edited = await self._edit_download_progress_card(
                        progress_message=msg,
                        progress_lang=lang_map,
                        progress_media=ctx.get("media"),
                        current=downloaded,
                        total=total,
                        speed=speed,
                        eta_seconds=eta,
                    )
                    if edited is None:
                        watchers.pop(msg_id, None)
                        if self._active_tasks.get(msg_id) is task:
                            self._active_tasks.pop(msg_id, None)
                        continue
                    ctx["last_percent"] = percent
                except Exception as ex:
                    if not ctx.get("edit_error_logged"):
                        ctx["edit_error_logged"] = True
                        logger.warning(
                            "download progress edit failed message_id=%s error=%s",
                            msg_id,
                            type(ex).__name__,
                        )
                    continue

        def _download(opts):
            with create_youtube_dl(opts, yt_dlp.YoutubeDL) as ydl:
                try:
                    ydl.download([url])
                except (yt_dlp.utils.DownloadError, yt_dlp.utils.ExtractorError) as ex:
                    raise RuntimeError(str(ex)) from ex
                except Exception as ex:
                    logger.warning("Download failed: %s", ex)
                    gc.collect()
                    raise
            return _resolve_downloaded_path(filename)

        loop = asyncio.get_running_loop()
        progress_signal = asyncio.Event()
        latest_progress: dict | None = None
        progress_pump_closed = False

        def _progress_hook(data):
            try:
                payload = dict(data or {})

                def _publish() -> None:
                    nonlocal latest_progress
                    if stream_for_playback:
                        self._publish_download_stream_source(
                            video_id,
                            video=video,
                            quality_tier=quality_tier,
                            info=payload.get("info_dict"),
                            local_path=filename,
                        )
                    if progress_pump_closed:
                        return
                    if (
                        latest_progress
                        and str(latest_progress.get("status", "")).lower() == "finished"
                        and str(payload.get("status", "")).lower() != "finished"
                    ):
                        return
                    latest_progress = payload
                    progress_signal.set()

                loop.call_soon_threadsafe(_publish)
            except Exception:
                return

        async def _progress_pump() -> None:
            """Coalesce worker-thread hooks and serialize Telegram edits."""
            nonlocal latest_progress
            while True:
                await progress_signal.wait()
                progress_signal.clear()
                payload = latest_progress
                latest_progress = None
                if payload is not None:
                    await _render_progress_for_watchers(task, payload)
                if progress_pump_closed and latest_progress is None:
                    return

        async def _close_progress_pump(*, cancel: bool = False) -> None:
            nonlocal progress_pump_closed
            if progress_pump_task.done():
                return
            if cancel:
                progress_pump_closed = True
                progress_pump_task.cancel()
            else:
                # Yield once so every hook already published by the worker
                # reaches the coalescer before it is closed.
                await asyncio.sleep(0)
                progress_pump_closed = True
                progress_signal.set()
            try:
                await progress_pump_task
            except asyncio.CancelledError:
                pass

        async def _execute_download_strategy(opts: dict) -> str | None:
            """Run one yt-dlp strategy under the shared resource limits."""
            sem = resource_manager.download_semaphore()
            if video:
                async with resource_manager.video_semaphore():
                    async with sem:
                        return await asyncio.to_thread(_download, opts)
            async with sem:
                return await asyncio.to_thread(_download, opts)

        async def _run_download():
            # First playback owns one physical yt-dlp operation. Recovery
            # ladders remain available to non-interactive prefetch/retry work,
            # but must not turn one cold /play into several extractor calls.
            max_attempts = (
                1
                if one_shot
                else max(1, int(getattr(config, "YTDLP_MAX_RETRIES", 3) or 3))
            )
            delays = backoff_delays(max_attempts)
            last_classified = None
            try:
                resource_manager.note_download(+1)
                if video:
                    resource_manager.note_video_job(+1)

                for attempt in range(max_attempts):
                    # READY may appear from parallel CDN path
                    ready_now = self._local_ready_path(
                        video_id, video=video, quality_tier=quality_tier
                    )
                    if ready_now:
                        return ready_now

                    if delays[attempt] > 0:
                        await asyncio.sleep(delays[attempt])

                    attempt_opts = dict(ydl_opts)
                    recovering_403 = bool(
                        last_classified
                        and last_classified.cls == ErrorClass.CLIENT_PO
                    )
                    # The official provider framework owns token binding and
                    # bypass-cache decisions. A later 403 rotates to materially
                    # different clients instead of repeating the failed context.
                    if po_token_provider is not None and attempt < 2:
                        attempt_opts = await po_token_provider.apply_to_ydl_opts(
                            attempt_opts,
                            video_id=video_id,
                        )
                    if recovering_403 and (
                        po_token_provider is None or attempt >= 2
                    ):
                        recovery_round = (
                            attempt
                            if po_token_provider is None
                            else max(1, attempt - 1)
                        )
                        attempt_opts = _recovery_client_opts(
                            attempt_opts, recovery_round=recovery_round
                        )

                    strategies: list[dict] = [attempt_opts]
                    if one_shot:
                        # No selector/cookie/client fallback in a one-shot
                        # acquisition: each would instantiate yt-dlp again.
                        strategies = [attempt_opts]
                    elif video:
                        permissive_opts = dict(attempt_opts)
                        permissive_opts["format"] = _permissive_download_format()
                        strategies.append(permissive_opts)
                    elif not one_shot:
                        # Audio: always include a permissive fallback so videos
                        # that lack m4a/opus variants don't silently return None.
                        audio_perm = dict(attempt_opts)
                        audio_perm["format"] = _permissive_download_format()
                        strategies.append(audio_perm)
                    if (
                        not one_shot
                        and last_classified
                        and last_classified.cls == ErrorClass.FORMAT
                    ):
                        strategies = list(reversed(strategies))

                    for strat in strategies:
                        try:
                            result = await _execute_download_strategy(strat)
                            if result and self.is_complete_media_file(
                                result,
                                min_bytes=(512 * 1024 if video else 64 * 1024),
                            ):
                                self._clear_auth_challenge()
                                return result
                        except Exception as ex:
                            last_classified = classify_error(ex)
                            logger.warning(
                                "download fail video_id=%s attempt=%s/%s class=%s msg=%s",
                                video_id,
                                attempt + 1,
                                max_attempts,
                                last_classified.name,
                                last_classified.message[:160],
                            )
                            if one_shot:
                                return None
                            desc = str(ex).lower()
                            if "too many open files" in desc:
                                return None
                            if (
                                last_classified.cls
                                == ErrorClass.AUTH_CHALLENGE
                            ):
                                _purge_partial_outputs()
                                try:
                                    from AnonX_3.core.metrics import metrics

                                    metrics.inc("youtube_auth_challenge")
                                except Exception:
                                    pass

                                logger.warning(
                                    "youtube_authenticated_runtime_failed=True "
                                    "action=download video_id=%s class=%s msg=%s",
                                    video_id,
                                    last_classified.name,
                                    last_classified.message[:160],
                                )
                                self._remember_auth_challenge(
                                    last_classified.message,
                                    video_id=video_id,
                                )
                                return None
                            if last_classified.cls in {
                                ErrorClass.PERMANENT,
                                ErrorClass.REGION,
                            }:
                                self._remember_permanent_failure(
                                    video_id, last_classified.message
                                )
                            if (
                                last_classified.cls == ErrorClass.CLIENT_PO
                            ):
                                _purge_partial_outputs()
                                try:
                                    from AnonX_3.core.metrics import metrics

                                    metrics.inc("youtube_403_recovery")
                                except Exception:
                                    pass
                                logger.info(
                                    "youtube 403 recovery scheduled video_id=%s next=%s",
                                    video_id,
                                    (
                                        "fresh_video_bound_po"
                                        if po_token_provider is not None
                                        and attempt + 1 < 2
                                        else "cookie_free_client_rotation"
                                    ),
                                )
                            if last_classified.cls == ErrorClass.FORMAT:
                                # Try the next materially different format
                                # selector before consuming an outer retry.
                                continue
                            if not should_retry(
                                last_classified, attempt + 1, max_attempts
                            ):
                                return None
                            break  # next attempt with backoff
                    else:
                        # All strategies returned None without exception
                        last_classified = classify_error("download returned no file")
                        if one_shot:
                            return None
                        if not should_retry(last_classified, attempt + 1, max_attempts):
                            return None
                return None
            except Exception as ex:
                logger.warning("Download failed: %s", ex)
                return None
            finally:
                resource_manager.note_download(-1)
                if video:
                    resource_manager.note_video_job(-1)

        ydl_opts["progress_hooks"] = [_progress_hook]

        task = asyncio.create_task(_run_download())
        if message_id and (
            progress_message is None
            or not utils.is_download_progress_closed(progress_message)
        ):
            self._active_tasks[message_id] = task
            if progress_message is not None:
                self._download_watchers.setdefault(task, {})[message_id] = {
                    "message": progress_message,
                    "lang": progress_lang or getattr(progress_message, "lang", {}) or {},
                    "media": progress_media,
                }
        inflight_bucket[inflight_key] = task
        progress_pump_task = asyncio.create_task(
            _progress_pump(),
            name=f"youtube-progress:{video_id}",
        )
        logger.info(
            "Starting download video_id=%s video=%s quality_tier=%s max_height=%s max_width=%s max_fps=%s",
            video_id,
            video,
            quality_tier,
            max_height if video else "audio",
            max_width if video else "audio",
            max_fps if video else "audio",
        )
        cleanup_started = False

        async def _finalize_owner() -> None:
            """Release only state owned by this completed yt-dlp task."""
            nonlocal cleanup_started
            if cleanup_started:
                return
            cleanup_started = True
            await _close_progress_pump(cancel=task.cancelled())
            if message_id and self._active_tasks.get(message_id) is task:
                self._active_tasks.pop(message_id, None)
            watchers = self._download_watchers.pop(task, {})
            for watcher_id in watchers:
                if self._active_tasks.get(watcher_id) is task:
                    self._active_tasks.pop(watcher_id, None)
            self._download_progress_state.pop(task, None)
            bucket = self._inflight_downloads.get(video_id)
            owns_inflight = bucket is None or bucket.get(inflight_key) is task
            # A replacement owner can only be created after this task is done.
            # Do not let an old cancellation callback erase its source event.
            if owns_inflight:
                self._release_download_stream_source(stream_key)
            if bucket is not None and bucket.get(inflight_key) is task:
                bucket.pop(inflight_key, None)
                if not bucket:
                    self._inflight_downloads.pop(video_id, None)

        def _finalize_later(_completed: asyncio.Task) -> None:
            if cleanup_started:
                return
            try:
                asyncio.create_task(
                    _finalize_owner(),
                    name=f"youtube-cleanup:{video_id}",
                )
            except RuntimeError:
                # Interpreter shutdown: the task is already complete and no
                # new playback request can join it.
                pass

        try:
            # Shield the shared worker: cancellation of one Telegram request
            # must not cancel the physical yt-dlp acquisition for its joiners.
            result = await asyncio.shield(task)
            if result is not None:
                # Synchronous yt-dlp completion normally means post-processing
                # has settled. Skip the old fixed two-poll delay for a complete
                # file; retain the defensive wait only for edge cases.
                min_bytes = 128 * 1024 if video else 16 * 1024
                settled = (
                    result
                    if self.is_complete_media_file(result, min_bytes=min_bytes)
                    else await self.wait_media_file_ready(
                        result,
                        video=video,
                        timeout=90.0 if video else 30.0,
                    )
                )
                if settled is None:
                    # A parallel CDN publish can atomically move the completed
                    # download from downloads/ into media/ready while another
                    # waiter still holds the original path.
                    settled = self._local_ready_path(
                        video_id,
                        video=video,
                        quality_tier=quality_tier,
                    )
                    if settled is None:
                        logger.warning(
                            "Download finished but file not ready video_id=%s path=%s",
                            video_id,
                            result,
                        )
                        return None
                result = settled
                self._record_local_cache_asset(
                    progress_media,
                    result,
                    video=video,
                    quality_tier=quality_tier,
                )
                await _close_progress_pump()
                await _render_progress_for_watchers(
                    task,
                    {"status": "finished", "filename": result},
                )
            return result
        except asyncio.CancelledError:
            if not task.done():
                task.add_done_callback(_finalize_later)
            raise
        finally:
            if task.done():
                await _finalize_owner()

    def attach_download_watcher(
        self,
        video_id: str,
        *,
        progress_message,
        progress_lang: dict | None = None,
        progress_throttle: float = 1.0,
        progress_media: Media | Track | None = None,
    ) -> bool:
        """Attach UI progress to the real yt-dlp task, not its prefetch wrapper."""
        message_id = getattr(progress_message, "id", None)
        if message_id is None or utils.is_download_progress_closed(progress_message):
            return False
        bucket = self._inflight_downloads.get(str(video_id)) or {}
        for download_task in bucket.values():
            if download_task.done():
                continue
            self._active_tasks[message_id] = download_task
            self._download_watchers.setdefault(download_task, {})[message_id] = {
                "message": progress_message,
                "lang": progress_lang
                or getattr(progress_message, "lang", {})
                or {},
                "media": progress_media,
            }
            return True
        return False

    def detach_download_progress(self, progress_message) -> bool:
        """Detach one status card without cancelling its background download."""
        key = utils.download_progress_key(progress_message)
        if key is None:
            return False
        message_id = key[1]
        detached = False
        for task, watchers in list(self._download_watchers.items()):
            ctx = watchers.get(message_id)
            if ctx is None or utils.download_progress_key(
                ctx.get("message")
            ) != key:
                continue
            watchers.pop(message_id, None)
            if not watchers and self._download_watchers.get(task) is watchers:
                self._download_watchers.pop(task, None)
            if self._active_tasks.get(message_id) is task:
                self._active_tasks.pop(message_id, None)
            detached = True
        return detached

    async def cancel(self, message_id: int) -> bool:
        """Cancel a resolver, or detach one card from a shared yt-dlp owner.

        ``_active_tasks`` intentionally points at the physical owner so a
        later request can attach to its progress.  Cancelling that task from a
        Telegram button would break every joiner and allow a second extractor
        to start while the worker thread is still unwinding.  Resolver/search
        tasks are not download owners and remain cancellable.
        """
        task = self._active_tasks.pop(message_id, None)
        detached = task is not None
        is_download_owner = bool(
            task
            and (
                task in self._download_watchers
                or any(
                    owner is task
                    for bucket in self._inflight_downloads.values()
                    for owner in bucket.values()
                )
            )
        )
        for watcher_task, watchers in list(self._download_watchers.items()):
            if message_id not in watchers:
                continue
            watchers.pop(message_id, None)
            if not watchers and self._download_watchers.get(watcher_task) is watchers:
                self._download_watchers.pop(watcher_task, None)
            detached = True
        if task and not task.done() and not is_download_owner:
            task.cancel()
        return detached
