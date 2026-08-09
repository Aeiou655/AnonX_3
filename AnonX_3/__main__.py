# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import asyncio
import ctypes
import logging
import os
import signal
import sys
import time
import traceback
import importlib
from datetime import datetime, timedelta, timezone
from contextlib import suppress
from pathlib import Path

from AnonX_3.core.lifecycle import (
    DUPLICATE_INSTANCE_EXIT_CODE,
    ProcessInstanceAlreadyRunning,
    exec_fresh_process,
    plan_crash_restart,
    resolve_package_name,
    resolve_runtime_identity,
)

# ── Unique process name for pkill targeting ──
_PROCESS_NAME = resolve_package_name(__package__)

def _set_process_title():
    """Set process title so pkill <name> (without -f) targets only this version."""
    title = _PROCESS_NAME.encode()[:15]
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_NAME = 15
        libc.prctl(PR_SET_NAME, title, 0, 0, 0)
    except Exception:
        pass
    try:
        sys.argv[0] = _PROCESS_NAME
    except Exception:
        pass

_set_process_title()

# ── Global unhandled exception hook ──
_original_excepthook = sys.excepthook

def _global_excepthook(exc_type, exc_value, exc_tb):
    """Log truly unhandled exceptions before the process crashes."""
    if issubclass(exc_type, KeyboardInterrupt):
        _original_excepthook(exc_type, exc_value, exc_tb)
        return
    try:
        critical_msg = "".join(
            traceback.format_exception(exc_type, exc_value, exc_tb)
        )
        # Write directly to stderr — logger may be dead
        sys.stderr.write(f"\n[FATAL] Unhandled exception:\n{critical_msg}\n")
        sys.stderr.flush()
        # Also try the file logger if alive
        try:
            logging.getLogger(_PROCESS_NAME).critical(
                "Unhandled exception in main thread:\n%s", critical_msg
            )
        except Exception:
            pass
    except Exception:
        pass
    _original_excepthook(exc_type, exc_value, exc_tb)

sys.excepthook = _global_excepthook

from AnonX_3 import (LOG_FILE_PATH, anon, app, bootstrap_restart_guard, config,
                   db, ensure_log_file, http_media_tasks, logger,
                   process_instance_lock, reset_runtime_dirs, stop, thumb, userbot, yt,
                   PROCESS_STOP_TIMEOUT_SEC)
from AnonX_3.core.error_monitor import install_error_monitor
from AnonX_3.core.resource_budget import log_effective_playback_mode
from AnonX_3.core.supervisor import supervisor
from AnonX_3.helpers import buttons
from AnonX_3.plugins import all_modules

# Cookie watcher integration
_cookie_watcher = None

# Downloader API integration
_downloader_api_server = None


MYANMAR_STANDARD_TIME = timezone(
    timedelta(hours=6, minutes=30),
    name="Asia/Yangon",
)
AUTO_RESTART_TIMEZONE_LABEL = "Asia/Yangon (UTC+06:30)"
AUTO_RESTART_STOP_TIMEOUT_SEC = PROCESS_STOP_TIMEOUT_SEC

_lifecycle_phase = "initialization"
_failure_phase: str | None = None
_HEARTBEAT_FILE = Path(
    (os.getenv("ANONX_HEARTBEAT_FILE", ".runtime/heartbeat") or ".runtime/heartbeat").strip()
)
_HEARTBEAT_INTERVAL_SEC = max(1.0, float(
    os.getenv("ANONX_HEARTBEAT_INTERVAL_SEC", "5") or 5
))


def _set_lifecycle_phase(phase: str) -> None:
    global _lifecycle_phase

    previous = _lifecycle_phase
    _lifecycle_phase = phase
    logger.info(
        "Lifecycle phase transition: pid=%s from=%s to=%s",
        os.getpid(),
        previous,
        phase,
    )


def _next_midnight_delay() -> tuple[float, datetime, str]:
    now = datetime.now(MYANMAR_STANDARD_TIME)
    next_midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    delay = max(1.0, (next_midnight - now).total_seconds())
    return delay, next_midnight, AUTO_RESTART_TIMEZONE_LABEL

def _reset_log_file_for_restart() -> None:
    try:
        os.remove(LOG_FILE_PATH)
        return
    except Exception:
        pass

    try:
        with open(LOG_FILE_PATH, "w", encoding="utf-8"):
            pass
    except Exception as ex:
        logger.warning("Failed to reset log.txt before restart: %s", ex)


async def _runtime_heartbeat() -> None:
    """Continuously prove event-loop liveness to the outer ``start`` watchdog."""
    path = _HEARTBEAT_FILE
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            now = time.time()
            path.write_text(f"pid={os.getpid()} wall={now:.3f}\n", encoding="utf-8")
            os.utime(path, None)
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            logger.warning("Runtime heartbeat update failed: %s", ex)
        await asyncio.sleep(_HEARTBEAT_INTERVAL_SEC)


async def _daily_auto_restart() -> None:
    """Fresh-process restart at 00:00 Asia/Yangon without deleting media/cache."""
    while True:
        delay, next_midnight, tz_label = _next_midnight_delay()
        logger.info(
            "Daily auto-restart scheduled for %s (%s); media cache preserved.",
            next_midnight.strftime("%Y-%m-%d %H:%M:%S"),
            tz_label,
        )
        await asyncio.sleep(delay)
        logger.warning("Running daily auto-restart at midnight (%s).", tz_label)
        current = asyncio.current_task()
        if current in tasks:
            tasks.remove(current)
        try:
            await asyncio.wait_for(stop(), timeout=AUTO_RESTART_STOP_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            logger.warning(
                "Daily auto-restart stop() timed out after %ss; forcing re-exec.",
                AUTO_RESTART_STOP_TIMEOUT_SEC,
            )
        except Exception as ex:
            logger.warning("Daily auto-restart stop() failed: %s", ex)
        finally:
            logger.warning("Daily auto-restart is starting a fresh interpreter.")
            child_env = dict(os.environ)
            child_env["ANONX_DAILY_RESTART_AT"] = next_midnight.strftime("%Y-%m-%d %H:%M:%S")
            child_env["ANONX_DAILY_RESTART_TZ"] = "Asia/Yangon"
            exec_fresh_process(
                resolve_package_name(__package__),
                reason="daily-auto-restart",
                clear_crash_state=True,
                env=child_env,
            )


async def _start_downloader_api() -> None:
    """Start the integrated downloader API server."""
    global _downloader_api_server
    try:
        import uvicorn
        from AnonX_3.downloader_api.main import create_app
        from AnonX_3.downloader_api.core.config import settings as api_settings
    except ImportError as ex:
        logger.warning("Downloader API dependencies not installed: %s", ex)
        logger.info("Run: pip install fastapi uvicorn pydantic-settings aiofiles httpx")
        # Sleep forever instead of returning (prevents restart loop)
        while True:
            await asyncio.sleep(86400)
        return

    try:
        api_app = create_app()
        api_config = uvicorn.Config(
            api_app,
            host=getattr(config, "DOWNLOADER_API_HOST", api_settings.host),
            port=getattr(config, "DOWNLOADER_API_PORT", api_settings.port),
            log_level="warning",
        )
        _downloader_api_server = uvicorn.Server(api_config)
        logger.info(
            "Starting Downloader API on %s:%s",
            api_config.host,
            api_config.port,
        )
        await _downloader_api_server.serve()
    except Exception as ex:
        logger.warning("Downloader API failed to start: %s", ex)
        # Sleep forever on error too
        while True:
            await asyncio.sleep(86400)


async def _periodic_cookie_refresh() -> None:
    while True:
        refresh_sec = max(
            300,
            int(getattr(config, "COOKIE_REFRESH_SEC", 21600) or 21600),
        )
        await asyncio.sleep(refresh_sec)
        try:
            await yt.refresh_local_cookies(reason="periodic")
        except Exception as ex:
            logger.warning("Cookie agent periodic refresh failed: %s", type(ex).__name__)


async def _periodic_auto_reply_cleanup() -> None:
    """Expire inactive learned-only replies in bounded, supervised batches."""
    while True:
        removed = await db.cleanup_stale_auto_reply_rules(
            max_idle_seconds=float(config.AUTO_LEARN_TTL_HOURS) * 3600.0,
            limit=config.AUTO_LEARN_CLEANUP_BATCH,
        )
        if removed:
            logger.info(
                "Auto-learn cleanup removed=%s ttl_hours=%s",
                len(removed),
                config.AUTO_LEARN_TTL_HOURS,
            )
        await asyncio.sleep(config.AUTO_LEARN_CLEANUP_INTERVAL_SEC)


async def _periodic_bot_health() -> None:
    """Monitor Pyrogram client connection and auto-reconnect if disconnected."""
    CHECK_INTERVAL = 30
    RECONNECT_DELAY = 5.0
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            if not app.is_connected:
                logger.warning(
                    "Bot client disconnected — attempting reconnect in %ss",
                    RECONNECT_DELAY,
                )
                await asyncio.sleep(RECONNECT_DELAY)
                if not app.is_connected:
                    await app.start()
                    logger.info("Bot client reconnected successfully.")
            # Also check userbot if available
            if hasattr(userbot, '_bots') and userbot._bots:
                for idx, ub in enumerate(userbot._bots):
                    if not ub.is_connected:
                        logger.warning(
                            "Userbot #%s disconnected — attempting reconnect",
                            idx + 1,
                        )
                        try:
                            await ub.start()
                            logger.info("Userbot #%s reconnected.", idx + 1)
                        except Exception as ex:
                            logger.warning(
                                "Userbot #%s reconnect failed: %s", idx + 1, ex
                            )
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            logger.warning("Bot health check failed: %s: %s", type(ex).__name__, ex)


async def idle():
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGABRT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass
        except Exception as ex:
            logger.warning("Failed to register signal handler for %s: %s", sig, ex)
    
    logger.info("Bot is running — press Ctrl+C to stop.")
    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        logger.info("Idle task cancelled — shutting down.")
    except Exception as ex:
        logger.critical("Idle loop crashed: %s: %s", type(ex).__name__, ex)

async def _run_once():
    _set_lifecycle_phase("database")
    try:
        ensure_log_file(LOG_FILE_PATH)
    except Exception:
        pass
    install_error_monitor()
    await db.connect()
    await buttons.load_styles(db)

    # Warm authorization filters and register every handler before starting
    # the Telegram client. Previously Bot/Assistant Started notifications were
    # sent first, so commands submitted immediately afterwards could be
    # dispatched while no plugin handlers (or persisted sudo IDs) were loaded.
    sudoers = await db.get_sudoers()
    owners = await db.get_owners()
    app.owners.update(owners)
    app._sudo_ids.update(owners)
    app._sudo_ids.update(sudoers)
    app.bl_users.update(await db.get_blacklisted())

    _set_lifecycle_phase("plugins")
    for module in all_modules:
        try:
            importlib.import_module(f"AnonX_3.plugins.{module}")
        except Exception as ex:
            logger.critical(
                "Failed to load plugin '%s': %s: %s — bot will continue without it.",
                module,
                type(ex).__name__,
                ex,
            )
    supervisor.spawn("auto_reply_cleanup", _periodic_auto_reply_cleanup)
    logger.info(
        "Loaded %s modules, %s owners and %s sudo users before bot startup.",
        len(all_modules),
        len(app.owners),
        len(app._sudo_ids),
    )

    _set_lifecycle_phase("main-client")
    await app.boot()
    # Spawn health monitor right after bot connects
    supervisor.spawn("bot_health", _periodic_bot_health)
    supervisor.spawn("runtime_heartbeat", _runtime_heartbeat)
    supervisor.spawn("daily_auto_restart", _daily_auto_restart)
    if config.STARTGROUP_URLS:
        logger.info(
            "Startgroup config loaded: urls=%s weights=%s",
            config.STARTGROUP_URLS,
            config.STARTGROUP_WEIGHTS or "uniform",
        )
    else:
        logger.warning(
            "Startgroup config missing or empty; falling back to @%s startgroup link.",
            getattr(app, "username", "unknown"),
        )
    _set_lifecycle_phase("assistants")
    await userbot.boot()
    _set_lifecycle_phase("voice")
    await anon.boot()
    _set_lifecycle_phase("media")
    await thumb.start()
    # Dynamic Resource Control: begin event-loop lag sampling now that a loop
    # exists. Never fatal — on failure the controller latches to fixed limits.
    try:
        from AnonX_3.core.resource_manager import resource_manager

        resource_manager.start_dynamic_control()
        logger.info(
            "dynamic_resource_control enabled=%s limits=%s",
            getattr(config, "DYNAMIC_RESOURCE_CONTROL", True),
            resource_manager.stats().get("limits"),
        )
    except Exception as ex:
        logger.warning("Dynamic resource control start failed (fixed limits): %s", ex)
    log_effective_playback_mode(logger)

    # Cookie watcher - dynamic real-time sync from configured Firefox profile
    global _cookie_watcher
    if getattr(config, "COOKIE_WATCHER_ENABLED", False):
        user_data_dir = getattr(config, "COOKIE_WATCHER_USER_DATA_DIR", "")
        if user_data_dir:
            try:
                from AnonX_3.core.cookie_watcher import FirefoxCookieWatcher
                cookie_output = f"{yt.cookie_dir}/{yt.cookie_txt_name}"

                async def _sync_browser_cookie_profile() -> bool:
                    selected = await yt.refresh_local_cookies(
                        force=True,
                        reason="cookie-watcher",
                        auth_recovery=True,
                    )
                    return bool(selected)

                _cookie_watcher = FirefoxCookieWatcher(
                    user_data_dir=user_data_dir,
                    cookie_output_path=cookie_output,
                    check_interval=getattr(config, "COOKIE_WATCHER_INTERVAL_SEC", 60),
                    youtube_domain_filter=getattr(config, "COOKIE_WATCHER_YOUTUBE_ONLY", True),
                    sync_callback=_sync_browser_cookie_profile,
                )
                _cookie_watcher.start()
                yt._cookie_watcher = _cookie_watcher
                logger.info(
                    "Cookie watcher started: monitoring %s every %ds",
                    user_data_dir,
                    getattr(config, "COOKIE_WATCHER_INTERVAL_SEC", 60),
                )
            except Exception as ex:
                logger.warning("Cookie watcher failed to start: %s", ex)
        else:
            logger.warning("COOKIE_WATCHER_ENABLED=True but COOKIE_WATCHER_USER_DATA_DIR not set")

    if getattr(config, "COOKIE_FREE_MODE", True):
        if yt.auth_cookie_recovery_enabled():
            logger.info(
                "Cookie-free mode active: Firefox cookies enabled only for "
                "classified YouTube auth-challenge recovery"
            )
        else:
            logger.info(
                "Cookie-free mode active: browser sessions and cookie files are disabled"
            )
    else:
        if config.COOKIES_URL:
            await yt.save_cookies(config.COOKIES_URL)
        yt.checked = False
        await yt.refresh_local_cookies(reason="startup")
        supervisor.spawn("periodic_cookie_refresh", _periodic_cookie_refresh)

    # ── Startup health dashboard: cookies, POT, proxy, browser profile ──
    await yt._log_startup_health()

    # Warm the sticky mweb direct-resolver runtime only after cookies/provider
    # configuration are finalized. No video is fetched here; this moves
    # YoutubeDL/plugin/cookie-jar construction off the first /play or /vplay.
    if getattr(config, "DIRECT_RESOLVER_STARTUP_WARM", True):
        try:
            await asyncio.wait_for(
                yt.warm_direct_resolver_runtime(),
                timeout=float(
                    getattr(config, "DIRECT_RESOLVER_STARTUP_WARM_TIMEOUT_SEC", 4.0)
                    or 4.0
                ),
            )
        except asyncio.TimeoutError:
            logger.warning("direct_resolver startup_warm timed out; continuing cold-safe")
        except Exception as ex:
            logger.warning(
                "direct_resolver startup_warm failed; continuing cold-safe: %s",
                type(ex).__name__,
            )

    # ── Periodic service health monitor (every 5 min) ──
    supervisor.spawn("service_health", yt._periodic_service_health)

    # Start integrated Downloader API
    if getattr(config, "DOWNLOADER_API_ENABLED", False):
        supervisor.spawn("downloader_api", _start_downloader_api)
        logger.info("Downloader API integration enabled")

    # CDN: fully dynamic — no .env required (default ON)
    if getattr(config, "CDN_ENABLED", True):
        from AnonX_3.core.cdn import cdn, cdn_gc_loop, start_cdn_origin

        try:
            cdn.ready_dir()
            cdn.tmp_dir()
        except Exception as ex:
            logger.warning("CDN media dirs: %s", ex)
        supervisor.spawn("cdn_gc_loop", cdn_gc_loop)
        try:
            origin_task = await start_cdn_origin()
            if origin_task is not None:
                http_media_tasks.append(origin_task)
        except Exception as ex:
            logger.warning("CDN origin start failed (play still uses local ready/): %s", ex)
        logger.info(
            "CDN auto: mode=%s root=%s origin=%s base=%s",
            getattr(config, "CDN_PLAY_MODE", "hybrid"),
            getattr(config, "CDN_MEDIA_ROOT", "media"),
            getattr(config, "CDN_ORIGIN_ENABLED", False),
            getattr(config, "CDN_PUBLIC_BASE_URL", "")
            or getattr(config, "CDN_ORIGIN_PUBLIC_BASE", "")
            or "local-ready",
        )

    # Optional health/metrics HTTP (HEALTH_PORT>0)
    try:
        from AnonX_3.core.health import start_health_server

        health_task = await start_health_server()
        if health_task is not None:
            http_media_tasks.append(health_task)
    except Exception as ex:
        logger.warning("Health server start failed: %s", ex)

    _set_lifecycle_phase("running")
    await idle()


async def main():
    """Run one lifecycle and always release partially-started resources."""
    global _failure_phase

    _failure_phase = None
    try:
        await _run_once()
    except BaseException:
        _failure_phase = _lifecycle_phase
        raise
    finally:
        _set_lifecycle_phase("shutdown")
        try:
            await stop()
        except Exception as ex:
            logger.critical("stop() crashed: %s: %s", type(ex).__name__, ex)


def _system_exit_code(error: SystemExit) -> int:
    code = error.code
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    print(str(code), file=sys.stderr)
    return 1


def _run_process() -> int:
    """Own one session lifecycle; only crashes replace the interpreter."""

    identity = resolve_runtime_identity(__package__)
    instance_lock = process_instance_lock
    try:
        instance_lock.acquire()
    except ProcessInstanceAlreadyRunning as ex:
        owner_pid = ex.owner_metadata.get("pid", "unknown")
        owner_started = ex.owner_metadata.get("started_at", "unknown")
        message = (
            f"[{identity.package_name}] Duplicate instance rejected: "
            f"session=<deploy-root>/{identity.session_path.name} "
            f"lock=<deploy-root>/{identity.lock_path.name} "
            f"owner_pid={owner_pid} owner_started_at={owner_started}. "
            f"Exit code={DUPLICATE_INSTANCE_EXIT_CODE}."
        )
        print(message, file=sys.stderr)
        logger.critical(message)
        return DUPLICATE_INSTANCE_EXIT_CODE

    logger.info(
        "Process instance lock acquired: package=%s pid=%s session=%s lock=%s",
        identity.package_name,
        os.getpid(),
        f"<deploy-root>/{identity.session_path.name}",
        f"<deploy-root>/{identity.lock_path.name}",
    )
    started_at = time.monotonic()
    crashed = False
    exit_code = 0
    try:
        # Package and __main__ imports are now complete.  From this exact
        # boundary onward, this function owns every terminal/crash outcome.
        bootstrap_restart_guard.complete()
        asyncio.run(main())
        logger.info("Application lifecycle completed normally; no restart requested.")
        print(
            f"[{identity.package_name}] Clean shutdown — exiting.",
            file=sys.stderr,
        )
    except KeyboardInterrupt:
        exit_code = 130
        logger.info("KeyboardInterrupt received; exiting without restart.")
        print(
            f"\n[{identity.package_name}] KeyboardInterrupt — exiting.",
            file=sys.stderr,
        )
    except SystemExit as ex:
        exit_code = _system_exit_code(ex)
        logger.info(
            "SystemExit received: code=%s; exiting without restart.",
            ex.code,
        )
        print(
            f"[{identity.package_name}] SystemExit code={ex.code} — "
            "exiting without restart.",
            file=sys.stderr,
        )
    except BaseException as ex:
        crashed = True
        exit_code = 1
        failure_phase = _failure_phase or _lifecycle_phase
        logger.critical(
            "Application lifecycle crashed: pid=%s phase=%s runtime=%.3fs "
            "error=%s: %s",
            os.getpid(),
            failure_phase,
            time.monotonic() - started_at,
            type(ex).__name__,
            ex,
            exc_info=True,
        )
        print(
            f"\n[FATAL] Process crashed: pid={os.getpid()} "
            f"phase={failure_phase} {type(ex).__name__}: {ex}",
            file=sys.stderr,
        )
        traceback.print_exception(type(ex), ex, ex.__traceback__, file=sys.stderr)
    if not crashed:
        instance_lock.release()
        logger.info(
            "Process instance lock released: package=%s pid=%s lock=%s",
            identity.package_name,
            os.getpid(),
            f"<deploy-root>/{identity.lock_path.name}",
        )
        return exit_code

    try:
        runtime_seconds = time.monotonic() - started_at
        restart_plan = plan_crash_restart(runtime_seconds)
        if restart_plan.reset_after_stable_runtime:
            logger.info(
                "Crash restart attempt state reset after %.3fs stable runtime.",
                runtime_seconds,
            )
        logger.critical(
            "Fresh-process crash restart scheduled: attempt=%s base_delay=%.1fs "
            "jittered_delay=%.3fs runtime=%.3fs",
            restart_plan.attempt,
            restart_plan.base_delay_seconds,
            restart_plan.delay_seconds,
            runtime_seconds,
        )
        print(
            f"[{identity.package_name}] Restarting with a fresh interpreter in "
            f"{restart_plan.delay_seconds:.2f}s "
            f"(attempt #{restart_plan.attempt}, "
            f"base {restart_plan.base_delay_seconds:.0f}s)...",
            file=sys.stderr,
        )
        # Retain ownership during backoff.  The shared exec helper releases the
        # lock immediately before replacing the interpreter, closing the race
        # where another launcher could seize the persistent session mid-retry.
        time.sleep(restart_plan.delay_seconds)
        exec_fresh_process(
            identity.package_name,
            reason=f"crash-restart-attempt-{restart_plan.attempt}",
        )
        raise RuntimeError("fresh-process exec helper returned unexpectedly")
    except KeyboardInterrupt:
        logger.info("Crash restart delay interrupted; exiting without restart.")
        instance_lock.release()
        return 130
    except Exception as ex:
        instance_lock.release()
        logger.critical(
            "Fresh-process exec failed: %s: %s",
            type(ex).__name__,
            ex,
            exc_info=True,
        )
        traceback.print_exception(type(ex), ex, ex.__traceback__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_run_process())
