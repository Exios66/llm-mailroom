"""One long-lived event loop per process for sync callers of the async storage.

Graph nodes, watcher threads and other synchronous code reach the async
SQLAlchemy storage through a bridge. That bridge used to be ``asyncio.run``:
a fresh event loop per call, and since ``storage.db._get_engine_state`` keeps
one engine per loop, a fresh engine (and fresh SQLite connections) per call.
At about 20 calls per document that dominated the per-document cost and grew
super-linearly with concurrency (see
``docs/reports/evaluations/2026-11-17-graph-throughput-baseline.md``).

``run_sync`` instead submits the work to a single daemon thread that runs one
loop for the life of the process, so every sync caller shares one engine and
session factory. The caller still blocks until its work finishes, still gets
the exception the coroutine raised, and still runs in a copy of its own
``contextvars`` context (what ``asyncio.run`` gave it).

Kill switch: ``MAILROOM_DB_SHARED_LOOP=0`` (or ``false``/``off``/``no``)
restores the old behaviour exactly: ``asyncio.run`` per call, or a fresh loop in
a helper thread when the caller's thread already runs a loop.

Calls made from the shared loop's own thread (a coroutine that calls sync
code that calls ``run_sync``) cannot block on that loop without deadlocking
it, so they fall back to a fresh loop in a helper thread, as before. That
protects one level of nesting only: code on that helper loop must not call
``run_sync`` again (none does today).

Coroutines passed to ``run_sync`` share one loop thread, so they must not
block it (no synchronous file locks, heavy CPU or blocking I/O between
awaits); such work stays on a private loop (see ``storage.warehouse``).
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import contextvars
import os
import threading
from typing import Any, Awaitable, Callable, TypeVar

import structlog

logger = structlog.get_logger(__name__)

T = TypeVar("T")

THREAD_NAME = "mailroom-storage-loop"
_ENV_SWITCH = "MAILROOM_DB_SHARED_LOOP"
_OFF_VALUES = {"0", "false", "off", "no"}
# Upper bound on how long process exit waits for the shared loop to dispose
# its engine and stop. Exit must never hang on a stuck database.
_EXIT_TIMEOUT_SECONDS = 2.0


def shared_loop_enabled() -> bool:
    """True unless ``MAILROOM_DB_SHARED_LOOP`` is 0/false/off/no."""
    return os.environ.get(_ENV_SWITCH, "").strip().lower() not in _OFF_VALUES


class _SharedLoop:
    """The loop, its thread and the pid that created them."""

    def __init__(self) -> None:
        self.pid = os.getpid()
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name=THREAD_NAME, daemon=True)
        self.thread.start()
        self.ready.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self.ready.set)
        try:
            self.loop.run_forever()
        finally:
            try:
                self.loop.close()
            except Exception:  # noqa: BLE001 - never raise out of a daemon thread
                pass

    def usable(self) -> bool:
        return (
            self.pid == os.getpid()
            and self.thread.is_alive()
            and not self.loop.is_closed()
        )

    def on_loop_thread(self) -> bool:
        return threading.get_ident() == self.thread.ident


_state_lock = threading.Lock()
_state: _SharedLoop | None = None


def _reset_after_fork() -> None:
    """In a forked child, drop the parent's loop (its fds and thread are not ours).

    The lock may have been held by another parent thread at fork time, so it
    is replaced rather than released.
    """
    global _state, _state_lock
    _state = None
    _state_lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


def _get_shared() -> _SharedLoop:
    global _state
    state = _state
    if state is not None and state.usable():
        return state
    with _state_lock:
        state = _state
        if state is None or not state.usable():
            # A different pid means a fork copied the parent's state: abandon
            # it (never close the parent's loop or touch its selector).
            state = _state = _SharedLoop()
        return state


def _run_legacy(coro: Awaitable[T], timeout: float | None) -> T:
    """The pre-bridge behaviour: asyncio.run per call, helper thread if a loop runs here."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # type: ignore[arg-type]
    # A loop is running in THIS thread; blocking on a future scheduled onto it
    # would deadlock, so run on a fresh loop in a helper thread. The ``with``
    # waits for the thread, so ``timeout`` only decides what is raised after.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result(timeout=timeout)  # type: ignore[arg-type]


class _Escaped(Exception):
    """Carries a KeyboardInterrupt/SystemExit from the loop thread to the caller."""

    def __init__(self, exc: BaseException) -> None:
        super().__init__(type(exc).__name__)
        self.exc = exc


def run_sync(work: Awaitable[T] | Callable[[], Awaitable[T]], *, timeout: float | None = None) -> T:
    """Run ``work`` to completion from sync code and return its result.

    ``work`` is a coroutine or a zero-argument callable that returns one. The
    caller blocks; exceptions raised by the coroutine are re-raised here.
    ``timeout`` (seconds) bounds the wait: on expiry the task is cancelled and
    ``TimeoutError`` is raised. ``None`` waits indefinitely.
    """
    # A coroutine object is not callable; a factory is.
    coro = work() if callable(work) else work
    if not shared_loop_enabled():
        return _run_legacy(coro, timeout)

    for attempt in (1, 2):
        shared = _get_shared()
        if shared.on_loop_thread():
            # Re-entrant call from the loop thread: blocking here would
            # deadlock the loop that has to run the work. Use a private loop.
            logger.debug("storage_loop_reentrant_fallback")
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result(timeout=timeout)  # type: ignore[arg-type]

        ctx = contextvars.copy_context()
        done: concurrent.futures.Future = concurrent.futures.Future()
        task_box: list[asyncio.Task] = []

        async def _guarded(inner=coro):
            # SystemExit/KeyboardInterrupt raised inside a task would escape
            # run_forever and kill the loop thread before the done-callback
            # runs, leaving the caller blocked forever. Hand them back instead.
            try:
                return await inner
            except (KeyboardInterrupt, SystemExit) as exc:
                raise _Escaped(exc) from None

        def _start() -> None:
            try:
                task = shared.loop.create_task(_guarded(), context=ctx)  # type: ignore[arg-type]
            except BaseException as exc:  # noqa: BLE001 - hand any failure back to the caller
                done.set_exception(exc)
                return
            task_box.append(task)

            def _finished(t: asyncio.Task) -> None:
                if done.done():
                    return
                if t.cancelled():
                    done.set_exception(asyncio.CancelledError())
                elif t.exception() is not None:
                    done.set_exception(t.exception())
                else:
                    done.set_result(t.result())

            task.add_done_callback(_finished)

        def _cancel() -> None:
            if task_box:
                task_box[0].cancel()

        try:
            shared.loop.call_soon_threadsafe(_start)
        except RuntimeError:
            # The loop was stopped between lookup and submit (process-exit
            # shutdown racing a daemon thread): retry once on a fresh loop.
            if attempt == 2:
                raise
            continue
        try:
            return done.result(timeout=timeout)
        except _Escaped as escaped:
            raise escaped.exc from None
        except concurrent.futures.TimeoutError:
            if done.done():
                raise  # the coroutine itself raised TimeoutError; pass it through
            shared.loop.call_soon_threadsafe(_cancel)
            raise TimeoutError(f"storage coroutine did not finish within {timeout} s") from None
        except BaseException:
            # KeyboardInterrupt etc. in the waiting caller: do not leave the work running.
            if not done.done():
                shared.loop.call_soon_threadsafe(_cancel)
            raise
    raise RuntimeError("unreachable")  # pragma: no cover


async def _dispose_engines() -> None:
    from storage.db import dispose_current_loop_engine

    await dispose_current_loop_engine()


def shutdown(timeout: float = _EXIT_TIMEOUT_SECONDS) -> None:
    """Best effort: dispose the shared loop's engine and stop the loop.

    Safe to call repeatedly and when no loop was ever started. Never raises.
    Blocks at most about ``2 * timeout`` seconds (engine dispose, then thread
    join). Callers still waiting on the loop when it stops are not woken: only
    call this at process exit or when no other thread uses the bridge.
    """
    global _state
    with _state_lock:
        state, _state = _state, None
    if state is None or state.pid != os.getpid() or not state.usable():
        return
    try:
        fut = asyncio.run_coroutine_threadsafe(_dispose_engines(), state.loop)
        try:
            fut.result(timeout=timeout)
        except Exception:  # noqa: BLE001 - exit must not fail on a stuck/failed dispose
            fut.cancel()
    except Exception:  # noqa: BLE001
        pass
    try:
        state.loop.call_soon_threadsafe(state.loop.stop)
        state.thread.join(timeout=timeout)
    except Exception:  # noqa: BLE001
        pass


atexit.register(shutdown)
