# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""Process ownership and fresh-interpreter restart primitives.

The main Pyrogram bot uses a persistent SQLite session.  Exactly one process
may own that session at a time, and a crashed lifecycle must never reuse the
already-stopped global client objects in the same interpreter.

This module intentionally depends only on the Python standard library so the
instance lock can be acquired before any network-backed service is started.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import random
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Mapping, MutableMapping, NoReturn, Sequence


DUPLICATE_INSTANCE_EXIT_CODE = 75
RESTART_ATTEMPT_ENV = "ANONX_INTERNAL_RESTART_ATTEMPT"
RESTART_STABLE_WINDOW_SEC = 120.0
CRASH_RESTART_DELAYS_SECONDS = (2.0, 4.0, 8.0, 16.0, 32.0, 60.0)
CRASH_RESTART_JITTER_RATIO = 0.10

_PACKAGE_DIR = Path(__file__).resolve().parents[1]
_DEFAULT_PACKAGE_NAME = _PACKAGE_DIR.name
_DEFAULT_DEPLOY_ROOT = _PACKAGE_DIR.parent
_ACTIVE_LOCK_GUARD = threading.RLock()
_ACTIVE_PROCESS_LOCK: ProcessInstanceLock | None = None


@dataclass(frozen=True)
class RuntimeIdentity:
    """Resolved names and paths shared by Pyrogram and the process guard."""

    package_name: str
    deploy_root: Path
    session_path: Path
    lock_path: Path


@dataclass(frozen=True)
class CrashRestartPlan:
    """One fresh-process crash restart decision."""

    attempt: int
    base_delay_seconds: float
    delay_seconds: float
    reset_after_stable_runtime: bool


def resolve_package_name(package_hint: str | None = None) -> str:
    """Return the active top-level package name without variant hard-coding."""

    candidate = (package_hint or _DEFAULT_PACKAGE_NAME).strip().split(".", 1)[0]
    if not candidate or not candidate.isidentifier():
        raise ValueError(f"Invalid package name: {candidate!r}")
    return candidate


def resolve_runtime_identity(
    package_hint: str | None = None,
    deploy_root: os.PathLike[str] | str | None = None,
) -> RuntimeIdentity:
    """Resolve the persistent session and stable lock paths for this variant."""

    package_name = resolve_package_name(package_hint)
    root = Path(deploy_root or _DEFAULT_DEPLOY_ROOT).expanduser().resolve()
    return RuntimeIdentity(
        package_name=package_name,
        deploy_root=root,
        session_path=root / f"{package_name}.session",
        lock_path=root / f"{package_name}.instance.lock",
    )


class ProcessInstanceAlreadyRunning(RuntimeError):
    """Raised when another process owns the active variant session."""

    def __init__(
        self,
        lock_path: Path,
        owner_metadata: Mapping[str, object] | None = None,
    ) -> None:
        self.lock_path = Path(lock_path)
        self.owner_metadata = dict(owner_metadata or {})
        owner_pid = self.owner_metadata.get("pid", "unknown")
        owner_started = self.owner_metadata.get("started_at", "unknown")
        super().__init__(
            "another process owns the bot session "
            f"(pid={owner_pid}, started_at={owner_started}, "
            f"lock=<deploy-root>/{self.lock_path.name})"
        )


def _lock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _prepare_windows_lock_byte(handle: BinaryIO) -> None:
    """Ensure the byte-range lock has one byte without replacing metadata."""

    if os.name != "nt":
        return
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\n")
        handle.flush()


def _lock_metadata_offset() -> int:
    """Keep Windows' locked sentinel byte separate from readable metadata."""

    return 1 if os.name == "nt" else 0


def _unlock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _is_lock_contention(error: OSError) -> bool:
    return (
        isinstance(error, BlockingIOError)
        or error.errno
        in {
            errno.EACCES,
            errno.EAGAIN,
            errno.EDEADLK,
            errno.EWOULDBLOCK,
        }
        or getattr(error, "winerror", None) in {33, 36}
    )


def _read_lock_metadata(handle: BinaryIO) -> dict[str, object]:
    try:
        # Windows denies reads of the byte range owned by the incumbent.  Byte
        # zero is therefore a whitespace sentinel and the JSON starts at byte
        # one.  POSIX flock does not restrict reads, so it needs no sentinel.
        handle.seek(_lock_metadata_offset())
        payload = handle.read(64 * 1024).decode("utf-8", errors="replace").strip()
        decoded = json.loads(payload) if payload else {}
        return decoded if isinstance(decoded, dict) else {}
    except Exception:
        return {}


class ProcessInstanceLock:
    """Cross-platform, process-lifetime lock over a stable metadata file.

    The file is never unlinked.  Operating-system lock ownership, rather than
    the presence or contents of the file, determines whether an instance is
    active.  A stale metadata file is therefore harmless after a hard crash.
    """

    def __init__(self, identity: RuntimeIdentity) -> None:
        self.identity = identity
        self.lock_path = identity.lock_path
        self._handle: BinaryIO | None = None
        self.metadata: dict[str, object] = {}

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> ProcessInstanceLock:
        global _ACTIVE_PROCESS_LOCK

        if self.acquired:
            return self

        with _ACTIVE_LOCK_GUARD:
            if (
                _ACTIVE_PROCESS_LOCK is not None
                and _ACTIVE_PROCESS_LOCK is not self
                and _ACTIVE_PROCESS_LOCK.acquired
            ):
                raise RuntimeError("this process already owns an instance lock")

        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(self.lock_path, flags, 0o600)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            _prepare_windows_lock_byte(handle)
            _lock_file(handle)
        except OSError as ex:
            owner_metadata = _read_lock_metadata(handle)
            handle.close()
            if _is_lock_contention(ex):
                raise ProcessInstanceAlreadyRunning(
                    self.lock_path,
                    owner_metadata,
                ) from None
            raise

        metadata: dict[str, object] = {
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            payload = (json.dumps(metadata, sort_keys=True) + "\n").encode("utf-8")
            handle.seek(0)
            handle.truncate(0)
            if os.name == "nt":
                handle.write(b"\n")
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        except Exception:
            try:
                _unlock_file(handle)
            finally:
                handle.close()
            raise

        self._handle = handle
        self.metadata = metadata
        with _ACTIVE_LOCK_GUARD:
            _ACTIVE_PROCESS_LOCK = self
        return self

    def release(self) -> None:
        global _ACTIVE_PROCESS_LOCK

        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            _unlock_file(handle)
        except OSError:
            # Closing the descriptor also releases an OS-owned lock.
            pass
        finally:
            handle.close()
            with _ACTIVE_LOCK_GUARD:
                if _ACTIVE_PROCESS_LOCK is self:
                    _ACTIVE_PROCESS_LOCK = None

    def __enter__(self) -> ProcessInstanceLock:
        return self.acquire()

    def __exit__(self, *_exc_info: object) -> None:
        self.release()


def release_active_process_lock() -> None:
    """Release the current guard immediately before replacing the process."""

    with _ACTIVE_LOCK_GUARD:
        active_lock = _ACTIVE_PROCESS_LOCK
    if active_lock is not None:
        active_lock.release()


def clear_crash_restart_state(
    env: MutableMapping[str, str] | None = None,
) -> None:
    """Clear inherited crash backoff state for an intentional fresh start."""

    target = os.environ if env is None else env
    target.pop(RESTART_ATTEMPT_ENV, None)


def _read_restart_attempt(env: Mapping[str, str]) -> int:
    try:
        return max(0, int(env.get(RESTART_ATTEMPT_ENV, "0")))
    except (TypeError, ValueError):
        return 0


def plan_crash_restart(
    runtime_seconds: float,
    env: MutableMapping[str, str] | None = None,
    jitter_fraction: float | None = None,
) -> CrashRestartPlan:
    """Persist and return the next bounded, jittered crash restart delay."""

    target = os.environ if env is None else env
    stable_reset = max(0.0, runtime_seconds) >= RESTART_STABLE_WINDOW_SEC
    previous_attempt = 0 if stable_reset else _read_restart_attempt(target)
    attempt = previous_attempt + 1
    base_delay = CRASH_RESTART_DELAYS_SECONDS[
        min(attempt - 1, len(CRASH_RESTART_DELAYS_SECONDS) - 1)
    ]
    jitter = (
        random.uniform(-CRASH_RESTART_JITTER_RATIO, CRASH_RESTART_JITTER_RATIO)
        if jitter_fraction is None
        else float(jitter_fraction)
    )
    if not -CRASH_RESTART_JITTER_RATIO <= jitter <= CRASH_RESTART_JITTER_RATIO:
        raise ValueError(
            "jitter_fraction must be within "
            f"[-{CRASH_RESTART_JITTER_RATIO}, {CRASH_RESTART_JITTER_RATIO}]"
        )
    delay = max(0.0, base_delay * (1.0 + jitter))
    target[RESTART_ATTEMPT_ENV] = str(attempt)
    return CrashRestartPlan(
        attempt=attempt,
        base_delay_seconds=base_delay,
        delay_seconds=delay,
        reset_after_stable_runtime=stable_reset,
    )


def exec_fresh_process(
    package_name: str | None = None,
    *,
    reason: str,
    clear_crash_state: bool = False,
    env: Mapping[str, str] | None = None,
) -> NoReturn:
    """Replace this interpreter with ``python -m <active package>``."""

    active_package = resolve_package_name(package_name)
    child_env = dict(os.environ if env is None else env)
    if clear_crash_state:
        clear_crash_restart_state(child_env)

    logging.getLogger(active_package).warning(
        "Replacing process with a fresh interpreter: reason=%s package=%s pid=%s",
        reason,
        active_package,
        os.getpid(),
    )
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass

    argv = [sys.executable, "-m", active_package]
    release_active_process_lock()
    os.execvpe(sys.executable, argv, child_env)
    raise RuntimeError("os.execvpe returned without replacing the process")


class BootstrapRestartGuard:
    """Recover failures raised while ``python -m <package>`` imports modules.

    Python imports a package's ``__init__`` before it executes ``__main__``.
    The normal lifecycle try/except therefore cannot see an exception raised by
    Config validation or a later module import.  This narrow exception hook is
    armed only for an exact direct ``-m <active-package>`` launch.  It keeps the
    process lock through cleanup and backoff, then uses the same fresh-exec
    helper as the protected runtime lifecycle.
    """

    def __init__(
        self,
        identity: RuntimeIdentity,
        instance_lock: ProcessInstanceLock,
    ) -> None:
        self.identity = identity
        self.instance_lock = instance_lock
        self._state_lock = threading.RLock()
        self._active = False
        self._installed = False
        self._handling = False
        self._started_at = 0.0
        self._previous_hook: Callable[..., object] = sys.__excepthook__
        self._installed_hook: Callable[..., object] | None = None
        self._cleanup: Callable[[], object] | None = None

    @staticmethod
    def _is_direct_package_launch(
        package_name: str,
        argv: Sequence[str],
    ) -> bool:
        """Return true only for the exact ``python -m <package>`` target."""

        arguments = list(argv)
        try:
            module_index = arguments.index("-m")
        except ValueError:
            return False
        return (
            module_index + 1 < len(arguments)
            and arguments[module_index + 1] == package_name
        )

    @property
    def active(self) -> bool:
        with self._state_lock:
            return self._active

    def install(self, argv: Sequence[str] | None = None) -> bool:
        """Install once when this interpreter directly launched the package."""

        with self._state_lock:
            if self._installed:
                return self._active
            self._installed = True
            launch_argv = (
                tuple(getattr(sys, "orig_argv", ()) or ())
                if argv is None
                else tuple(argv)
            )
            if not self._is_direct_package_launch(
                self.identity.package_name,
                launch_argv,
            ):
                return False

            self._previous_hook = sys.excepthook
            self._installed_hook = self._handle_unhandled
            self._started_at = time.monotonic()
            self._active = True
            sys.excepthook = self._installed_hook
            return True

    def set_cleanup(self, cleanup: Callable[[], object]) -> None:
        """Register package-owned cleanup once the service graph exists."""

        with self._state_lock:
            self._cleanup = cleanup

    def complete(self) -> None:
        """Disarm immediately before the normal lifecycle takes ownership."""

        with self._state_lock:
            self._active = False
            self._cleanup = None
            installed_hook = self._installed_hook
            previous_hook = self._previous_hook
        # ``__main__`` may have installed a logging wrapper around our hook.
        # Restore only when nobody replaced the hook after us; otherwise the
        # inactive guard remains a safe delegate in that wrapper's chain.
        if installed_hook is not None and sys.excepthook is installed_hook:
            sys.excepthook = previous_hook

    def _delegate(self, exc_type, exc_value, exc_tb) -> None:
        hook = self._previous_hook or sys.__excepthook__
        hook(exc_type, exc_value, exc_tb)

    def _log_restart_failure(self, error: BaseException) -> None:
        try:
            logging.getLogger(self.identity.package_name).critical(
                "Bootstrap fresh-process recovery failed: pid=%s "
                "phase=package-bootstrap error=%s: %s",
                os.getpid(),
                type(error).__name__,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
        except Exception:
            pass

    def _handle_unhandled(self, exc_type, exc_value, exc_tb) -> None:
        """Cleanup and replace the interpreter for one bootstrap crash."""

        if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            self._delegate(exc_type, exc_value, exc_tb)
            return

        with self._state_lock:
            if not self._active or self._handling:
                delegate = True
                cleanup = None
                runtime_seconds = 0.0
            else:
                delegate = False
                self._handling = True
                # Prevent recursive hook failures from scheduling another exec.
                self._active = False
                cleanup = self._cleanup
                runtime_seconds = max(0.0, time.monotonic() - self._started_at)
        if delegate:
            self._delegate(exc_type, exc_value, exc_tb)
            return

        logger = logging.getLogger(self.identity.package_name)
        sanitized_session = f"<deploy-root>/{self.identity.session_path.name}"
        try:
            logger.critical(
                "Package bootstrap crashed: pid=%s phase=package-bootstrap "
                "runtime=%.3fs session=%s error=%s: %s",
                os.getpid(),
                runtime_seconds,
                sanitized_session,
                exc_type.__name__,
                exc_value,
                exc_info=(exc_type, exc_value, exc_tb),
            )
        except Exception:
            pass
        try:
            traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.stderr)
        except Exception:
            pass

        if cleanup is not None:
            try:
                cleanup()
            except (KeyboardInterrupt, SystemExit) as terminal:
                self.instance_lock.release()
                self._delegate(type(terminal), terminal, terminal.__traceback__)
                return
            except BaseException as cleanup_error:
                # Cleanup is best effort at this pre-main boundary.  A broken
                # closer must not strand the process in the already-imported
                # interpreter that just failed.
                try:
                    logger.critical(
                        "Package bootstrap cleanup failed before restart: "
                        "pid=%s error=%s: %s",
                        os.getpid(),
                        type(cleanup_error).__name__,
                        cleanup_error,
                        exc_info=(
                            type(cleanup_error),
                            cleanup_error,
                            cleanup_error.__traceback__,
                        ),
                    )
                except Exception:
                    pass

        try:
            restart_plan = plan_crash_restart(runtime_seconds)
            try:
                logger.critical(
                    "Bootstrap fresh-process restart scheduled: pid=%s "
                    "attempt=%s base_delay=%.1fs jittered_delay=%.3fs",
                    os.getpid(),
                    restart_plan.attempt,
                    restart_plan.base_delay_seconds,
                    restart_plan.delay_seconds,
                )
            except Exception:
                pass
            time.sleep(restart_plan.delay_seconds)
            exec_fresh_process(
                self.identity.package_name,
                reason=f"bootstrap-crash-attempt-{restart_plan.attempt}",
            )
            raise RuntimeError("fresh-process exec helper returned unexpectedly")
        except (KeyboardInterrupt, SystemExit) as terminal:
            self.instance_lock.release()
            self._delegate(type(terminal), terminal, terminal.__traceback__)
        except BaseException as restart_error:
            # exec_fresh_process() releases at the last responsible moment;
            # this is idempotent when exec itself failed after that release.
            self.instance_lock.release()
            self._log_restart_failure(restart_error)
            self._delegate(exc_type, exc_value, exc_tb)
