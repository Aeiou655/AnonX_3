# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""Optional lightweight health + metrics HTTP server (admin/loopback)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from AnonX_3 import config, logger
from AnonX_3.core.metrics import metrics


async def collect_health() -> dict[str, Any]:
    """Aggregate component health without failing hard."""
    components: dict[str, Any] = {}
    overall = "ok"

    # Bot / process
    components["bot"] = {"status": "ok", "uptime_sec": metrics.snapshot()["uptime_sec"]}

    # Mongo
    try:
        from AnonX_3 import db

        ok = bool(getattr(db, "mongo", None) or getattr(db, "client", None))
        # lightweight: if connected flag exists
        connected = getattr(db, "is_connected", None)
        if callable(connected):
            ok = bool(await connected()) if asyncio.iscoroutinefunction(connected) else bool(connected())
        components["mongo"] = {"status": "ok" if ok else "degraded"}
        if not ok:
            overall = "degraded"
    except Exception as ex:
        components["mongo"] = {"status": "error", "error": str(ex)[:120]}
        overall = "degraded"

    # CDN / origin
    try:
        from AnonX_3.core.cdn.manager import cdn

        components["cdn"] = {
            "status": "ok" if cdn.enabled else "disabled",
            "root": str(cdn.media_root()) if cdn.enabled else None,
            "public_base": (getattr(config, "CDN_PUBLIC_BASE_URL", "") or "")
            or (getattr(config, "CDN_ORIGIN_PUBLIC_BASE", "") or "")
            or None,
        }
    except Exception as ex:
        components["cdn"] = {"status": "error", "error": str(ex)[:120]}

    # Resource manager
    try:
        from AnonX_3.core.resource_manager import resource_manager

        snap = resource_manager.snapshot()
        stats = resource_manager.stats()
        dyn = stats.get("dynamic") or {}
        lanes = dyn.get("lanes") or {}
        components["resources"] = {
            "status": "ok",
            "band": snap.band,
            "cpu": snap.cpu_percent,
            "ram": snap.ram_percent,
            "disk_pct": resource_manager.disk_usage_pct(),
            "limits": stats.get("limits"),
        }
        # Dynamic Resource Control view: current capacity, active jobs, waiting
        # jobs, CPU, RAM and event-loop lag — the signals an operator needs to
        # tell "busy" apart from "about to stutter".
        components["dynamic_capacity"] = {
            "status": "degraded" if dyn.get("degraded") else "ok",
            "mode": dyn.get("mode", "fixed"),
            "degraded": bool(dyn.get("degraded")),
            "degraded_reason": dyn.get("degraded_reason"),
            "reason": dyn.get("reason"),
            "pressure": dyn.get("pressure"),
            "demand": dyn.get("demand"),
            "cpu_percent": dyn.get("cpu_percent", snap.cpu_percent),
            "ram_percent": dyn.get("ram_percent", snap.ram_percent),
            "load_per_core": dyn.get("load_per_core"),
            "event_loop_lag_ms": dyn.get(
                "event_loop_lag_ms", resource_manager.event_loop_lag_ms()
            ),
            "foreground_waiting": dyn.get("foreground_waiting", 0),
            "background_paused": bool(dyn.get("background_paused")),
            # Stream scaling: MAX_ACTIVE_STREAMS is the *baseline*, not the cap.
            # Shows baseline, live capacity, the runtime-derived safe ceiling,
            # the machine hard ceiling, active streams and why it sits there.
            "streams": resource_manager.stream_scaling(),
            "capacity": {k: v.get("capacity") for k, v in lanes.items()},
            "active_jobs": {k: v.get("active") for k, v in lanes.items()},
            "waiting_jobs": {k: v.get("waiting") for k, v in lanes.items()},
            "waiting_foreground": {
                k: v.get("waiting_foreground") for k, v in lanes.items()
            },
            "waiting_background": {
                k: v.get("waiting_background") for k, v in lanes.items()
            },
            "fixed_limits": stats.get("fixed_limits"),
            "lanes": lanes,
            "counters": dyn.get("counters"),
        }
        if dyn.get("degraded"):
            overall = "degraded" if overall == "ok" else overall
        if snap.band == "high":
            overall = "degraded" if overall == "ok" else overall
    except Exception as ex:
        components["resources"] = {"status": "error", "error": str(ex)[:120]}
        components["dynamic_capacity"] = {"status": "error", "error": str(ex)[:120]}

    # Singleflight / Redis
    try:
        from AnonX_3.core.downloader.singleflight import singleflight

        components["singleflight"] = {
            "status": "ok",
            "backend": getattr(config, "SINGLEFLIGHT_BACKEND", "memory"),
            **singleflight.stats(),
        }
    except Exception as ex:
        components["singleflight"] = {"status": "error", "error": str(ex)[:120]}

    # PO token provider
    try:
        from AnonX_3.core.provider.po_token import po_token_provider

        configured = po_token_provider.enabled()
        plugin_available = po_token_provider.plugin_available()
        components["po_token"] = {
            "status": (
                "ok"
                if configured and plugin_available
                else "error"
                if configured
                else "disabled"
            ),
            "enabled": configured,
            "plugin_available": plugin_available,
        }
    except Exception as ex:
        components["po_token"] = {"status": "error", "error": str(ex)[:120]}

    # Fallback
    components["fallback"] = {
        "status": "ok" if getattr(config, "FALLBACK_SOUNDCLOUD", True) else "disabled",
        "soundcloud": bool(getattr(config, "FALLBACK_SOUNDCLOUD", True)),
    }

    # Cleanup worker is a loop — report as configured
    components["cleanup"] = {
        "status": "ok" if getattr(config, "CDN_ENABLED", False) else "disabled",
        "gc_interval_sec": getattr(config, "CDN_GC_INTERVAL_SEC", None),
    }

    return {
        "status": overall,
        "ts": time.time(),
        "components": components,
        "metrics": metrics.snapshot(),
    }


async def start_health_server() -> asyncio.Task | None:
    """Start health HTTP server if HEALTH_PORT > 0."""
    port = int(getattr(config, "HEALTH_PORT", 0) or 0)
    if port <= 0:
        return None
    try:
        from aiohttp import web
    except ImportError:
        logger.warning("HEALTH_PORT set but aiohttp unavailable")
        return None

    host = (getattr(config, "HEALTH_HOST", "127.0.0.1") or "127.0.0.1").strip()
    token = (getattr(config, "HEALTH_TOKEN", "") or "").strip()

    def _authorized(request) -> bool:
        if not token:
            return True
        hdr = request.headers.get("Authorization") or ""
        if hdr == f"Bearer {token}":
            return True
        if request.query.get("token") == token:
            return True
        return False

    async def health_handler(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        data = await collect_health()
        code = 200 if data.get("status") == "ok" else 503
        return web.json_response(data, status=code)

    async def metrics_handler(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        accept = request.headers.get("Accept", "")
        if "text/plain" in accept or request.query.get("format") == "prom":
            return web.Response(
                text=metrics.prometheus_text(),
                content_type="text/plain; version=0.0.4",
            )
        return web.json_response(metrics.snapshot())

    async def ready_handler(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"ready": True})

    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_get("/metrics", metrics_handler)
    app.router.add_get("/ready", ready_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    logger.info("Health server listening on http://%s:%s (/health /metrics /ready)", host, port)

    async def _keepalive():
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await runner.cleanup()
            raise

    return asyncio.create_task(_keepalive(), name="health_server")
