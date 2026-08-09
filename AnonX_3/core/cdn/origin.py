# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""Optional built-in static origin for ready/ (no Nginx required for lab/VPS)."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

from AnonX_3 import config, logger
from AnonX_3.core.cdn.manager import cdn


def _local_ip() -> str:
    """Detect primary local IP without external HTTP calls."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except Exception:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "127.0.0.1"


async def start_cdn_origin() -> asyncio.Task | None:
    """Start built-in origin: auto free port + auto local IP (zero config)."""
    if not getattr(config, "CDN_ENABLED", False):
        return None
    if not getattr(config, "CDN_ORIGIN_ENABLED", False):
        return None

    try:
        from aiohttp import web
    except ImportError:
        logger.warning("CDN origin requested but aiohttp is unavailable")
        return None

    ready = cdn.ready_dir()
    host = getattr(config, "CDN_ORIGIN_HOST", "0.0.0.0") or "0.0.0.0"
    # 0 → OS assigns an unused port (auto detect)
    port = int(getattr(config, "CDN_ORIGIN_PORT", 0) or 0)
    prefix = getattr(config, "CDN_URL_PREFIX", "/media") or "/media"
    if not prefix.startswith("/"):
        prefix = f"/{prefix}"
    prefix = prefix.rstrip("/") or "/media"

    app = web.Application()
    app.router.add_static(prefix + "/", path=str(ready), show_index=False, follow_symlinks=False)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()

    # Resolve bound port when 0 (auto).
    sockets = []
    try:
        sockets = list(site._server.sockets)  # type: ignore[attr-defined]
    except Exception:
        sockets = []
    bound_port = port
    if sockets:
        try:
            bound_port = int(sockets[0].getsockname()[1])
        except Exception:
            bound_port = port

    public_ip = _local_ip()  # local interface IP, no external HTTP
    public_base = f"http://{public_ip}:{bound_port}"
    # Always record runtime base; used when CDN_PUBLIC_BASE_URL is empty.
    config.CDN_ORIGIN_PUBLIC_BASE = public_base
    if not (getattr(config, "CDN_PUBLIC_BASE_URL", "") or "").strip():
        # Zero-config play URL base for hybrid/cdn modes.
        logger.info(
            "CDN zero-config: IP=%s PORT=%s (auto) base=%s",
            public_ip,
            bound_port,
            public_base,
        )
    logger.info(
        "CDN origin serving ready/ at %s%s/ (bind=%s:%s root=%s)",
        public_base,
        prefix,
        host,
        bound_port,
        ready,
    )

    async def _keep_alive() -> None:
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await runner.cleanup()
            raise

    return asyncio.create_task(_keep_alive(), name="cdn_origin")
