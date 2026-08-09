# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲

import os
import sys
import time
import asyncio
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ``python -m <variant>`` imports this package before executing ``__main__``.
# Claim the persistent session before even opening the shared log handler, and
# before Config, Mongo, or Pyrogram clients are constructed.  A rejected
# duplicate therefore touches no application-owned service or shared database.
from AnonX_3.core.lifecycle import (
    BootstrapRestartGuard,
    DUPLICATE_INSTANCE_EXIT_CODE,
    ProcessInstanceAlreadyRunning,
    ProcessInstanceLock,
    resolve_runtime_identity,
)

runtime_identity = resolve_runtime_identity(__name__)
process_instance_lock = ProcessInstanceLock(runtime_identity)
try:
    process_instance_lock.acquire()
except ProcessInstanceAlreadyRunning as ex:
    owner_pid = ex.owner_metadata.get("pid", "unknown")
    owner_started = ex.owner_metadata.get("started_at", "unknown")
    duplicate_message = (
        f"[{runtime_identity.package_name}] Duplicate instance rejected before "
        "service initialization: "
        f"session=<deploy-root>/{runtime_identity.session_path.name} "
        f"lock=<deploy-root>/{runtime_identity.lock_path.name} "
        f"owner_pid={owner_pid} owner_started_at={owner_started}. "
        f"Exit code={DUPLICATE_INSTANCE_EXIT_CODE}."
    )
    print(duplicate_message, file=sys.stderr)
    raise SystemExit(DUPLICATE_INSTANCE_EXIT_CODE) from None

bootstrap_restart_guard = BootstrapRestartGuard(
    runtime_identity,
    process_instance_lock,
)
bootstrap_restart_guard.install()


class _IgnorePyrogramPeerIdInvalid(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "pyrogram.dispatcher":
            return True
        message = record.getMessage().lower()
        if "peer_id_invalid" in message:
            return False
        return True


class _TimezoneFormatter(logging.Formatter):
    def __init__(self, *args, tz_name: str = "Asia/Yangon", **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self._tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            try:
                self._tz = ZoneInfo("UTC")
            except ZoneInfoNotFoundError:
                self._tz = timezone.utc

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, self._tz)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat(timespec="seconds")


_LOG_TIMEZONE = (os.getenv("ACTIVEVC_TIMEZONE", "Asia/Yangon") or "Asia/Yangon").strip()
_LOG_FORMAT = "[%(asctime)s - %(levelname)s] - %(name)s: %(message)s"
_LOG_DATEFMT = "%d-%b-%y %H:%M:%S"


def _log_path_candidates() -> list[str]:
    env_path = (os.getenv("AnonX_LOG_FILE", "") or "").strip()
    package_dir = os.path.abspath(os.path.dirname(__file__))
    repo_root = os.path.abspath(os.path.join(package_dir, ".."))
    workspace_root = os.path.abspath(os.path.join(package_dir, "..", ".."))
    cwd_root = os.path.abspath(os.getcwd())
    candidates = [
        env_path,
        os.path.join(cwd_root, "log.txt"),
        os.path.join(workspace_root, "log.txt"),
        os.path.join(repo_root, "log.txt"),
    ]
    seen: set[str] = set()
    normalized: list[str] = []
    for path in candidates:
        if not path:
            continue
        full_path = os.path.abspath(path)
        if full_path in seen:
            continue
        seen.add(full_path)
        normalized.append(full_path)
    return normalized


def resolve_log_file_path() -> str:
    for candidate in _log_path_candidates():
        if os.path.exists(candidate):
            return candidate
    return _log_path_candidates()[0]


def ensure_log_file(path: str | None = None) -> str:
    target = os.path.abspath(path or resolve_log_file_path())
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "a", encoding="utf-8"):
        pass
    return target


def write_log_snapshot_marker(
    path: str | None = None,
    *,
    reason: str = "Log snapshot requested.",
) -> str:
    target = ensure_log_file(path)
    timestamp = datetime.now().strftime(_LOG_DATEFMT)
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(f"[{timestamp} - INFO] - AnonX: {reason}\n")
    return target


def flush_log_handlers() -> None:
    for holder in (logging.getLogger(), logging.getLogger(__name__)):
        for handler in getattr(holder, "handlers", []):
            try:
                handler.flush()
            except Exception:
                pass


_TESTING = (os.getenv("AnonX_TESTING", "") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
if _TESTING:
    # Unit/smoke diagnostics must never contaminate the deployed runtime log.
    LOG_FILE_PATH = os.devnull
    _LOG_HANDLERS = [logging.NullHandler()]
else:
    LOG_FILE_PATH = ensure_log_file()
    _LOG_HANDLERS = [
        RotatingFileHandler(LOG_FILE_PATH, maxBytes=10485760, backupCount=5),
        logging.StreamHandler(),
    ]

logging.basicConfig(
    format=_LOG_FORMAT,
    datefmt=_LOG_DATEFMT,
    handlers=_LOG_HANDLERS,
    level=logging.INFO,
)
for _handler in logging.getLogger().handlers:
    _handler.setFormatter(
        _TimezoneFormatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT, tz_name=_LOG_TIMEZONE)
    )
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("ntgcalls").setLevel(logging.CRITICAL)
logging.getLogger("pymongo").setLevel(logging.ERROR)
logging.getLogger("pyrogram").setLevel(logging.ERROR)
logging.getLogger("pytgcalls").setLevel(logging.ERROR)
logging.getLogger("pyrogram.dispatcher").addFilter(_IgnorePyrogramPeerIdInvalid())
logger = logging.getLogger(__name__)


__version__ = "3.4.10"

logger.info(
    "Bootstrap instance lock acquired before service initialization: "
    "package=%s pid=%s session=%s lock=%s",
    runtime_identity.package_name,
    os.getpid(),
    f"<deploy-root>/{runtime_identity.session_path.name}",
    f"<deploy-root>/{runtime_identity.lock_path.name}",
)

from config import Config

config = Config()
config.check()
tasks = []
http_media_tasks = []
boot = time.time()

TASK_CANCEL_TIMEOUT_SEC = 20
CLIENT_STOP_TIMEOUT_SEC = 15
PROCESS_STOP_TIMEOUT_SEC = 45

from AnonX_3.core.bot import Bot
app = Bot()

from AnonX_3.core.bot_api import BotAPI
bot_api = BotAPI()

from AnonX_3.core.dir import ensure_dirs, reset_runtime_dirs, runtime_storage_percent
ensure_dirs()

from AnonX_3.core.userbot import Userbot
userbot = Userbot()

from AnonX_3.core.mongo import MongoDB
db = MongoDB()

from AnonX_3.core.lang import Language
lang = Language()

from AnonX_3.core.telegram import Telegram
from AnonX_3.core.tiktok import TikTok
from AnonX_3.core.facebook import Facebook
from AnonX_3.core.youtube import YouTube
from AnonX_3.core.downloader.singleflight import shutdown_singleflights
tg = Telegram()
tiktok = TikTok()
facebook = Facebook()
yt = YouTube()

from AnonX_3.helpers import Queue, Thumbnail
queue = Queue()
thumb = Thumbnail()

from AnonX_3.core.calls import TgCall
anon = TgCall()

_shutdown_lock = asyncio.Lock()
_shutdown_state = "running"


def _collect_all_tasks(roots: list[asyncio.Task], *, skip_id: int) -> list[asyncio.Task]:
    """Walk the task tree iteratively; returns a flat list with no duplicates.

    Python 3.12+ ``Task.cancel()`` recursively walks ``_children``, so a deep
    or circular tree overflows the C stack.  Gather every reachable task here
    so we can clear ``_children`` before calling ``cancel()``.
    """
    collected: list[asyncio.Task] = []
    seen = {skip_id}
    stack = list(roots)
    while stack:
        t = stack.pop()
        tid = id(t)
        if tid in seen:
            continue
        seen.add(tid)
        collected.append(t)
        for child in getattr(t, "_children", ()):
            if not child.done() and id(child) not in seen:
                stack.append(child)
    return collected


async def _cancel_owned_task_registry(
    registry: list[asyncio.Task],
    *,
    label: str,
    preserve: tuple[asyncio.Task, ...] = (),
) -> None:
    """Cancel one explicit task registry, optionally preserving later-stage work."""

    current = asyncio.current_task()
    preserved_ids = {id(task) for task in preserve if task is not None}
    roots = [
        task
        for task in list(registry)
        if task is not current and id(task) not in preserved_ids
    ]
    registry[:] = [
        task
        for task in registry
        if id(task) in preserved_ids and not task.done()
    ]
    pending = [
        task
        for task in _collect_all_tasks(
            roots,
            skip_id=id(current) if current else 0,
        )
        if not task.done()
    ]

    # Defuse recursive cancel: clear _children on every owned task so
    # Task.cancel() stays O(1) instead of walking the tree.
    for task in pending:
        children = getattr(task, "_children", None)
        if children is not None:
            try:
                children.clear()
            except Exception:
                pass
    for task in pending:
        try:
            task.cancel()
        except Exception:
            pass
    if pending:
        results = await asyncio.gather(*pending, return_exceptions=True)
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            raise ExceptionGroup(f"{label} shutdown failed", failures)


async def _cancel_registered_tasks() -> None:
    """Cancel general application tasks, deferring the media renderer."""

    thumbnail_worker = getattr(thumb, "_worker_task", None)
    preserve = (
        (thumbnail_worker,)
        if isinstance(thumbnail_worker, asyncio.Task)
        else ()
    )
    await _cancel_owned_task_registry(
        tasks,
        label="Background task",
        preserve=preserve,
    )


async def _cancel_http_media_tasks() -> None:
    """Stop health/CDN HTTP tasks after Telegram clients have quiesced."""

    await _cancel_owned_task_registry(
        http_media_tasks,
        label="HTTP/media task",
    )


async def _stop_background_watchers() -> None:
    """Stop watcher tasks that are intentionally outside ``tasks``."""
    owned: list[asyncio.Task] = []
    failures: list[Exception] = []

    watcher = getattr(yt, "_cookie_watcher", None)
    if watcher is not None:
        watcher_task = getattr(watcher, "_task", None)
        if isinstance(watcher_task, asyncio.Task):
            owned.append(watcher_task)
        try:
            watcher.stop()
        except Exception as ex:
            logger.warning("Cookie watcher stop failed: %s", ex)
            failures.append(ex)

    refresh_task = getattr(yt, "_cookie_refresh_task", None)
    if isinstance(refresh_task, asyncio.Task):
        owned.append(refresh_task)
        yt._cookie_refresh_task = None

    for task in dict.fromkeys(owned):
        if not task.done():
            task.cancel()
    if owned:
        results = await asyncio.gather(
            *dict.fromkeys(owned),
            return_exceptions=True,
        )
        failures.extend(result for result in results if isinstance(result, Exception))
    if failures:
        raise ExceptionGroup("Background watcher shutdown failed", failures)


async def _run_shutdown_step(
    label: str,
    closer,
    *,
    deadline: float,
    timeout: float,
) -> Exception | None:
    """Run one ordered closer within the shared process deadline."""
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        logger.warning("Shutdown deadline exhausted before %s.", label)
        return TimeoutError(f"Shutdown deadline exhausted before {label}")
    step_timeout = min(max(0.001, timeout), remaining)
    try:
        await asyncio.wait_for(closer(), timeout=step_timeout)
    except asyncio.TimeoutError:
        logger.warning("%s timed out after %.1fs during shutdown.", label, step_timeout)
        return TimeoutError(f"{label} timed out after {step_timeout:.1f}s")
    except asyncio.CancelledError:
        logger.warning("%s was cancelled during shutdown.", label)
        return RuntimeError(f"{label} was cancelled during shutdown")
    except Exception as ex:
        logger.warning("%s failed during shutdown: %s", label, ex)
        return ex
    return None


async def stop() -> None:
    """Stop the singleton service graph once, in reverse dependency order."""
    global _shutdown_state

    async with _shutdown_lock:
        if _shutdown_state == "stopped":
            return
        _shutdown_state = "stopping"
        logger.info("Stopping...")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PROCESS_STOP_TIMEOUT_SEC
        shutdown_failures: list[Exception] = []

        try:
            # 1. Supervisors and background watchers must stop spawning work.
            from AnonX_3.core.supervisor import supervisor

            failure = await _run_shutdown_step(
                "supervisor.shutdown",
                supervisor.shutdown,
                deadline=deadline,
                timeout=TASK_CANCEL_TIMEOUT_SEC,
            )
            if failure is not None:
                shutdown_failures.append(failure)
            failure = await _run_shutdown_step(
                "background_watchers.stop",
                _stop_background_watchers,
                deadline=deadline,
                timeout=TASK_CANCEL_TIMEOUT_SEC,
            )
            if failure is not None:
                shutdown_failures.append(failure)
            failure = await _run_shutdown_step(
                "background_tasks.cancel",
                _cancel_registered_tasks,
                deadline=deadline,
                timeout=TASK_CANCEL_TIMEOUT_SEC,
            )
            if failure is not None:
                shutdown_failures.append(failure)

            # 2-6. Reverse dependency order for long-lived services.  HTTP
            # ingress and media helpers stay alive until main Pyrogram stops,
            # then quiesce before Mongo is closed.
            async def _stop_dynamic_capacity() -> None:
                from AnonX_3.core.resource_manager import resource_manager

                await resource_manager.stop_dynamic_control()

            ordered_services = [
                ("anon.shutdown", anon.shutdown),
                ("userbot.exit", userbot.exit),
                ("app.exit", app.exit),
                ("http_media_tasks.cancel", _cancel_http_media_tasks),
                ("tg.shutdown", tg.shutdown),
                ("tiktok.shutdown", tiktok.shutdown),
                ("facebook.shutdown", facebook.shutdown),
                ("singleflights.shutdown", shutdown_singleflights),
                ("dynamic_capacity.stop", _stop_dynamic_capacity),
                ("thumb.close", thumb.close),
                ("yt.close", yt.close),
                ("bot_api.close", bot_api.close),
                ("db.close", db.close),
            ]
            for label, closer in ordered_services:
                failure = await _run_shutdown_step(
                    label,
                    closer,
                    deadline=deadline,
                    timeout=CLIENT_STOP_TIMEOUT_SEC,
                )
                if failure is not None:
                    shutdown_failures.append(failure)
        finally:
            # Errors and timeouts are terminal for this interpreter.  Never
            # re-enter already-closed Mongo/Pyrogram objects on another stop().
            tasks.clear()
            http_media_tasks.clear()
            _shutdown_state = "stopped"

        if shutdown_failures:
            logger.error(
                "Shutdown incomplete: %s step(s) failed or timed out.",
                len(shutdown_failures),
            )
        else:
            logger.info("Stopped.\n")


def _cleanup_package_bootstrap_services() -> None:
    """Best-effort cleanup if a later ``__main__`` import fails."""

    asyncio.run(stop())


bootstrap_restart_guard.set_cleanup(_cleanup_package_bootstrap_services)
