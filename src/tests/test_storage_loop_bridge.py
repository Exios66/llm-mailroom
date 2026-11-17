"""Sync callers of the async storage share ONE event loop, hence one engine.

Regression for the per-call ``asyncio.run`` bridge: every ``_run_coro`` used to
create a fresh loop, and ``storage.db`` keeps one engine per loop, so a document
paid for ~20 loops, ~20 engines and ~31 SQLite connections
(docs/reports/evaluations/2026-11-17-graph-throughput-baseline.md).
"""

import asyncio
import contextvars
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import text

from storage import db, loop_bridge

SRC = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolated_bridge(monkeypatch, tmp_path):
    """A private DB per test, and the module-global loop stopped around it so a
    test never inherits (or leaks) the shared loop thread."""
    monkeypatch.delenv("MAILROOM_DB_SHARED_LOOP", raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'bridge.db'}")
    loop_bridge.shutdown()
    yield
    loop_bridge.shutdown()


@pytest.fixture
def engines(monkeypatch):
    """Count (and keep) every engine ``storage.db`` creates."""
    created = []
    real = db.create_async_engine

    def counting(*args, **kwargs):
        engine = real(*args, **kwargs)
        created.append(engine)
        return engine

    monkeypatch.setattr(db, "create_async_engine", counting)
    return created


def _select_one():
    async def _q():
        async with db.async_session() as session:
            return (await session.execute(text("select 1"))).scalar_one()

    return _q


def test_run_coro_shares_one_engine_across_calls(engines):
    """The throughput regression: N sync calls -> 1 engine, not N."""
    from graph.build_graph import _run_coro

    for _ in range(12):
        assert _run_coro(_select_one()) == 1

    assert len(engines) == 1


def test_run_coro_shares_one_engine_across_threads(engines):
    from graph.build_graph import _run_coro

    errors = []

    def work():
        try:
            for _ in range(4):
                assert _run_coro(_select_one()) == 1
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert not errors
    assert len(engines) == 1


def test_calls_run_on_one_named_daemon_loop_thread():
    seen = []

    async def probe():
        seen.append((asyncio.get_running_loop(), threading.current_thread()))

    for _ in range(3):
        loop_bridge.run_sync(probe())

    assert len({id(loop) for loop, _ in seen}) == 1
    thread = seen[0][1]
    assert thread.name == loop_bridge.THREAD_NAME
    assert thread.daemon is True
    assert thread is not threading.current_thread()


def test_accepts_coroutine_object_or_factory():
    async def answer():
        return 42

    assert loop_bridge.run_sync(answer()) == 42
    assert loop_bridge.run_sync(answer) == 42


def test_exception_propagates_and_loop_stays_usable():
    class Boom(Exception):
        pass

    async def fail():
        raise Boom("bad write")

    with pytest.raises(Boom, match="bad write"):
        loop_bridge.run_sync(fail())
    assert loop_bridge.run_sync(_select_one()) == 1


def test_contextvars_of_the_caller_are_visible_and_not_written_back():
    var = contextvars.ContextVar("bridge_probe", default="unset")
    var.set("from-caller")

    async def read_then_write():
        seen = var.get()
        var.set("from-coroutine")
        return seen

    assert loop_bridge.run_sync(read_then_write()) == "from-caller"
    # asyncio.run ran the coroutine in a copy of the context; so does the bridge.
    assert var.get() == "from-caller"


def test_reentrant_call_from_the_loop_thread_does_not_deadlock():
    result = {}

    async def inner():
        return threading.current_thread().name

    async def outer():
        # A coroutine on the shared loop calling sync code that bridges again.
        return loop_bridge.run_sync(inner())

    def call():
        try:
            result["value"] = loop_bridge.run_sync(outer())
        except BaseException as exc:  # noqa: BLE001
            result["error"] = exc

    caller = threading.Thread(target=call, daemon=True)
    caller.start()
    caller.join(15)

    assert not caller.is_alive(), "re-entrant run_sync deadlocked the shared loop"
    assert "error" not in result, result.get("error")
    # The nested call ran on a fresh private loop, not on the (blocked) shared one.
    assert result["value"] != loop_bridge.THREAD_NAME


def test_caller_thread_with_its_own_running_loop():
    from graph.build_graph import _run_coro

    async def handler():
        # What an async API handler does when it reaches sync storage code.
        return _run_coro(_select_one()), loop_bridge.run_sync(_select_one())

    assert asyncio.run(handler()) == (1, 1)


def test_sixteen_concurrent_threads_all_succeed():
    results, errors = [], []
    lock = threading.Lock()

    async def work(i):
        await asyncio.sleep(0.01)
        async with db.async_session() as session:
            value = (await session.execute(text("select :i"), {"i": i})).scalar_one()
        return value

    def call(i):
        try:
            value = loop_bridge.run_sync(work(i))
            with lock:
                results.append(value)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=call, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert not errors
    assert sorted(results) == list(range(16))


def test_kill_switch_restores_one_loop_and_engine_per_call(monkeypatch, engines):
    monkeypatch.setenv("MAILROOM_DB_SHARED_LOOP", "0")
    caller = threading.current_thread()
    ran_in = []

    async def probe():
        ran_in.append(threading.current_thread())
        return await _select_one()()

    for _ in range(3):
        assert loop_bridge.run_sync(probe()) == 1

    assert ran_in == [caller] * 3  # asyncio.run in the caller's own thread
    assert len(engines) == 3  # a fresh loop, hence a fresh engine, per call
    assert loop_bridge._state is None  # the shared thread was never started
    assert not any(t.name == loop_bridge.THREAD_NAME for t in threading.enumerate())


@pytest.mark.parametrize("value", ["0", "false", "OFF", "no"])
def test_kill_switch_spellings(monkeypatch, value):
    monkeypatch.setenv("MAILROOM_DB_SHARED_LOOP", value)
    assert loop_bridge.shared_loop_enabled() is False


def test_kill_switch_with_a_running_loop_uses_a_helper_thread(monkeypatch):
    monkeypatch.setenv("MAILROOM_DB_SHARED_LOOP", "0")

    async def handler():
        return loop_bridge.run_sync(_select_one())

    assert asyncio.run(handler()) == 1


def test_pid_change_starts_a_new_loop_and_thread():
    first = loop_bridge._get_shared()
    first_loop, first_thread = first.loop, first.thread
    assert loop_bridge.run_sync(_select_one()) == 1

    first.pid = -1  # what a forked child sees: the state belongs to another pid
    try:
        assert loop_bridge.run_sync(_select_one()) == 1
        second = loop_bridge._get_shared()
        assert second is not first
        assert second.pid == os.getpid()
        assert second.loop is not first_loop
        assert second.thread is not first_thread
    finally:
        first_loop.call_soon_threadsafe(first_loop.stop)
        first_thread.join(5)


def test_dead_loop_thread_is_replaced():
    first = loop_bridge._get_shared()
    first.loop.call_soon_threadsafe(first.loop.stop)
    first.thread.join(5)
    assert not first.thread.is_alive()

    assert loop_bridge.run_sync(_select_one()) == 1
    assert loop_bridge._get_shared() is not first


def test_timeout_cancels_the_work_and_raises():
    cancelled = threading.Event()

    async def slow():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(TimeoutError, match="did not finish"):
        loop_bridge.run_sync(slow(), timeout=0.2)
    assert cancelled.wait(5)
    assert loop_bridge.run_sync(_select_one()) == 1


def test_a_timeout_error_raised_by_the_coroutine_is_passed_through():
    async def inner_timeout():
        raise TimeoutError("db said so")

    with pytest.raises(TimeoutError, match="db said so"):
        loop_bridge.run_sync(inner_timeout(), timeout=5)


def test_shutdown_is_idempotent_and_the_bridge_restarts_afterwards():
    loop_bridge.shutdown()  # nothing started yet
    assert loop_bridge.run_sync(_select_one()) == 1
    thread = loop_bridge._get_shared().thread

    loop_bridge.shutdown()
    loop_bridge.shutdown()
    assert not thread.is_alive()
    assert loop_bridge._state is None

    assert loop_bridge.run_sync(_select_one()) == 1
    assert loop_bridge._get_shared().thread is not thread


@pytest.fixture
def disposed(monkeypatch):
    """Record the URL of every AsyncEngine that gets disposed."""
    from sqlalchemy.ext.asyncio import AsyncEngine

    seen = []
    real = AsyncEngine.dispose

    async def spy(self, *args, **kwargs):
        seen.append(self.url.render_as_string(hide_password=False))
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(AsyncEngine, "dispose", spy)
    return seen


def test_shutdown_disposes_the_loops_engine(disposed):
    loop_bridge.run_sync(_select_one())
    assert disposed == []
    shared_loop = loop_bridge._get_shared().loop
    assert shared_loop in db._loop_states

    loop_bridge.shutdown()

    assert disposed == [os.environ["DATABASE_URL"]]
    assert shared_loop not in db._loop_states  # the stopped loop's engine state was forgotten
    assert not db._pending_disposals


def test_replaced_engine_is_disposed_on_the_long_lived_loop(monkeypatch, tmp_path, disposed):
    """A URL change (tests, MAILROOM_BASE_DIR) must not strand the old engine."""
    first = f"sqlite+aiosqlite:///{tmp_path / 'one.db'}"
    second = f"sqlite+aiosqlite:///{tmp_path / 'two.db'}"

    monkeypatch.setenv("DATABASE_URL", first)
    assert loop_bridge.run_sync(_select_one()) == 1
    assert disposed == []
    monkeypatch.setenv("DATABASE_URL", second)
    assert loop_bridge.run_sync(_select_one()) == 1

    # The dispose task runs on the shared loop; give it a turn to finish.
    deadline = time.monotonic() + 5
    while not disposed and time.monotonic() < deadline:
        loop_bridge.run_sync(asyncio.sleep(0.01))
    assert disposed == [first]


def test_process_exits_promptly_after_a_write(tmp_path):
    """The daemon loop thread and aiosqlite worker threads never hang exit."""
    script = textwrap.dedent(
        """
        from sqlalchemy import text
        from storage import db, loop_bridge

        async def write():
            async with db.async_session() as session:
                await session.execute(text("create table if not exists t (x integer)"))
                await session.execute(text("insert into t values (1)"))
                await session.commit()

        loop_bridge.run_sync(write())
        print("written")
        """
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'exit.db'}",
    }
    env.pop("MAILROOM_DB_SHARED_LOOP", None)
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert "written" in proc.stdout
    assert time.monotonic() - started < 30


def test_wired_call_sites_use_the_bridge():
    """The per-document bridges all go through ``run_sync`` (and so share the loop)."""
    import inspect

    from graph import build_graph
    from pipeline import relations
    from storage import warehouse

    for fn in (build_graph._run_coro, relations._run, warehouse._run_async):
        assert "run_sync" in inspect.getsource(fn), fn.__qualname__
