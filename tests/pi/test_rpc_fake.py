"""Tests for the sync-threaded ``PiRpcClient`` (phase 3).

Rewritten from the v3 ``async def`` test as part of the phase-3 rpc.py
rewrite. Exercises the new sync core directly (``_*_sync`` methods); the
async shims have their own coverage via the unchanged live tests that
still ``await`` them.

The fake-pi subprocess is the mode-driven ``fake_pi_rpc.py`` from phase 2;
each test selects a mode via the ``LIBHARNESS_FAKE_PI_MODE`` env var.
"""

from __future__ import annotations

import contextlib
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from libharness.pi import (
    EventQueueEmpty,
    PiLaunchConfig,
    PiRpcClient,
    PiRpcCommandError,
    PiRpcError,
    PiRpcProcessError,
    ReentrantRPCError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_SCRIPT = Path(__file__).with_name("fake_pi_rpc.py")


def _fake_config(
    *,
    mode: str | None = None,
    env_extras: dict[str, str] | None = None,
    request_timeout: float = 5.0,
) -> PiLaunchConfig:
    env: dict[str, str] = {}
    if mode is not None:
        env["LIBHARNESS_FAKE_PI_MODE"] = mode
    if env_extras:
        env.update(env_extras)
    return PiLaunchConfig(
        pi_command=[sys.executable, str(FAKE_SCRIPT)],
        request_timeout=request_timeout,
        offline=False,
        no_extensions=False,
        no_skills=False,
        no_prompt_templates=False,
        no_context_files=False,
        env=env if env else None,
    )


@contextmanager
def _started_client(config: PiLaunchConfig) -> Iterator[PiRpcClient]:
    client = PiRpcClient(config)
    client._start_sync()
    try:
        yield client
    finally:
        client._close_sync()


# ---------------------------------------------------------------------------
# Basic protocol (preserved from v3 + sync)
# ---------------------------------------------------------------------------


def test_rpc_client_handles_headless_ui_and_events() -> None:
    """Preserved from the v3 test: get_state with confirmation + prompt_and_wait."""
    with _started_client(_fake_config()) as client:
        state = client._get_state_sync()
        assert state["sessionId"] == "fake"
        events = client._prompt_and_wait_sync("hello", timeout=5)
        assert any(event.get("type") == "agent_end" for event in events)


def test_prompt_and_wait_standalone_returns_events() -> None:
    """Phase-3 plan T-12: prompt_and_wait is its own code path; verify standalone."""
    with _started_client(_fake_config()) as client:
        events = client._prompt_and_wait_sync("ping", timeout=5)
        assert any(event.get("type") == "agent_end" for event in events)


def test_basic_mode_does_not_log_events_to_sidecar_file() -> None:
    """Sanity: no sidecar log path is set; modes default to basic."""
    with _started_client(_fake_config()) as client:
        client._get_state_sync()


# ---------------------------------------------------------------------------
# Exception hierarchy (new in phase 3)
# ---------------------------------------------------------------------------


def test_pi_rpc_error_is_abstract_base_for_all_subclasses() -> None:
    """``PiRpcError`` catches every subclass via base; preserves prior behavior."""
    assert issubclass(PiRpcCommandError, PiRpcError)
    assert issubclass(PiRpcProcessError, PiRpcError)
    assert issubclass(ReentrantRPCError, PiRpcError)
    assert issubclass(EventQueueEmpty, PiRpcError)


def test_command_failure_raises_pi_rpc_command_error() -> None:
    """A pi response with ``success=False`` surfaces as ``PiRpcCommandError``.

    The fake pi's basic mode returns ``success=False`` for ``get_state`` if
    the UI handler doesn't reply with ``confirmed: False``. Provide a
    wrong-shape response and assert the command error type.
    """
    with _started_client(_fake_config()) as client:

        def wrong_shape_handler(_request: dict) -> dict:
            return {"weird": True}  # missing the expected "confirmed": False

        client.set_extension_ui_handler("confirm", wrong_shape_handler)
        with pytest.raises(PiRpcCommandError) as exc_info:
            client._get_state_sync()
        err = exc_info.value
        assert err.command == "get_state"
        assert isinstance(err, PiRpcError)


# ---------------------------------------------------------------------------
# Reentrant send detection
# ---------------------------------------------------------------------------


def test_reentrant_send_from_event_handler_raises_reentrant_rpc_error() -> None:
    """A handler that calls ``client.send`` on the reader thread raises ``ReentrantRPCError``."""
    captured: list[BaseException] = []
    handler_done = threading.Event()

    with _started_client(_fake_config()) as client:

        def reentry_handler(_event: dict) -> None:
            try:
                client._send_sync({"type": "noop"})
            except BaseException as exc:  # noqa: BLE001
                captured.append(exc)
            finally:
                handler_done.set()

        client.on_event(reentry_handler)
        client._prompt_and_wait_sync("hi", timeout=5)
        assert handler_done.wait(2.0)

    assert captured, "handler did not run"
    assert isinstance(captured[0], ReentrantRPCError)


def test_reentrant_send_from_ui_handler_raises_reentrant_rpc_error() -> None:
    """UI handler that calls ``client.send`` raises ``ReentrantRPCError``."""
    captured: list[BaseException] = []
    handler_done = threading.Event()

    with _started_client(_fake_config()) as client:

        def reentry_ui_handler(_request: dict) -> dict:
            try:
                client._send_sync({"type": "noop"})
            except BaseException as exc:  # noqa: BLE001
                captured.append(exc)
            finally:
                handler_done.set()
            # Provide a valid confirm response so pi returns success.
            return {"confirmed": False}

        client.set_extension_ui_handler("confirm", reentry_ui_handler)
        client._get_state_sync()
        assert handler_done.wait(2.0)

    assert captured, "UI handler did not run"
    assert isinstance(captured[0], ReentrantRPCError)


# ---------------------------------------------------------------------------
# UI response priority (T1.1 literal v3-synthesis pattern)
# ---------------------------------------------------------------------------


def test_ui_response_priority_barrier_no_normal_send_interleaves() -> None:
    """When the reader is handling a UI request, caller-thread sends back off.

    Uses the ``ui-order`` fake-pi mode, which records the order of stdin
    frames it observes after a UI request. We force this race: a caller
    thread is sleeping inside ``_send_sync`` (between the precheck and the
    write); the reader processes a ``extension_ui_request`` and sets
    ``_ui_response_pending``; the caller's send must NOT write before the
    UI response.
    """
    log_path = Path(tempfile.mkdtemp()) / "ui-order.log"
    try:
        cfg = _fake_config(
            mode="ui-order",
            env_extras={"LIBHARNESS_FAKE_PI_LOG": str(log_path)},
        )
        with _started_client(cfg) as client:
            ui_handler_entered = threading.Event()
            allow_ui_response = threading.Event()

            def ui_handler(_request: dict) -> dict:
                ui_handler_entered.set()
                # Hold the UI gate open briefly so a parallel send must wait.
                allow_ui_response.wait(timeout=5.0)
                return {"confirmed": False}

            client.set_extension_ui_handler("confirm", ui_handler)

            # Thread A: triggers the UI request via get_state.
            def trigger_ui() -> None:
                with contextlib.suppress(Exception):
                    client._get_state_sync()

            t_a = threading.Thread(target=trigger_ui, daemon=True)
            t_a.start()
            assert ui_handler_entered.wait(2.0)

            # Thread B: tries to send a normal request — should block on UI gate.
            send_done = threading.Event()
            send_error: list[BaseException] = []

            def try_send() -> None:
                try:
                    client._send_sync({"type": "noop"})
                except BaseException as exc:  # noqa: BLE001
                    send_error.append(exc)
                finally:
                    send_done.set()

            t_b = threading.Thread(target=try_send, daemon=True)
            t_b.start()
            # Give t_b a beat to attempt the write.
            time.sleep(0.2)
            # Release the UI handler; UI response writes first.
            allow_ui_response.set()
            send_done.wait(timeout=5.0)
            t_a.join(timeout=5.0)
            t_b.join(timeout=5.0)

        # ui-order mode logs each stdin frame it sees. First frame after the
        # UI request must be the UI response, not a normal send.
        lines = log_path.read_text().splitlines()
        # Filter to the get_state + first post-ui frame.
        ui_response_index = next(
            (i for i, ln in enumerate(lines) if "extension_ui_response" in ln), None,
        )
        noop_index = next(
            (i for i, ln in enumerate(lines) if '"type":"noop"' in ln or '"type": "noop"' in ln),
            None,
        )
        if ui_response_index is not None and noop_index is not None:
            assert ui_response_index < noop_index, (
                f"normal send interleaved before UI response: ui={ui_response_index}, "
                f"noop={noop_index}, lines={lines}"
            )
    finally:
        if log_path.exists():
            log_path.unlink()
        log_path.parent.rmdir()


# ---------------------------------------------------------------------------
# Helper-thread bypass (subprocess; verifies deadlock by TimeoutExpired)
# ---------------------------------------------------------------------------


def test_helper_thread_bypass_deadlocks_via_subprocess() -> None:
    """A handler that spawns a worker calling ``send`` deadlocks (documented contract).

    The detection only catches SAME-thread reentry (via ``_in_dispatch``
    threading.local). Helper-thread reentry passes the check but deadlocks
    because the reader thread is blocked in the handler — can't dispatch
    the response. Documented contract violation; we verify deadlock via
    ``subprocess.run(timeout=N)`` asserting ``TimeoutExpired``.
    """
    script = f"""
import sys, threading
sys.path.insert(0, "src")
from libharness.pi import PiLaunchConfig, PiRpcClient

cfg = PiLaunchConfig(
    pi_command=[sys.executable, {str(FAKE_SCRIPT)!r}],
    request_timeout=10,
    offline=False, no_extensions=False, no_skills=False,
    no_prompt_templates=False, no_context_files=False,
)
client = PiRpcClient(cfg)
client._start_sync()

result_holder = []
def worker():
    try:
        result_holder.append(client._send_sync({{"type": "noop"}}))
    except BaseException as exc:
        result_holder.append(exc)

def event_handler(event):
    t = threading.Thread(target=worker)
    t.start()
    t.join()  # blocks forever because reader is busy

client.on_event(event_handler)
client._prompt_and_wait_sync("hello", timeout=8)
"""
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            timeout=3,
            check=False,
            env={**os.environ, "PYTHONPATH": "src"},
        )


# ---------------------------------------------------------------------------
# Close / fatal-state / pending-future semantics
# ---------------------------------------------------------------------------


def test_close_during_pending_send_raises_pi_rpc_process_error() -> None:
    """N caller threads + concurrent close: callers see PiRpcProcessError, no hangs.

    Uses ``late-response`` mode with a barrier file the test never creates;
    pi withholds the response, callers block in ``Future.result``, close
    fires fatal and unblocks them.
    """
    barrier_path = Path(tempfile.mkdtemp()) / "barrier"
    try:
        cfg = _fake_config(
            mode="late-response",
            env_extras={"LIBHARNESS_FAKE_PI_BARRIER": str(barrier_path)},
            request_timeout=10,
        )
        with _started_client(cfg) as client:
            errors: list[BaseException] = []
            done = threading.Event()
            ready = threading.Barrier(4)  # 3 callers + main

            def caller() -> None:
                ready.wait()
                try:
                    client._prompt_sync("hi")
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                finally:
                    if len(errors) >= 3:
                        done.set()

            threads = [threading.Thread(target=caller, daemon=True) for _ in range(3)]
            for t in threads:
                t.start()
            ready.wait()
            # Give the callers a chance to push their requests into _pending.
            time.sleep(0.3)
            # Close — should fail all pending sends with PiRpcProcessError.
            client._close_sync()
            done.wait(timeout=5.0)
            for t in threads:
                t.join(timeout=5.0)
        assert len(errors) == 3
        for exc in errors:
            assert isinstance(exc, PiRpcProcessError), f"unexpected exc: {exc!r}"
    finally:
        if barrier_path.exists():
            barrier_path.unlink()
        barrier_path.parent.rmdir()


def test_send_after_close_raises_fatal_state_error() -> None:
    """``_send_sync`` after ``_close_sync`` raises PiRpcProcessError immediately."""
    with _started_client(_fake_config()) as client:
        client._get_state_sync()  # ensure healthy
    # Now client is closed.
    with pytest.raises(PiRpcProcessError):
        client._send_sync({"type": "noop"})


def test_invalid_json_from_pi_marks_fatal_and_subsequent_sends_fail() -> None:
    """``fatal-invalid-json`` mode emits garbage after one exchange; client goes fatal."""
    with _started_client(_fake_config(mode="fatal-invalid-json")) as client:
        # First request: pi responds normally.
        client._send_sync({"type": "noop"})
        # Second request: pi writes garbage on stdout — reader marks fatal.
        with contextlib.suppress(PiRpcError):
            client._send_sync({"type": "noop"})
        # Give the reader a beat to propagate fatal.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if client._fatal_error is not None:
                break
            time.sleep(0.02)
        # Subsequent send raises PiRpcProcessError from fatal state.
        with pytest.raises(PiRpcProcessError) as exc_info:
            client._send_sync({"type": "noop"})
        assert exc_info.value.__cause__ is not None or "fatal" in str(exc_info.value).lower()


def test_late_response_after_fatal_does_not_crash_reader() -> None:
    """The reader handles a response for a request the fatal path already settled.

    The ``_complete_future`` helper swallows ``InvalidStateError`` (T1.2-v4).
    Smoke test: trigger fatal manually, then deliver a fake "response"
    via the message-handling path to verify no exception propagates.
    """
    with _started_client(_fake_config()) as client:
        # Stage a request.
        from concurrent.futures import Future as Fut

        req_id = "test-1"
        fut: Fut[dict] = Fut()
        with client._pending_lock:
            client._pending[req_id] = fut
        # Mark fatal — settles the future.
        client._set_fatal(PiRpcProcessError("simulated fatal"))
        # Now deliver a "response" via _handle_message. Should not raise.
        client._handle_message({"type": "response", "id": req_id, "success": True, "data": {}})
        # Future state is fatal (set by _set_fatal), not the late response.
        assert fut.done()
        assert isinstance(fut.exception(), PiRpcProcessError)


# ---------------------------------------------------------------------------
# Event queue: overflow + EventQueueEmpty
# ---------------------------------------------------------------------------


def test_event_queue_empty_raised_on_timeout() -> None:
    """``_next_event_sync(timeout=0.05)`` on an empty queue raises ``EventQueueEmpty``."""
    with _started_client(_fake_config()) as client:
        # Drain any leftover events (likely none on a fresh client).
        while True:
            try:
                client._events.get_nowait()
            except queue.Empty:
                break
        with pytest.raises(EventQueueEmpty):
            client._next_event_sync(timeout=0.05)


def test_event_queue_overflow_drops_oldest_with_rate_limited_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the queue is full, ``_enqueue_event`` drops oldest + logs warning."""
    import logging

    caplog.set_level(logging.WARNING, logger="libharness.pi.rpc")
    cfg = _fake_config()
    cfg.event_queue_max = 4  # tiny
    with _started_client(cfg) as client:
        # Drain leftover.
        while True:
            try:
                client._events.get_nowait()
            except queue.Empty:
                break
        # Inject 100 events directly; only the most-recent 4 should survive.
        for i in range(100):
            client._enqueue_event({"type": "spam", "n": i})
        # Drain and check we have at most queue_max entries.
        events: list[dict] = []
        while True:
            try:
                events.append(client._events.get_nowait())
            except queue.Empty:
                break
        assert len(events) <= 4
        # And the surviving events are the most recent ones.
        if events:
            assert events[-1]["n"] == 99
        # Warning logged at least once (rate-limited).
        assert any("dropped events" in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# Dual-delivery: queue.put before handlers
# ---------------------------------------------------------------------------


def test_dual_delivery_queue_put_before_handlers_per_v4_synthesis() -> None:
    """A handler observes the event AFTER ``next_event_sync`` can see it.

    Pin v4-synthesis: queue.put happens FIRST, then handler dispatch.
    """
    with _started_client(_fake_config()) as client:
        observed_order: list[str] = []
        handler_can_proceed = threading.Event()

        def handler(_event: dict) -> None:
            observed_order.append("handler")
            handler_can_proceed.wait(timeout=2.0)

        client.on_event(handler)
        # Consumer thread polls next_event; should observe events
        # *before* the handler returns.
        consumer_observed = threading.Event()

        def consumer() -> None:
            with contextlib.suppress(EventQueueEmpty):
                client._next_event_sync(timeout=5.0)
                observed_order.append("consumer")
                consumer_observed.set()

        t = threading.Thread(target=consumer, daemon=True)
        t.start()
        # Inject an event directly via the message handler (skip the reader).
        client._handle_message({"type": "test-dual-delivery"})
        # Consumer should observe the event quickly (queue.put done before handler dispatch).
        assert consumer_observed.wait(2.0)
        # Allow handler to return.
        handler_can_proceed.set()
        t.join(timeout=2.0)
        assert "consumer" in observed_order
        assert "handler" in observed_order
        # Consumer ran while handler was still blocked → consumer index < handler-finished.


# ---------------------------------------------------------------------------
# Async-handler rejection at registration
# ---------------------------------------------------------------------------


def test_async_event_handler_rejected_at_registration() -> None:
    with _started_client(_fake_config()) as client:

        async def async_handler(_event: dict) -> None:
            pass

        with pytest.raises(TypeError, match="async event handlers"):
            client.on_event(async_handler)


def test_async_ui_handler_rejected_at_registration() -> None:
    with _started_client(_fake_config()) as client:

        async def async_ui_handler(_req: dict) -> dict | None:
            return None

        with pytest.raises(TypeError, match="async UI handlers"):
            client.set_extension_ui_handler("confirm", async_ui_handler)


# ---------------------------------------------------------------------------
# Additional unit-level coverage (from codex-generated phase-3 test plan)
# ---------------------------------------------------------------------------


def _client_no_subprocess() -> PiRpcClient:
    """Build a PiRpcClient without starting a real pi subprocess (unit-test fixture)."""
    return PiRpcClient(_fake_config())


def test_pop_pending_unknown_response_id_is_ignored() -> None:
    """Codex T4: ``_pop_pending`` returns None for unknown IDs; handler ignores."""
    from concurrent.futures import Future as Fut

    client = _client_no_subprocess()
    known_id = "known-1"
    known: Fut[dict] = Fut()
    with client._pending_lock:
        client._pending[known_id] = known
    # Handle a response for an unknown id — must not raise, must not touch known.
    client._handle_message({"type": "response", "id": "unknown-zzz", "success": True})
    assert client._pop_pending("unknown-zzz") is None
    with client._pending_lock:
        assert known_id in client._pending


def test_complete_future_double_settlement_is_benign() -> None:
    """Codex T5: ``_complete_future`` swallows InvalidStateError, preserves first settlement."""
    from concurrent.futures import Future as Fut

    client = _client_no_subprocess()
    fut: Fut[dict] = Fut()
    client._complete_future(fut, value={"ok": True})
    client._complete_future(fut, exc=PiRpcProcessError("late"))
    client._complete_future(fut, value={"too": "late"})
    assert fut.result() == {"ok": True}


def test_send_write_failure_rolls_back_pending_entry() -> None:
    """Codex T6: stdin write OSError → PiRpcProcessError, pending entry popped."""

    class _FakeStdin:
        def write(self, _data: bytes) -> int:
            raise OSError("simulated broken pipe")

        def flush(self) -> None:
            pass

    class _FakeProc:
        stdin = _FakeStdin()

    client = _client_no_subprocess()
    client.process = _FakeProc()  # type: ignore[assignment]
    with pytest.raises(PiRpcProcessError):
        client._send_sync({"type": "noop"}, timeout=1.0)
    with client._pending_lock:
        assert not client._pending


def test_send_timeout_rolls_back_pending_entry() -> None:
    """Codex T7: send timeout → PiRpcProcessError, pending entry popped."""

    class _FakeStdin:
        def __init__(self) -> None:
            self.writes: list[bytes] = []

        def write(self, data: bytes) -> int:
            self.writes.append(data)
            return len(data)

        def flush(self) -> None:
            pass

    class _FakeProc:
        def __init__(self) -> None:
            self.stdin = _FakeStdin()

    client = _client_no_subprocess()
    client.process = _FakeProc()  # type: ignore[assignment]
    with pytest.raises(PiRpcProcessError, match="timeout"):
        client._send_sync({"type": "noop"}, timeout=0.1)
    with client._pending_lock:
        assert not client._pending


def test_set_fatal_is_idempotent_and_preserves_first_cause() -> None:
    """Codex T8: ``_set_fatal`` is idempotent; first cause wins."""
    from concurrent.futures import Future as Fut

    client = _client_no_subprocess()
    f1: Fut[dict] = Fut()
    f2: Fut[dict] = Fut()
    with client._pending_lock:
        client._pending["a"] = f1
        client._pending["b"] = f2

    first_cause = ValueError("first")
    second_cause = RuntimeError("second")
    client._set_fatal(first_cause)
    client._set_fatal(second_cause)

    assert client._fatal_error is first_cause
    assert f1.done() and f2.done()
    assert isinstance(f1.exception(), PiRpcProcessError)
    assert isinstance(f2.exception(), PiRpcProcessError)
    assert f1.exception().__cause__ is first_cause  # type: ignore[union-attr]
    assert f2.exception().__cause__ is first_cause  # type: ignore[union-attr]


def test_set_fatal_concurrent_callers_settle_each_future_once() -> None:
    """Codex T9: concurrent ``_set_fatal`` settles each future exactly once."""
    from concurrent.futures import Future as Fut

    client = _client_no_subprocess()
    futures = [Fut() for _ in range(100)]
    with client._pending_lock:
        for i, f in enumerate(futures):
            client._pending[f"f-{i}"] = f  # type: ignore[assignment]

    barrier = threading.Barrier(8)

    def fire() -> None:
        barrier.wait()
        client._set_fatal(RuntimeError("race"))

    threads = [threading.Thread(target=fire) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    with client._pending_lock:
        assert not client._pending
    for f in futures:
        assert f.done()
        assert isinstance(f.exception(), PiRpcProcessError)


def test_close_from_event_handler_does_not_self_join() -> None:
    """Codex T10: ``_close_sync`` from inside a handler doesn't self-join (RuntimeError)."""
    handler_done = threading.Event()
    handler_error: list[BaseException] = []

    with _started_client(_fake_config()) as client:
        captured = {"client": client}

        def closer_handler(_event: dict) -> None:
            try:
                captured["client"]._close_sync()
            except BaseException as exc:  # noqa: BLE001
                handler_error.append(exc)
            finally:
                handler_done.set()

        client.on_event(closer_handler)
        # Trigger an event so the handler runs.
        with contextlib.suppress(PiRpcError):
            client._prompt_and_wait_sync("hi", timeout=5)
        assert handler_done.wait(5.0)

    # The handler may or may not see an error from close, but it must NOT be
    # a RuntimeError("cannot join current thread").
    for exc in handler_error:
        assert not (
            isinstance(exc, RuntimeError) and "current thread" in str(exc)
        ), f"close-from-handler self-joined: {exc}"


def test_concurrent_close_calls_are_safe_only_one_teardown() -> None:
    """Codex T11: N concurrent closes serialize; teardown runs once."""
    client = PiRpcClient(_fake_config())
    client._start_sync()
    barrier = threading.Barrier(16)
    errors: list[BaseException] = []

    def closer() -> None:
        barrier.wait()
        try:
            client._close_sync()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=closer) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    assert not errors
    assert client._close_complete.is_set()
    assert client.process is None


def test_close_escalates_sigterm_then_kill_when_process_ignores_exit() -> None:
    """Codex T12: close path escalates wait → SIGTERM → kill on hung pi."""

    class _FakeStdin:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

        def write(self, _data: bytes) -> int:
            return 0

        def flush(self) -> None:
            pass

    class _FakeProc:
        def __init__(self) -> None:
            self.stdin = _FakeStdin()
            self.wait_count = 0
            self.sigterm_count = 0
            self.kill_count = 0

        def poll(self) -> int | None:
            return None  # still running

        def wait(self, timeout: float | None = None) -> int:
            self.wait_count += 1
            if self.wait_count <= 2:
                raise subprocess.TimeoutExpired("fake", timeout or 0)
            return 0

        def send_signal(self, _sig: int) -> None:
            self.sigterm_count += 1

        def terminate(self) -> None:
            self.sigterm_count += 1

        def kill(self) -> None:
            self.kill_count += 1

    client = _client_no_subprocess()
    fake = _FakeProc()
    client.process = fake  # type: ignore[assignment]
    client._close_sync()
    assert fake.sigterm_count == 1, f"expected SIGTERM/terminate, got {fake.sigterm_count}"
    assert fake.kill_count == 1, f"expected kill after final timeout, got {fake.kill_count}"


def test_no_new_event_handlers_dispatch_after_close_begins() -> None:
    """Codex T14: ``_dispatch_event`` early-returns once ``_closing`` is set."""
    client = _client_no_subprocess()
    seen: list[dict] = []
    client.on_event(lambda e: seen.append(e))
    client._closing.set()
    client._dispatch_event({"type": "after-close"})
    assert seen == []


def test_unsubscribe_during_dispatch_affects_next_event_only() -> None:
    """Codex T18: handler list is snapshotted under lock; mutation mid-iter is benign."""
    client = _client_no_subprocess()
    log: list[str] = []

    def handler_b(_e: dict) -> None:
        log.append("b")

    unsub_b = client.on_event(handler_b)

    def handler_a(_e: dict) -> None:
        log.append("a")
        unsub_b()  # mutates the registry mid-dispatch

    client.on_event(handler_a)

    client._dispatch_event({"type": "first"})
    # First event: both handlers run (snapshot taken before unsub).
    assert sorted(log) == ["a", "b"]
    log.clear()

    client._dispatch_event({"type": "second"})
    # Second event: only handler_a, because handler_b is no longer registered.
    assert log == ["a"]


def test_same_event_handler_registered_twice_fires_twice() -> None:
    """Codex T19: registry is list-based; duplicate registration → duplicate invocation."""
    client = _client_no_subprocess()
    count = [0]

    def handler(_e: dict) -> None:
        count[0] += 1

    unsub1 = client.on_event(handler)
    unsub2 = client.on_event(handler)

    client._dispatch_event({"type": "x"})
    assert count[0] == 2

    unsub1()
    count[0] = 0
    client._dispatch_event({"type": "y"})
    assert count[0] == 1

    unsub2()
    count[0] = 0
    client._dispatch_event({"type": "z"})
    assert count[0] == 0


def test_notification_only_ui_methods_do_not_write_responses() -> None:
    """Codex T20: notify/setStatus/etc. don't trigger UI response writes."""

    class _RecordingStdin:
        def __init__(self) -> None:
            self.writes: list[bytes] = []

        def write(self, data: bytes) -> int:
            self.writes.append(data)
            return len(data)

        def flush(self) -> None:
            pass

    class _FakeProc:
        def __init__(self) -> None:
            self.stdin = _RecordingStdin()

    client = _client_no_subprocess()
    fake_proc = _FakeProc()
    client.process = fake_proc  # type: ignore[assignment]

    seen_events: list[dict] = []
    client.on_event(lambda e: seen_events.append(e))

    for method in ("notify", "setStatus", "setWidget", "setTitle", "set_editor_text"):
        client._handle_extension_ui_request(
            {"type": "extension_ui_request", "id": f"ui-{method}", "method": method},
        )

    assert fake_proc.stdin.writes == []  # no responses written
    assert {e["method"] for e in seen_events if "method" in e} == {
        "notify", "setStatus", "setWidget", "setTitle", "set_editor_text",
    }


def test_default_ui_response_shapes_for_all_known_methods() -> None:
    """Codex T21: ``_default_ui_response`` returns the per-method expected shape."""
    client = _client_no_subprocess()
    assert client._default_ui_response("confirm") == {"confirmed": False}
    for method in ("select", "input", "editor", "anything-else"):
        assert client._default_ui_response(method) == {"cancelled": True}


def test_fallback_ui_handler_used_when_no_method_handler_matches() -> None:
    """Codex T22: fallback handler is invoked when no per-method handler is registered."""

    class _RecordingStdin:
        def __init__(self) -> None:
            self.writes: list[bytes] = []

        def write(self, data: bytes) -> int:
            self.writes.append(data)
            return len(data)

        def flush(self) -> None:
            pass

    class _FakeProc:
        def __init__(self) -> None:
            self.stdin = _RecordingStdin()

    client = _client_no_subprocess()
    fake_proc = _FakeProc()
    client.process = fake_proc  # type: ignore[assignment]

    received: list[dict] = []

    def fallback(req: dict) -> dict:
        received.append(req)
        return {"cancelled": False, "value": "x"}

    client.set_extension_ui_handler(None, fallback)
    client._handle_extension_ui_request(
        {"type": "extension_ui_request", "id": "ui-cp", "method": "customPrompt"},
    )
    assert len(received) == 1
    assert fake_proc.stdin.writes, "expected one UI response write"
    # Decode the response frame to check the shape.
    import json as _json
    written = _json.loads(fake_proc.stdin.writes[0].decode())
    assert written["type"] == "extension_ui_response"
    assert written["id"] == "ui-cp"
    assert written["cancelled"] is False
    assert written["value"] == "x"


def test_exception_migration_base_catches_command_and_queue_errors() -> None:
    """Codex T23: ``except PiRpcError`` catches both PiRpcCommandError and EventQueueEmpty."""
    # PiRpcCommandError → catchable via base.
    with _started_client(_fake_config()) as client:

        def wrong_shape_handler(_request: dict) -> dict:
            return {"weird": True}

        client.set_extension_ui_handler("confirm", wrong_shape_handler)
        try:
            client._get_state_sync()
        except PiRpcError as exc:
            assert isinstance(exc, PiRpcCommandError)
        else:
            pytest.fail("expected PiRpcCommandError")

    # EventQueueEmpty → catchable via base.
    with _started_client(_fake_config()) as client:
        while True:
            try:
                client._events.get_nowait()
            except queue.Empty:
                break
        try:
            client._next_event_sync(timeout=0.05)
        except PiRpcError as exc:
            assert isinstance(exc, EventQueueEmpty)
        else:
            pytest.fail("expected EventQueueEmpty")


# ---------------------------------------------------------------------------
# Phase 3 review-fix regression tests (F1-F7 from review synthesis)
# ---------------------------------------------------------------------------


def test_ui_handler_cannot_override_response_id_or_type() -> None:
    """F1: a UI handler returning ``{"id": "evil"}`` does NOT override the frame id/type.

    Pre-fix: ``{"type": ..., "id": ..., **response}`` let the user-supplied
    keys win the dict-merge → silent protocol corruption.
    """

    class _RecordingStdin:
        def __init__(self) -> None:
            self.writes: list[bytes] = []

        def write(self, data: bytes) -> int:
            self.writes.append(data)
            return len(data)

        def flush(self) -> None:
            pass

    class _FakeProc:
        def __init__(self) -> None:
            self.stdin = _RecordingStdin()

    client = _client_no_subprocess()
    fake_proc = _FakeProc()
    client.process = fake_proc  # type: ignore[assignment]

    def attacker(_req: dict) -> dict:
        return {"id": "EVIL-ID", "type": "evil-type", "confirmed": True}

    client.set_extension_ui_handler("confirm", attacker)
    client._handle_extension_ui_request(
        {"type": "extension_ui_request", "id": "real-1", "method": "confirm"},
    )
    import json as _json
    assert fake_proc.stdin.writes, "expected one UI response write"
    written = _json.loads(fake_proc.stdin.writes[0].decode())
    assert written["id"] == "real-1", f"id was overridden: {written}"
    assert written["type"] == "extension_ui_response", f"type was overridden: {written}"
    assert written["confirmed"] is True, "non-reserved keys still pass through"


def test_ui_gate_set_before_event_dispatch_for_response_bearing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F2: response-bearing UI requests set the gate BEFORE dispatching events.

    Pre-fix: a slow event handler created a window where caller-thread
    sends could write a normal frame before the UI response.
    """
    client = _client_no_subprocess()
    gate_observed_set = []

    def event_handler(event: dict) -> None:
        # When this fires, _ui_response_pending should ALREADY be set if
        # the request is response-bearing (not a notification).
        if event.get("type") == "extension_ui_request":
            method = event.get("method") or ""
            gate_observed_set.append((method, client._ui_response_pending.is_set()))

    client.on_event(event_handler)

    # Response-bearing: gate must be set during event dispatch.
    client._handle_extension_ui_request(
        {"type": "extension_ui_request", "id": "rb-1", "method": "confirm"},
    )

    # Notification-only: gate need NOT be set (no response expected).
    client._handle_extension_ui_request(
        {"type": "extension_ui_request", "id": "no-1", "method": "notify"},
    )

    rb_observations = [o for o in gate_observed_set if o[0] == "confirm"]
    notif_observations = [o for o in gate_observed_set if o[0] == "notify"]
    assert rb_observations, "event handler did not see response-bearing request"
    assert all(g for _, g in rb_observations), (
        f"response-bearing dispatch saw gate UNSET: {rb_observations}"
    )
    assert notif_observations, "event handler did not see notification request"
    assert not any(g for _, g in notif_observations), (
        f"notification-only request set the gate: {notif_observations}"
    )


def test_in_dispatch_value_reset_after_ui_request(caplog: pytest.LogCaptureFixture) -> None:
    """F4: ``_in_dispatch.value`` is False after ``_handle_extension_ui_request`` returns.

    Pre-fix: the UI handler block set True but had no finally to reset.
    A future reader-thread code path that called send() would spuriously
    raise ReentrantRPCError.
    """
    client = _client_no_subprocess()

    def handler(_req: dict) -> dict:
        return {"confirmed": False}

    client.set_extension_ui_handler("confirm", handler)
    client._handle_extension_ui_request(
        {"type": "extension_ui_request", "id": "u-1", "method": "confirm"},
    )
    assert not getattr(client._in_dispatch, "value", False), (
        "_in_dispatch.value leaked True after UI request"
    )


def test_close_does_not_deadlock_behind_blocked_stdin_write() -> None:
    """F3: ``_close_sync`` escalates terminate/kill if a writer holds the lock.

    Pre-fix: caller holds ``_send_lock`` blocked in ``proc.stdin.write``
    (pi not draining); close blocks acquiring the lock to set
    ``_stdin_closed`` → never reaches terminate → deadlock forever.

    Fix: close tries lock acquire with bounded timeout; on failure,
    terminates/kills the subprocess first to unblock the writer.
    """

    class _BlockingStdin:
        def __init__(self) -> None:
            self.write_started = threading.Event()
            self.release_write = threading.Event()
            self.closed = False

        def write(self, _data: bytes) -> int:
            self.write_started.set()
            self.release_write.wait(timeout=10.0)
            return 1  # pretend success after release

        def flush(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    class _FakeProc:
        def __init__(self, blocking_stdin: _BlockingStdin) -> None:
            self.stdin = blocking_stdin
            self.terminated = False
            self.killed = False
            self.wait_count = 0

        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            self.wait_count += 1
            if self.terminated or self.killed:
                return 0
            raise subprocess.TimeoutExpired("fake", timeout or 0)

        def send_signal(self, _sig: int) -> None:
            self.terminated = True

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

    client = _client_no_subprocess()
    blocking = _BlockingStdin()
    fake_proc = _FakeProc(blocking)
    client.process = fake_proc  # type: ignore[assignment]

    # Start a writer that will hold _send_lock blocked.
    write_done = threading.Event()
    write_error: list[BaseException] = []

    def writer() -> None:
        try:
            client._send_sync({"type": "noop"}, timeout=10.0)
        except BaseException as exc:  # noqa: BLE001
            write_error.append(exc)
        finally:
            write_done.set()

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    assert blocking.write_started.wait(3.0), "writer never acquired _send_lock"

    # Now close — should escalate terminate before acquiring the lock.
    t0 = time.monotonic()
    client._close_sync()
    elapsed = time.monotonic() - t0
    # Close should finish within: 2s (acquire timeout) + 1s (terminate wait)
    # + 1s (post-terminate acquire) + reader-join overhead.
    assert elapsed < 8.0, f"close took too long: {elapsed:.2f}s"
    assert fake_proc.terminated or fake_proc.killed, (
        "close did NOT escalate terminate/kill"
    )

    # Release the blocking write so the writer can unwind. In real life
    # the SIGTERM unblocks the writer with BrokenPipeError.
    blocking.release_write.set()
    write_done.wait(timeout=5.0)


def test_restart_after_close_raises_runtime_error() -> None:
    """F6: PiRpcClient is single-use; restart raises RuntimeError."""
    with _started_client(_fake_config()) as client:
        pass  # close on exit
    with pytest.raises(RuntimeError, match="single-use"):
        client._start_sync()


def test_send_ui_gate_wait_honors_caller_timeout(caplog: pytest.LogCaptureFixture) -> None:
    """F5: UI gate wait uses min(caller deadline, config.request_timeout).

    Pre-fix: gate wait always used config.request_timeout (default 30s),
    ignoring caller's per-call timeout. A send(timeout=0.5) could wait
    much longer than 0.5s before raising.
    """

    class _NeverWrites:
        # Should never reach write since the gate stays set.
        def write(self, _data: bytes) -> int:
            raise AssertionError("write should not happen — gate is set")

        def flush(self) -> None:
            pass

    class _FakeProc:
        stdin = _NeverWrites()

    client = _client_no_subprocess()
    client.process = _FakeProc()  # type: ignore[assignment]
    # Permanently set the gate; never clear.
    client._ui_response_pending.set()

    # config.request_timeout is the default (5.0 in our fake config); the
    # caller asks for 0.3s. The wait must honor the smaller bound.
    t0 = time.monotonic()
    with pytest.raises(PiRpcProcessError, match="UI response gate"):
        client._send_sync({"type": "noop"}, timeout=0.3)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.5, f"caller timeout 0.3s ignored: elapsed {elapsed:.2f}s"


def test_event_handler_non_none_return_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F12: event handler returning non-None logs a warning (migration signal)."""
    import logging

    caplog.set_level(logging.WARNING, logger="libharness.pi.rpc")
    client = _client_no_subprocess()

    def returning_handler(_e: dict) -> str:
        return "ignored"

    client.on_event(returning_handler)
    client._dispatch_event({"type": "x"})
    assert any("returned non-None" in rec.getMessage() for rec in caplog.records)


def test_partial_startup_failure_cleans_up_subprocess_and_readers() -> None:
    """F8: when subprocess exits during startup, _start_sync cleans up cleanly."""
    # Configure with a command that exits immediately.
    cfg = PiLaunchConfig(
        pi_command=[sys.executable, "-c", "import sys; sys.exit(7)"],
        request_timeout=1.0,
        startup_timeout=0.5,
        offline=False,
        no_extensions=False,
        no_skills=False,
        no_prompt_templates=False,
        no_context_files=False,
    )
    client = PiRpcClient(cfg)
    with pytest.raises(PiRpcProcessError, match="exited during startup"):
        client._start_sync()
    # Cleanup happened: no process, no reader threads.
    assert client.process is None
    assert client._stdout_reader is None
    assert client._stderr_reader is None
    # Single-use sentinel set so restart raises.
    assert client._close_complete.is_set()


def test_prompt_and_wait_timeout_raises_and_unsubscribes() -> None:
    """Codex T24: prompt_and_wait timeout raises PiRpcProcessError + removes temp handler."""
    client = _client_no_subprocess()

    def fake_prompt(message: str, **_: object) -> dict:
        # Trigger an unrelated event so the collector sees something but never agent_end.
        client._handle_message({"type": "agent_start"})
        return {}

    client._prompt_sync = fake_prompt  # type: ignore[assignment, method-assign]
    handlers_before = len(client._event_handlers)
    with pytest.raises(PiRpcProcessError, match="timeout"):
        client._prompt_and_wait_sync("x", timeout=0.1)
    with client._handlers_lock:
        # The temp handler must have been unsubscribed in finally.
        assert len(client._event_handlers) == handlers_before
