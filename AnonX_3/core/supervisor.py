import asyncio
import time
from collections.abc import Awaitable, Callable

from AnonX_3 import logger, tasks


CoroutineFactory = Callable[[], Awaitable[None]]


def _collect_owned_task_tree(roots: list[asyncio.Task]) -> list[asyncio.Task]:
    """Flatten only the supervisor-owned task trees without a global sweep."""
    current = asyncio.current_task()
    seen = {id(current)} if current is not None else set()
    collected: list[asyncio.Task] = []
    stack = list(roots)
    while stack:
        task = stack.pop()
        identity = id(task)
        if identity in seen:
            continue
        seen.add(identity)
        collected.append(task)
        for child in getattr(task, "_children", ()):
            if id(child) not in seen:
                stack.append(child)
    return collected


def _clear_own_children() -> None:
    """Cancel and clear child tasks of the currently-running task.

    Must be called from inside a running asyncio task.  Clears
    ``_children`` so downstream ``Task.cancel()`` cannot recurse into
    an already-cancelled subtree.
    """
    try:
        curr = asyncio.current_task()
        if curr is None:
            return
        children = list(getattr(curr, "_children", ()))
        # Clear before cancelling: prevents recursive cancel cascade.
        try:
            curr._children.clear()
        except Exception:
            pass
        for child in children:
            if not child.done():
                try:
                    child.cancel()
                except Exception:
                    pass
    except Exception:
        pass


class CriticalTaskSupervisor:
    """Keep essential background loops alive until application shutdown."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._restarts: dict[str, int] = {}
        self._stopping = False
        self._shutdown_complete = False
        self._shutdown_lock = asyncio.Lock()

    async def _runner(self, name: str, factory: CoroutineFactory) -> None:
        backoff = 1.0
        while not self._stopping:
            started = time.monotonic()
            try:
                await factory()
            except asyncio.CancelledError:
                # Clear children before re-raising so recursive cancel in
                # Python 3.12+ stop() never overflows the stack.
                _clear_own_children()
                raise
            except Exception:
                self._restarts[name] = self._restarts.get(name, 0) + 1
                logger.exception(
                    "Critical task %s crashed; restarting in %.1fs (restart=%s)",
                    name,
                    backoff,
                    self._restarts[name],
                )
            else:
                self._restarts[name] = self._restarts.get(name, 0) + 1
                logger.error(
                    "Critical task %s returned unexpectedly; restarting in %.1fs (restart=%s)",
                    name,
                    backoff,
                    self._restarts[name],
                )

            if self._stopping:
                return
            runtime = time.monotonic() - started
            if runtime >= 120:
                backoff = 1.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)

    def spawn(self, name: str, factory: CoroutineFactory) -> asyncio.Task:
        if self._stopping:
            raise RuntimeError("Critical task supervisor is shutting down")
        existing = self._tasks.get(name)
        if existing and not existing.done():
            return existing
        task = asyncio.create_task(
            self._runner(name, factory),
            name=f"critical:{name}",
        )
        self._tasks[name] = task
        tasks.append(task)

        def _cleanup(done: asyncio.Task) -> None:
            if self._tasks.get(name) is done:
                self._tasks.pop(name, None)
            try:
                tasks.remove(done)
            except ValueError:
                pass

        task.add_done_callback(_cleanup)
        return task

    async def shutdown(self) -> None:
        """Cancel, await, and forget only tasks owned by this supervisor."""
        async with self._shutdown_lock:
            if self._shutdown_complete:
                return

            self._stopping = True
            roots = list(self._tasks.values())
            self._tasks.clear()
            self._restarts.clear()

            root_ids = {id(task) for task in roots}
            tasks[:] = [task for task in tasks if id(task) not in root_ids]
            pending = [task for task in _collect_owned_task_tree(roots) if not task.done()]

            # Defuse Python 3.12+'s recursive Task.cancel() walk before
            # cancelling the flattened owned tree.
            for task in pending:
                children = getattr(task, "_children", None)
                if children is not None:
                    try:
                        children.clear()
                    except Exception:
                        pass
            for task in pending:
                task.cancel()

            results = []
            try:
                if pending:
                    results = await asyncio.gather(*pending, return_exceptions=True)
            finally:
                self._shutdown_complete = True

            failures = [
                result
                for result in results
                if isinstance(result, Exception)
                and not isinstance(result, asyncio.CancelledError)
            ]
            if failures:
                raise ExceptionGroup("Supervisor shutdown failed", failures)

    def status(self) -> dict[str, dict[str, int | bool]]:
        return {
            name: {
                "running": not task.done(),
                "restarts": self._restarts.get(name, 0),
            }
            for name, task in self._tasks.items()
        }


supervisor = CriticalTaskSupervisor()
