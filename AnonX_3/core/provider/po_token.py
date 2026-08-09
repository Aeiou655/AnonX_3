# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""Configure yt-dlp's official PO-token provider framework."""

from __future__ import annotations

import importlib.util
import time

from AnonX_3 import config, logger


class PoTokenProvider:
    PROVIDER_KEY = "youtubepot-bgutilhttp"

    #: A dead sidecar must not stall playback, so the probe is short and its
    #: verdict is cached. Failures re-probe sooner than successes so a restarted
    #: provider is picked up quickly.
    _HEALTH_TIMEOUT_SEC = 2.0
    _HEALTH_TTL_OK_SEC = 120.0
    _HEALTH_TTL_FAIL_SEC = 15.0

    def __init__(self) -> None:
        self._missing_plugin_logged = False
        self._health: tuple[float, bool] | None = None
        self._last_health_state: bool | None = None

    def base_url(self) -> str:
        return (
            (
                getattr(config, "POT_PROVIDER_URL", "")
                or getattr(config, "PO_TOKEN_PROVIDER_URL", "")
                or ""
            )
            .strip()
            .rstrip("/")
        )

    def enabled(self) -> bool:
        if not bool(getattr(config, "PO_TOKEN_PROVIDER_ENABLED", False)):
            return False
        return bool(self.base_url())

    @staticmethod
    def plugin_available() -> bool:
        try:
            return (
                importlib.util.find_spec(
                    "yt_dlp_plugins.extractor.getpot_bgutil_http"
                )
                is not None
            )
        except Exception:
            return False

    def operational(self) -> bool:
        if not self.enabled():
            return False
        if self.plugin_available():
            return True
        if not self._missing_plugin_logged:
            logger.warning(
                "PO-token provider configured but bgutil plugin is unavailable"
            )
            self._missing_plugin_logged = True
        return False

    async def healthy(self, *, force_refresh: bool = False) -> bool:
        """True when the bgutil sidecar answers on its HTTP port.

        Purely diagnostic: callers log the verdict, they never gate extraction
        on it. Without this probe a dead sidecar is only visible as a generic
        yt-dlp timeout, which reads like a YouTube fault rather than a local
        service that needs restarting.
        """
        if not self.enabled():
            return False
        now = time.monotonic()
        if not force_refresh and self._health is not None and self._health[0] > now:
            return self._health[1]
        ok = await self._probe()
        ttl = self._HEALTH_TTL_OK_SEC if ok else self._HEALTH_TTL_FAIL_SEC
        self._health = (now + ttl, ok)
        if ok != self._last_health_state:
            # Only on transition, so a down provider cannot flood the log.
            (logger.info if ok else logger.warning)(
                "po_token provider health=%s url=%s",
                "up" if ok else "down",
                self.base_url() or "unset",
            )
            self._last_health_state = ok
        return ok

    async def _probe(self) -> bool:
        base = self.base_url()
        if not base:
            return False
        try:
            import aiohttp
        except ImportError:
            return False
        timeout = aiohttp.ClientTimeout(total=self._HEALTH_TIMEOUT_SEC)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{base}/ping") as response:
                    return 200 <= response.status < 300
        except Exception as ex:
            logger.debug("po_token health probe failed: %s", type(ex).__name__)
            return False

    def health_hint(self) -> str:
        """Operator-facing remedy for a down sidecar."""
        return (
            "bgutil PO-token provider unreachable at "
            f"{self.base_url() or 'unset'} -- "
            "run: systemctl restart bgutil-pot.service"
        )

    def apply_to_ydl_opts_sync(
        self,
        opts: dict,
        *,
        video_id: str | None = None,
    ) -> dict:
        """Point the bgutil yt-dlp plugin at the long-lived provider sidecar."""
        out = dict(opts)
        if not self.enabled():
            return out

        base_url = self.base_url()
        extractor_args = dict(out.get("extractor_args") or {})
        provider_args = dict(extractor_args.get(self.PROVIDER_KEY) or {})
        provider_args["base_url"] = [base_url]
        extractor_args[self.PROVIDER_KEY] = provider_args
        script_args = dict(extractor_args.get("youtubepot-bgutilscript") or {})
        server_home = (
            getattr(config, "YTDLP_POT_SERVER_HOME", "") or ""
        ).strip()
        if server_home:
            script_args["server_home"] = [server_home]
            extractor_args["youtubepot-bgutilscript"] = script_args

        client = (
            getattr(config, "PO_TOKEN_CLIENT", "mweb") or "mweb"
        ).strip() or "mweb"
        yt_args = dict(extractor_args.get("youtube") or {})
        if not yt_args.get("player_client"):
            yt_args["player_client"] = [client]
        extractor_args["youtube"] = yt_args
        out["extractor_args"] = extractor_args
        logger.debug(
            "PO-token plugin configured client=%s video_id=%s",
            client,
            video_id,
        )
        return out

    async def apply_to_ydl_opts(
        self,
        opts: dict,
        *,
        video_id: str | None = None,
    ) -> dict:
        """Async variant that also reports sidecar health.

        Configuration is applied either way. A down provider is logged with its
        remedy but never stripped: the plugin re-pings at use time and the
        sidecar may well be back by then, so disabling it here would turn a
        transient restart into a silent loss of PO tokens.
        """
        if self.enabled() and not await self.healthy():
            logger.warning("%s (video_id=%s)", self.health_hint(), video_id or "")
        return self.apply_to_ydl_opts_sync(opts, video_id=video_id)


po_token_provider = PoTokenProvider()
