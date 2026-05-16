"""Tests for the threaded ``PythonToolServer`` (phase 2).

Rewritten from the v3 async test as part of the threads rewrite. Uses raw
``socket`` clients (not ``asyncio.open_connection``) so the test exercises
the bridge with the same kind of consumer the TypeScript shim is — a sync
JSONL TCP client.

The original async test (``test_server_manifest_execute_and_updates``) is
preserved at the top to keep wire-compatibility with the async harness
during phases 2-3 (the async shims on ``PythonToolServer.start``/``close``
must still work). Phase 4 will drop both the shims and the async test.
"""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from libharness.pi.jsonl import dumps_line
from libharness.pi.server import PythonToolServer
from libharness.pi.tools import ToolContext, ToolRegistry, ToolResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FrameReader:
    """Per-socket buffered JSONL reader.

    ``recv()`` can return multiple frames at once; a stateful buffer keeps
    leftover bytes between calls so each ``read_frame()`` returns exactly
    one parsed JSON dict.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = bytearray()

    def read_frame(self, *, timeout: float = 5.0) -> dict:
        self._sock.settimeout(timeout)
        while b"\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                if not self._buf:
                    raise EOFError("server closed without a frame")
                break
            self._buf.extend(chunk)
        line, sep, rest = bytes(self._buf).partition(b"\n")
        if not sep:
            raise EOFError(f"partial frame at close: {line!r}")
        self._buf = bytearray(rest)
        return json.loads(line.decode("utf-8"))


def _send_frame(sock: socket.socket, frame: dict) -> None:
    sock.sendall(dumps_line(frame))


def _read_frame(sock: socket.socket, *, timeout: float = 5.0) -> dict:
    """Single-frame convenience for tests that consume one response.

    Tests reading multiple frames from the same socket should construct a
    ``_FrameReader`` so leftover bytes don't get dropped.
    """
    return _FrameReader(sock).read_frame(timeout=timeout)


@contextmanager
def _started_server(registry: ToolRegistry, **kwargs: object) -> Iterator[PythonToolServer]:
    server = PythonToolServer(registry, **kwargs)  # type: ignore[arg-type]
    server._start_sync()
    try:
        yield server
    finally:
        server._close_sync()


@contextmanager
def _connect(server: PythonToolServer, *, timeout: float = 5.0) -> Iterator[socket.socket]:
    endpoint = server.endpoint
    sock = socket.create_connection((endpoint.host, endpoint.port), timeout=timeout)
    try:
        yield sock
    finally:
        with contextlib.suppress(OSError):
            sock.close()


# ---------------------------------------------------------------------------
# Preserved async test (phase-2/3 shim compatibility)
# ---------------------------------------------------------------------------


def test_async_shim_preserves_manifest_execute_and_updates() -> None:
    """The async ``start``/``close`` shims still work; preserves harness compat.

    Run via ``asyncio.run`` so this test stays sync at the pytest layer
    (AST guard) while still exercising the async surface end-to-end. Phase 4
    will drop the shims and this test along with them.
    """
    import asyncio

    async def body() -> None:
        registry = ToolRegistry()

        @registry.register(description="Echo a message")
        async def echo(message: str, ctx: ToolContext) -> ToolResult:
            await ctx.update(f"working on {message}")
            return ToolResult.text(f"echo:{message}", details={"length": len(message)})

        server = PythonToolServer(registry)
        endpoint = await server.start()
        try:
            reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
            writer.write(dumps_line({"id": "m1", "type": "manifest", "token": endpoint.token}))
            await writer.drain()
            manifest = json.loads((await reader.readline()).decode())
            assert manifest["success"] is True
            assert manifest["data"]["protocolVersion"] == 1
            assert manifest["data"]["tools"][0]["name"] == "echo"
            writer.close()
            await writer.wait_closed()

            reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
            writer.write(dumps_line({"id": "e1", "type": "execute", "token": endpoint.token, "tool": "echo", "params": {"message": "hi"}}))
            await writer.drain()
            first = json.loads((await reader.readline()).decode())
            second = json.loads((await reader.readline()).decode())
            assert first["type"] == "update"
            assert "working on hi" in first["data"]["content"][0]["text"]
            assert second["type"] == "response"
            assert second["success"] is True
            assert second["data"]["content"][0]["text"] == "echo:hi"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.close()

    asyncio.run(body())


# ---------------------------------------------------------------------------
# Basic protocol — sync clients
# ---------------------------------------------------------------------------


def test_manifest_round_trip_sync_client() -> None:
    registry = ToolRegistry()

    @registry.register(description="Identity")
    def ident(x: str) -> str:
        return x

    with _started_server(registry) as server, _connect(server) as sock:
        _send_frame(sock, {"id": "m1", "type": "manifest", "token": server.token})
        frame = _read_frame(sock)
        assert frame["type"] == "response"
        assert frame["success"] is True
        assert frame["data"]["protocolVersion"] == 1
        assert {t["name"] for t in frame["data"]["tools"]} == {"ident"}


def test_execute_sync_tool() -> None:
    registry = ToolRegistry()

    @registry.register(description="Add two ints")
    def add(a: int, b: int) -> ToolResult:
        return ToolResult.text(str(a + b))

    with _started_server(registry) as server, _connect(server) as sock:
        _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "add", "params": {"a": 2, "b": 40}})
        frame = _read_frame(sock)
        assert frame["type"] == "response"
        assert frame["success"] is True
        assert frame["data"]["content"][0]["text"] == "42"


def test_execute_sync_tool_with_ctx_update() -> None:
    """Phase-2 ``ctx.update`` from a sync tool body via ``await`` boilerplate.

    The current ``ToolContext.update`` is ``async def`` (rewrite in phase 3),
    so a sync tool body either has to schedule an ``asyncio.run`` or simply
    not call it. The threaded server invokes ``collect_tool_result`` via
    ``asyncio.run`` per execute, so an ``async def`` tool body whose body
    awaits ``ctx.update`` works end-to-end. This test pins that path.
    """
    registry = ToolRegistry()

    @registry.register(description="Two-update tool")
    async def two_updates(ctx: ToolContext) -> ToolResult:
        await ctx.update("first")
        await ctx.update("second")
        return ToolResult.text("done")

    with _started_server(registry) as server, _connect(server) as sock:
        _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "two_updates", "params": {}})
        reader = _FrameReader(sock)
        first = reader.read_frame()
        second = reader.read_frame()
        third = reader.read_frame()
        assert first["type"] == "update"
        assert first["data"]["content"][0]["text"] == "first"
        assert second["type"] == "update"
        assert second["data"]["content"][0]["text"] == "second"
        assert third["type"] == "response"
        assert third["success"] is True


def test_invalid_token_rejected() -> None:
    registry = ToolRegistry()

    with _started_server(registry) as server, _connect(server) as sock:
        _send_frame(sock, {"id": "m1", "type": "manifest", "token": "nope"})
        frame = _read_frame(sock)
        assert frame["success"] is False
        assert "invalid bridge token" in frame["error"]


def test_unknown_tool_returns_error() -> None:
    registry = ToolRegistry()

    with _started_server(registry) as server, _connect(server) as sock:
        _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "ghost", "params": {}})
        frame = _read_frame(sock)
        assert frame["success"] is False
        assert "unknown tool" in frame["error"]


def test_unknown_request_type_returns_error() -> None:
    registry = ToolRegistry()

    with _started_server(registry) as server, _connect(server) as sock:
        _send_frame(sock, {"id": "x1", "type": "wat", "token": server.token})
        frame = _read_frame(sock)
        assert frame["success"] is False
        assert "unknown bridge request type" in frame["error"]


# ---------------------------------------------------------------------------
# T1.3 (preserved): timeout floor + scope
# ---------------------------------------------------------------------------


def test_handler_timeout_on_partial_bytes() -> None:
    """A client that opens the connection but never sends a full frame times out."""
    registry = ToolRegistry()
    # timeout_ms=10 is below the 1-second floor; floor activates → 1s read timeout.
    with _started_server(registry, timeout_ms=10) as server, _connect(server, timeout=5.0) as sock:
        sock.sendall(b'{"id": "m1", "type": "manifest", "token": "')  # half a frame
        frame = _read_frame(sock, timeout=3.0)
        assert frame["success"] is False
        assert "timeout" in frame["error"]


def test_small_timeout_ms_still_gets_one_second_floor() -> None:
    """``timeout_ms`` below 1000 gets floored to 1.0s so handshake doesn't spuriously fail."""
    registry = ToolRegistry()

    @registry.register(description="Quick")
    def quick() -> str:
        return "ok"

    with _started_server(registry, timeout_ms=1) as server, _connect(server) as sock:
        # If the timeout were 1 ms, this would time out before we even finished
        # sending the frame. With the 1s floor, the handshake completes fine.
        _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "quick", "params": {}})
        frame = _read_frame(sock)
        assert frame["success"] is True


def test_long_running_tool_does_not_spuriously_cancel() -> None:
    """``timeout_ms`` scopes the *initial-frame* read only.

    After the request frame is read, ``settimeout(None)`` restores blocking
    semantics; the tool can take longer than ``timeout_ms`` without being
    treated as a stalled-handshake.
    """
    registry = ToolRegistry()

    @registry.register(description="Slow")
    def slow() -> str:
        time.sleep(1.5)  # >> 100 ms timeout floor
        return "slow-done"

    with _started_server(registry, timeout_ms=100) as server, _connect(server) as sock:
        _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "slow", "params": {}})
        frame = _read_frame(sock, timeout=5.0)
        assert frame["success"] is True
        assert frame["data"]["content"][0]["text"] == "slow-done"


# ---------------------------------------------------------------------------
# T1.4 half-duplex contract enforcement
# ---------------------------------------------------------------------------


def test_half_duplex_protocol_violation_byte_logged_and_flagged(caplog: pytest.LogCaptureFixture) -> None:
    """A byte sent after the request frame is a protocol violation.

    The tool is cancelled; the response carries ``protocolViolation: true``;
    a warning is logged with the offending bytes.
    """
    import logging

    caplog.set_level(logging.WARNING, logger="libharness.pi.server")

    registry = ToolRegistry()
    saw_cancel = threading.Event()

    @registry.register(description="Watches for cancel")
    async def waiter(ctx: ToolContext) -> ToolResult:
        # Wait up to 3 s for the cancel flag; if it fires, raise a cancel-ish error.
        for _ in range(300):
            if ctx.cancelled:
                saw_cancel.set()
                raise RuntimeError("tool sees cancel")
            await _async_sleep(0.01)
        return ToolResult.text("never-cancelled")

    with _started_server(registry) as server, _connect(server) as sock:
        _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "waiter", "params": {}})
        # Now send an extra byte — the half-duplex contract violation.
        sock.sendall(b"X")
        frame = _read_frame(sock, timeout=5.0)
        assert frame["success"] is False
        assert frame.get("protocolViolation") is True
    assert saw_cancel.is_set()
    assert any("protocol violation" in rec.getMessage() for rec in caplog.records)


def test_clean_client_disconnect_cancels_tool_without_protocol_violation() -> None:
    """Closing the socket cleanly (``b""`` from ``recv``) cancels but does NOT flag a violation."""
    registry = ToolRegistry()
    saw_cancel = threading.Event()
    response_attempted = threading.Event()

    @registry.register(description="Watches for cancel")
    async def waiter(ctx: ToolContext) -> ToolResult:
        for _ in range(300):
            if ctx.cancelled:
                saw_cancel.set()
                response_attempted.set()
                raise RuntimeError("cancelled cleanly")
            await _async_sleep(0.01)
        return ToolResult.text("never-cancelled")

    with _started_server(registry) as server, _connect(server) as sock:
        _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "waiter", "params": {}})
        # Cleanly close — no extra bytes.
        sock.close()
        assert response_attempted.wait(5.0)
    assert saw_cancel.is_set()


# ---------------------------------------------------------------------------
# Slowloris bounded by max_handlers (T2.5: track in process_request)
# ---------------------------------------------------------------------------


def test_slowloris_bounded_by_max_handlers() -> None:
    """``max_handlers + 2`` connections; peak active handler count ≤ max_handlers.

    Covers the v4-synthesis T2.5 timing fix: active handlers are tracked
    inside ``process_request`` (at thread creation), so the peak count cannot
    exceed the slot semaphore size even briefly.
    """
    registry = ToolRegistry()
    proceed = threading.Event()

    @registry.register(description="Hangs until released")
    def hanger() -> str:
        proceed.wait(timeout=10.0)
        return "ok"

    peak = [0]

    def sample_peak(server: PythonToolServer) -> None:
        while not proceed.is_set():
            with server._handler_threads_lock:
                peak[0] = max(peak[0], len(server._active_handler_threads))
            time.sleep(0.005)

    max_handlers = 2
    with _started_server(registry, max_handlers=max_handlers) as server:
        sampler = threading.Thread(target=sample_peak, args=(server,), daemon=True)
        sampler.start()

        socks: list[socket.socket] = []
        try:
            # Open max_handlers + 2 = 4 connections; first 2 occupy the slots,
            # remaining 2 are rejected (server closes them).
            for _ in range(max_handlers + 2):
                s = socket.create_connection((server.endpoint.host, server.endpoint.port), timeout=2.0)
                socks.append(s)
                _send_frame(s, {"id": "e1", "type": "execute", "token": server.token, "tool": "hanger", "params": {}})
            time.sleep(0.2)  # let the server settle so the sampler observes the peak
            with server._handler_threads_lock:
                live_peak = len(server._active_handler_threads)
            assert peak[0] <= max_handlers, f"peak={peak[0]} exceeded max_handlers={max_handlers}"
            assert live_peak <= max_handlers, f"live={live_peak} exceeded max_handlers={max_handlers}"
        finally:
            proceed.set()
            for s in socks:
                with contextlib.suppress(OSError):
                    s.close()
            sampler.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Registry snapshot semantics
# ---------------------------------------------------------------------------


def test_late_register_does_not_leak_into_running_server() -> None:
    """A tool registered after ``start()`` is NOT visible to the running server.

    Phase 1 added ``ToolRegistry.snapshot()``; phase 2's server uses it on
    start so the bound registry is frozen for the server's lifetime
    (T2.6-v4).
    """
    registry = ToolRegistry()

    @registry.register(description="Initial")
    def initial() -> str:
        return "initial-ok"

    with _started_server(registry) as server, _connect(server) as sock:
        # Register a new tool AFTER the server started.
        @registry.register(description="Late")
        def late() -> str:
            return "late-ok"

        _send_frame(sock, {"id": "m1", "type": "manifest", "token": server.token})
        frame = _read_frame(sock)
        names = {t["name"] for t in frame["data"]["tools"]}
        assert names == {"initial"}, f"late tool leaked into running server: {names}"


def test_caller_registry_not_mutated_by_server_start() -> None:
    """``PythonToolServer.start`` snapshots; the live registry stays usable."""
    registry = ToolRegistry()

    @registry.register(description="One")
    def one() -> str:
        return "one"

    with _started_server(registry):
        # Caller still owns the live registry; can add more.
        @registry.register(description="Two")
        def two() -> str:
            return "two"

    # After server close, both tools are still on the live registry.
    assert "one" in registry
    assert "two" in registry


# ---------------------------------------------------------------------------
# Concurrent execute — no cross-talk between handlers
# ---------------------------------------------------------------------------


def test_concurrent_tool_calls_do_not_cross_talk() -> None:
    """Two tools each emit a distinct sentinel; each client sees only its own."""
    registry = ToolRegistry()
    barrier = threading.Barrier(2, timeout=5.0)

    @registry.register(description="Sentinel A")
    async def emit_a(ctx: ToolContext) -> ToolResult:
        barrier.wait()
        await ctx.update("A-update")
        return ToolResult.text("A-final")

    @registry.register(description="Sentinel B")
    async def emit_b(ctx: ToolContext) -> ToolResult:
        barrier.wait()
        await ctx.update("B-update")
        return ToolResult.text("B-final")

    received_a: list[dict] = []
    received_b: list[dict] = []

    def run(tool: str, into: list[dict]) -> None:
        with _connect(server) as sock:
            _send_frame(sock, {"id": tool, "type": "execute", "token": server.token, "tool": tool, "params": {}})
            reader = _FrameReader(sock)
            into.append(reader.read_frame(timeout=5.0))
            into.append(reader.read_frame(timeout=5.0))

    with _started_server(registry) as server:
        t_a = threading.Thread(target=run, args=("emit_a", received_a))
        t_b = threading.Thread(target=run, args=("emit_b", received_b))
        t_a.start()
        t_b.start()
        t_a.join(timeout=10.0)
        t_b.join(timeout=10.0)

    assert [f["data"]["content"][0]["text"] for f in received_a] == ["A-update", "A-final"]
    assert [f["data"]["content"][0]["text"] for f in received_b] == ["B-update", "B-final"]


# ---------------------------------------------------------------------------
# Wire-shape regression (T2.2-v4: preserve shim contract)
# ---------------------------------------------------------------------------


def test_update_wire_frame_uses_data_field_not_result() -> None:
    """Update frames are ``{"id", "type": "update", "data": ...}``.

    The v4 plan's pseudocode accidentally renamed ``data`` to ``result``;
    that would break ``shim.py``. This test pins the current wire shape so
    a future rewrite cannot quietly regress it.
    """
    registry = ToolRegistry()

    @registry.register(description="Emit one update")
    async def emit(ctx: ToolContext) -> ToolResult:
        await ctx.update("hello")
        return ToolResult.text("done")

    with _started_server(registry) as server, _connect(server) as sock:
        _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "emit", "params": {}})
        frame = _read_frame(sock)
        assert frame["type"] == "update"
        assert "data" in frame and "result" not in frame
        assert frame["id"] == "e1"


def test_tool_spec_to_manifest_includes_optional_fields() -> None:
    """ToolSpec.to_manifest emits optional fields when set; unit-level wire check."""
    from libharness.pi.tools import ToolSpec

    spec = ToolSpec(
        name="full",
        description="d",
        prompt_snippet="snippet",
        prompt_guidelines=("g1", "g2"),
        execution_mode="parallel",
    )
    manifest = spec.to_manifest()
    assert manifest["promptSnippet"] == "snippet"
    assert manifest["promptGuidelines"] == ["g1", "g2"]
    assert manifest["executionMode"] == "parallel"


def test_tool_result_to_wire_emits_non_default_fields() -> None:
    """Integration-level: terminate + details survive the round trip."""
    registry = ToolRegistry()

    @registry.register(description="Return with terminate=True")
    def stopper() -> ToolResult:
        return ToolResult.text("bye", details={"reason": "test"}, terminate=True)

    with _started_server(registry) as server, _connect(server) as sock:
        _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "stopper", "params": {}})
        frame = _read_frame(sock)
        assert frame["data"]["terminate"] is True
        assert frame["data"]["details"]["reason"] == "test"


# ---------------------------------------------------------------------------
# bridge_write_timeout via select.select (T1.4-v4 deviation)
# ---------------------------------------------------------------------------


def test_bridge_write_timeout_uses_select(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ctx.update`` calls ``select.select`` (not ``socket.settimeout``) when timeout is set.

    Unit-level verification of the T1.4-v4 deviation. The sidecar's
    ``recv(1)`` runs on the same socket; ``socket.settimeout`` would race it.
    This test asserts the select-based path is exercised — sister test
    ``test_bridge_write_timeout_default_none_does_not_call_select`` asserts
    the no-timeout path skips select entirely.
    """
    import libharness.pi.server as server_mod

    select_write_timeouts: list[float] = []
    real_select = server_mod.select.select

    def fake_select(rlist, wlist, xlist, timeout):  # type: ignore[no-untyped-def]
        if wlist and not rlist:
            select_write_timeouts.append(timeout)
        return real_select(rlist, wlist, xlist, timeout)

    monkeypatch.setattr(server_mod.select, "select", fake_select)

    registry = ToolRegistry()

    @registry.register(description="One update")
    async def emit(ctx: ToolContext) -> ToolResult:
        await ctx.update("payload")
        return ToolResult.text("done")

    with _started_server(registry, bridge_write_timeout=2.0) as server, _connect(server) as sock:
        _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "emit", "params": {}})
        reader = _FrameReader(sock)
        first = reader.read_frame()
        second = reader.read_frame()
        assert first["type"] == "update"
        assert second["type"] == "response"

    assert 2.0 in select_write_timeouts, (
        f"select.select not invoked with bridge_write_timeout=2.0: {select_write_timeouts}"
    )


def test_bridge_write_timeout_raises_when_select_reports_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``select.select`` returns empty write-ready, ``ctx.update`` raises ``PiRpcProcessError``.

    The tool's exception path runs; the response frame carries the error.
    """
    import libharness.pi.server as server_mod

    original_select = server_mod.select.select

    def starving_select(rlist, wlist, xlist, timeout):  # type: ignore[no-untyped-def]
        # If asked for write-readiness on a socket, always report none.
        if wlist:
            return ([], [], [])
        return original_select(rlist, wlist, xlist, timeout)

    monkeypatch.setattr(server_mod.select, "select", starving_select)

    registry = ToolRegistry()

    @registry.register(description="One update")
    async def emit(ctx: ToolContext) -> ToolResult:
        await ctx.update("payload")
        return ToolResult.text("never reached")

    with _started_server(registry, bridge_write_timeout=0.05) as server, _connect(server) as sock:
        _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "emit", "params": {}})
        frame = _read_frame(sock, timeout=5.0)
        assert frame["success"] is False
        assert "timeout" in frame["error"].lower()


def test_bridge_write_timeout_default_none_does_not_call_select(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``bridge_write_timeout=None`` (default), ``ctx.update`` does plain ``sendall``."""
    import libharness.pi.server as server_mod

    select_calls: list[object] = []
    real_select = server_mod.select.select

    def counting_select(rlist, wlist, xlist, timeout):  # type: ignore[no-untyped-def]
        if wlist:
            select_calls.append((rlist, wlist, timeout))
        return real_select(rlist, wlist, xlist, timeout)

    monkeypatch.setattr(server_mod.select, "select", counting_select)

    registry = ToolRegistry()

    @registry.register(description="One update")
    async def emit(ctx: ToolContext) -> ToolResult:
        await ctx.update("payload")
        return ToolResult.text("done")

    with _started_server(registry) as server, _connect(server) as sock:
        _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "emit", "params": {}})
        reader = _FrameReader(sock)
        reader.read_frame()  # update
        reader.read_frame()  # response

    assert select_calls == [], f"select.select called with bridge_write_timeout=None: {select_calls}"


# ---------------------------------------------------------------------------
# Daemon shutdown cleanliness with uncooperative tool
# ---------------------------------------------------------------------------


def test_close_returns_promptly_even_with_uncooperative_tool() -> None:
    """A tool body that ignores cancellation does NOT prevent ``close`` from returning.

    Out-of-process via ``subprocess.run`` so an actual hang fails the test
    fast rather than wedging pytest. Pytest's own thread-leak isn't tested
    here; ``daemon_threads = True`` is process-exit safety.
    """
    script = """
import sys, threading, time
from libharness.pi.server import PythonToolServer
from libharness.pi.tools import ToolContext, ToolRegistry, ToolResult

registry = ToolRegistry()

@registry.register(description="Ignores cancel")
async def ignores_cancel(ctx: ToolContext) -> ToolResult:
    # Spin forever; doesn't check ctx.cancelled.
    while True:
        time.sleep(0.5)

server = PythonToolServer(registry)
server._start_sync()

# Launch a connection that triggers the uncooperative tool.
import socket, json
endpoint = server.endpoint
sock = socket.create_connection((endpoint.host, endpoint.port), timeout=2)
sock.sendall((json.dumps({"id": "e1", "type": "execute", "token": endpoint.token, "tool": "ignores_cancel", "params": {}}) + "\\n").encode())

# Give the handler a moment to start the tool.
time.sleep(0.5)

# Now close. Must return within 5 s.
t0 = time.monotonic()
server._close_sync()
elapsed = time.monotonic() - t0
print(f"CLOSE_ELAPSED_S={elapsed:.3f}")

# Cleanup the dangling socket; intentionally NOT shutting down the daemon
# tool thread (it's leaked by design — daemon=True is process-exit safety).
sock.close()
sys.exit(0)
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=15.0,
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, f"subprocess failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    elapsed_line = [ln for ln in proc.stdout.splitlines() if ln.startswith("CLOSE_ELAPSED_S=")]
    assert elapsed_line, f"missing CLOSE_ELAPSED_S marker:\n{proc.stdout}"
    elapsed = float(elapsed_line[0].split("=", 1)[1])
    # 2 s server-thread join + 2 s handler join = 4 s upper bound; we expect
    # well under that since the server thread exits immediately on shutdown().
    assert elapsed < 6.0, f"close too slow: {elapsed}s"


# ---------------------------------------------------------------------------
# Additional coverage (from codex-generated phase-2 test plan)
# ---------------------------------------------------------------------------


def test_bridge_write_timeout_error_is_not_reported_as_cancellation(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A write-timeout is a ``ctx.update timeout`` process error, not a cancel/violation."""
    import logging

    import libharness.pi.server as server_mod

    caplog.set_level(logging.WARNING, logger="libharness.pi.server")
    real_select = server_mod.select.select

    def starving_select(rlist, wlist, xlist, timeout):  # type: ignore[no-untyped-def]
        if wlist and not rlist:
            return ([], [], [])
        return real_select(rlist, wlist, xlist, timeout)

    monkeypatch.setattr(server_mod.select, "select", starving_select)

    registry = ToolRegistry()

    @registry.register(description="One update")
    async def emit(ctx: ToolContext) -> ToolResult:
        await ctx.update("payload")
        return ToolResult.text("never reached")

    with _started_server(registry, bridge_write_timeout=0.05) as server, _connect(server) as sock:
        _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "emit", "params": {}})
        frame = _read_frame(sock, timeout=5.0)

    assert frame["success"] is False
    assert "ctx.update timeout" in frame["error"]
    assert "tool execution cancelled" not in frame["error"]
    assert frame.get("protocolViolation") is not True
    assert not any("protocol violation" in rec.getMessage() for rec in caplog.records)


def test_process_request_tracks_handler_before_thread_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """T2.5: the handler thread is in ``_active_handler_threads`` before ``Thread.start()``."""
    import libharness.pi.server as server_mod

    registry = ToolRegistry()
    tracked_before_start = threading.Event()
    real_start = threading.Thread.start
    captured_server: list[PythonToolServer] = []

    def tracking_start(self: threading.Thread) -> None:
        if self.name == "bridge-handler" and captured_server:
            server = captured_server[0]
            with server._handler_threads_lock:
                if self in server._active_handler_threads:
                    tracked_before_start.set()
        real_start(self)

    monkeypatch.setattr(server_mod.threading.Thread, "start", tracking_start)

    with _started_server(registry) as server:
        captured_server.append(server)
        with _connect(server) as sock:
            _send_frame(sock, {"id": "m1", "type": "manifest", "token": server.token})
            _read_frame(sock)

    assert tracked_before_start.is_set(), "handler thread was not tracked before t.start()"


def test_thread_start_failure_releases_slot_active_entry_and_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``Thread.start()`` raises, the slot is released, tracking is cleaned, request shut down."""
    import libharness.pi.server as server_mod

    registry = ToolRegistry()
    real_start = threading.Thread.start
    fired_event = threading.Event()

    def failing_start(self: threading.Thread) -> None:
        if self.name == "bridge-handler":
            fired_event.set()
            raise RuntimeError("simulated start failure")
        real_start(self)

    monkeypatch.setattr(server_mod.threading.Thread, "start", failing_start)

    with _started_server(registry) as server:
        slot_count_before = server._handler_slots._value  # type: ignore[attr-defined]
        # Trigger the accept loop. Open the connection and poll until the
        # failure branch fires (avoids race where the server hasn't yet
        # accepted by the time we check).
        sock = socket.create_connection(
            (server.endpoint.host, server.endpoint.port), timeout=2.0,
        )
        try:
            # Send some bytes to force the accept and ensure the server starts
            # processing the connection.
            with contextlib.suppress(OSError):
                sock.sendall(b'{"id":"x","type":"manifest","token":"x"}\n')
            # The failure branch must fire within a reasonable time.
            assert fired_event.wait(3.0), "Thread.start failure branch never fired"
        finally:
            with contextlib.suppress(OSError):
                sock.close()
        # After the failure, wait for the slot to be released.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if server._handler_slots._value == slot_count_before:  # type: ignore[attr-defined]
                break
            time.sleep(0.01)
        with server._handler_threads_lock:
            assert not server._active_handler_threads
        assert server._handler_slots._value == slot_count_before, (  # type: ignore[attr-defined]
            f"slot leaked: baseline={slot_count_before}, after={server._handler_slots._value}"  # type: ignore[attr-defined]
        )


def test_live_registry_metadata_mutation_after_start_does_not_affect_manifest_snapshot() -> None:
    """Mutating a registered ToolSpec's nested parameters dict doesn't change the server's manifest."""
    registry = ToolRegistry()

    @registry.register(
        description="Search",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "original"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    def search(query: str) -> str:
        return query

    with _started_server(registry) as server, _connect(server) as sock:
        registered = registry.get("search")
        registered.spec.parameters["properties"]["query"]["description"] = "mutated"
        registered.spec.parameters["properties"]["extra"] = {"type": "string"}

        _send_frame(sock, {"id": "m1", "type": "manifest", "token": server.token})
        frame = _read_frame(sock)
        tool = next(t for t in frame["data"]["tools"] if t["name"] == "search")
        assert tool["parameters"]["properties"]["query"]["description"] == "original"
        assert "extra" not in tool["parameters"]["properties"]


def test_manifest_snapshot_stable_under_concurrent_live_registry_mutation_ft() -> None:
    """FT-relevant: parallel manifest requests + caller-side registrations never crash or interleave.

    The server's frozen registry is captured at start; concurrent caller-side
    mutation of the live registry doesn't leak into any manifest response.
    """
    registry = ToolRegistry()

    @registry.register(description="Initial")
    def initial() -> str:
        return "ok"

    stop = threading.Event()
    barrier = threading.Barrier(2, timeout=10.0)

    def mutator() -> None:
        barrier.wait()
        i = 0
        while not stop.is_set():
            iteration = i  # bind the loop variable into the closure
            with contextlib.suppress(Exception):
                registry.register(
                    lambda iteration=iteration: f"late-{iteration}",
                    name=f"late_{iteration}",
                    description="late",
                )
            i += 1

    with _started_server(registry) as server:
        t = threading.Thread(target=mutator, daemon=True)
        t.start()
        try:
            barrier.wait()
            for _ in range(25):
                with _connect(server) as sock:
                    _send_frame(sock, {"id": "m", "type": "manifest", "token": server.token})
                    frame = _read_frame(sock)
                    names = {tt["name"] for tt in frame["data"]["tools"]}
                    assert names == {"initial"}, f"snapshot drift: {names}"
        finally:
            stop.set()
            t.join(timeout=5.0)


def test_in_band_multi_byte_protocol_violation_sets_cancel_before_tool_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Multi-byte in-band extras flag cancellation + protocolViolation before the tool runs."""
    import logging

    caplog.set_level(logging.WARNING, logger="libharness.pi.server")
    registry = ToolRegistry()

    @registry.register(description="Reports cancellation state")
    def reporter(ctx: ToolContext) -> ToolResult:
        return ToolResult.text(f"cancelled={ctx.cancelled}")

    with _started_server(registry) as server, _connect(server) as sock:
        body = dumps_line({"id": "e1", "type": "execute", "token": server.token, "tool": "reporter", "params": {}}) + b"XYZ"
        sock.sendall(body)
        frame = _read_frame(sock, timeout=5.0)

    assert frame["success"] is True
    assert frame.get("protocolViolation") is True
    assert "cancelled=True" in frame["data"]["content"][0]["text"]
    assert any("protocol violation" in rec.getMessage() for rec in caplog.records)


def test_out_of_band_protocol_violation_after_sidecar_is_waiting_is_flagged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An extra byte sent *after* the request frame is observed (sidecar path)."""
    import logging

    caplog.set_level(logging.WARNING, logger="libharness.pi.server")
    registry = ToolRegistry()
    tool_entered = threading.Event()

    @registry.register(description="Waits for cancel")
    async def waiter(ctx: ToolContext) -> ToolResult:
        tool_entered.set()
        for _ in range(300):
            if ctx.cancelled:
                raise RuntimeError("saw-cancel")
            await _async_sleep(0.01)
        return ToolResult.text("never-cancelled")

    with _started_server(registry) as server, _connect(server) as sock:
        _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "waiter", "params": {}})
        assert tool_entered.wait(3.0)
        sock.sendall(b"X")
        frame = _read_frame(sock, timeout=5.0)

    assert frame["success"] is False
    assert frame.get("protocolViolation") is True
    assert any("protocol violation" in rec.getMessage() for rec in caplog.records)


def test_second_json_frame_in_same_recv_is_protocol_violation_not_decoder_error() -> None:
    """A complete second JSONL frame in the same recv is treated as a half-duplex violation."""
    registry = ToolRegistry()

    @registry.register(description="Quick")
    def quick() -> str:
        return "ok"

    with _started_server(registry) as server, _connect(server) as sock:
        body = dumps_line({"id": "e1", "type": "execute", "token": server.token, "tool": "quick", "params": {}})
        body += dumps_line({"id": "m1", "type": "manifest", "token": server.token})
        sock.sendall(body)
        frame = _read_frame(sock, timeout=5.0)

    assert frame["id"] == "e1"
    assert frame.get("protocolViolation") is True
    # Tool still ran (we accept the first frame); response carries the violation flag.
    assert frame["success"] is True


def test_empty_decoder_leftover_does_not_mark_protocol_violation_or_cancel() -> None:
    """A normal single-frame request has no protocolViolation flag and no cancel."""
    registry = ToolRegistry()

    @registry.register(description="Reports cancellation state")
    def reporter(ctx: ToolContext) -> ToolResult:
        return ToolResult.text(f"cancelled={ctx.cancelled}")

    with _started_server(registry) as server, _connect(server) as sock:
        _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "reporter", "params": {}})
        frame = _read_frame(sock)

    assert frame["success"] is True
    assert "protocolViolation" not in frame
    assert "cancelled=False" in frame["data"]["content"][0]["text"]


def test_each_execute_gets_fresh_closed_asyncio_loop() -> None:
    """Per-execute ``asyncio.run`` creates and closes a new loop each request."""
    import asyncio

    registry = ToolRegistry()
    loops_seen: list[asyncio.AbstractEventLoop] = []

    @registry.register(description="Capture loop")
    async def capture() -> ToolResult:
        loops_seen.append(asyncio.get_running_loop())
        return ToolResult.text(str(len(loops_seen)))

    with _started_server(registry) as server:
        for _ in range(2):
            with _connect(server) as sock:
                _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "capture", "params": {}})
                _read_frame(sock)

    assert len(loops_seen) == 2
    assert loops_seen[0] is not loops_seen[1]
    assert all(loop.is_closed() for loop in loops_seen)


def test_close_before_any_connection_returns_promptly_and_clears_state() -> None:
    """A started server with no connections closes within < 1 s and clears its state."""
    server = PythonToolServer(ToolRegistry())
    server._start_sync()
    t0 = time.monotonic()
    server._close_sync()
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0
    assert server._http_server is None
    assert server._serve_thread is None
    with server._handler_threads_lock:
        assert not server._active_handler_threads
    # Idempotent: second close is a no-op.
    server._close_sync()


def test_rapid_start_close_cycles_do_not_leak_serve_threads() -> None:
    """20 rapid start/close cycles don't accumulate ``pi-bridge-server`` threads."""
    baseline = {t.ident for t in threading.enumerate() if t.name == "pi-bridge-server"}
    for _ in range(20):
        s = PythonToolServer(ToolRegistry())
        s._start_sync()
        s._close_sync()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        current = {t.ident for t in threading.enumerate() if t.name == "pi-bridge-server"}
        if current == baseline:
            return
        time.sleep(0.05)
    current = {t.ident for t in threading.enumerate() if t.name == "pi-bridge-server"}
    assert current == baseline, f"leaked serve threads: {current - baseline}"


def test_close_during_sidecar_wait_returns_within_join_bound() -> None:
    """In-process: close during an active handler returns within the 2 s join bound."""
    registry = ToolRegistry()
    tool_entered = threading.Event()
    release_tool = threading.Event()

    @registry.register(description="Held tool")
    def held(ctx: ToolContext) -> ToolResult:
        tool_entered.set()
        release_tool.wait(timeout=10.0)
        return ToolResult.text("done")

    server = PythonToolServer(registry)
    server._start_sync()
    sock = socket.create_connection((server.endpoint.host, server.endpoint.port), timeout=2.0)
    try:
        _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "held", "params": {}})
        assert tool_entered.wait(3.0)
        t0 = time.monotonic()
        server._close_sync()
        elapsed = time.monotonic() - t0
        # Bound: serve-thread join (<=2 s, usually fast after shutdown) +
        # handler-join total deadline (3 s). Uncooperative tool body sits
        # past the deadline; daemon=True is the process-exit safety net.
        assert elapsed < 5.0, f"close too slow with held handler: {elapsed}s"
    finally:
        release_tool.set()
        with contextlib.suppress(OSError):
            sock.close()


def test_over_cap_connection_is_closed_without_response(caplog: pytest.LogCaptureFixture) -> None:
    """When all slots are busy, an additional connection is closed without a response."""
    import logging

    caplog.set_level(logging.WARNING, logger="libharness.pi.server")
    registry = ToolRegistry()
    first_entered = threading.Event()
    release_first = threading.Event()

    @registry.register(description="Hangs")
    def hold(ctx: ToolContext) -> ToolResult:
        first_entered.set()
        release_first.wait(timeout=10.0)
        return ToolResult.text("done")

    with _started_server(registry, max_handlers=1) as server:
        first = socket.create_connection((server.endpoint.host, server.endpoint.port), timeout=2.0)
        try:
            _send_frame(first, {"id": "e1", "type": "execute", "token": server.token, "tool": "hold", "params": {}})
            assert first_entered.wait(3.0)

            second = socket.create_connection((server.endpoint.host, server.endpoint.port), timeout=2.0)
            try:
                _send_frame(second, {"id": "m1", "type": "manifest", "token": server.token})
                second.settimeout(1.5)
                data = second.recv(4096)
                assert data == b"", f"over-cap connection got data: {data!r}"
            finally:
                second.close()

            with server._handler_threads_lock:
                assert len(server._active_handler_threads) == 1
            assert any("bridge at capacity" in rec.getMessage() for rec in caplog.records)
        finally:
            release_first.set()
            with contextlib.suppress(OSError):
                first.close()


def test_handler_slot_released_after_tool_exception_allows_next_connection() -> None:
    """A tool that raises still releases its slot so subsequent connections work."""
    registry = ToolRegistry()

    @registry.register(description="Raises")
    def boom() -> ToolResult:
        raise ValueError("boom")

    @registry.register(description="OK")
    def ok() -> str:
        return "ok"

    with _started_server(registry, max_handlers=1) as server:
        with _connect(server) as sock:
            _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "boom", "params": {}})
            frame = _read_frame(sock)
            assert frame["success"] is False
            assert "boom" in frame["error"]
        # Wait for slot release.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            with server._handler_threads_lock:
                if not server._active_handler_threads:
                    break
            time.sleep(0.01)
        with _connect(server) as sock2:
            _send_frame(sock2, {"id": "e2", "type": "execute", "token": server.token, "tool": "ok", "params": {}})
            frame2 = _read_frame(sock2)
            assert frame2["success"] is True
            assert frame2["data"]["content"][0]["text"] == "ok"


def test_non_json_serializable_tool_result_returns_error_frame() -> None:
    """A tool returning a non-JSON-serializable value gets a structured error frame."""
    registry = ToolRegistry()

    @registry.register(description="Bad details")
    def bad() -> ToolResult:
        return ToolResult.text("bad", details={"obj": object()})

    @registry.register(description="OK")
    def ok() -> str:
        return "ok"

    with _started_server(registry, max_handlers=1) as server:
        with _connect(server) as sock:
            _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "bad", "params": {}})
            frame = _read_frame(sock)
            assert frame["success"] is False
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            with server._handler_threads_lock:
                if not server._active_handler_threads:
                    break
            time.sleep(0.01)
        with _connect(server) as sock2:
            _send_frame(sock2, {"id": "e2", "type": "execute", "token": server.token, "tool": "ok", "params": {}})
            assert _read_frame(sock2)["success"] is True


def test_oversize_request_frame_returns_error_and_releases_handler() -> None:
    """A request frame >8 MiB returns an error and the slot is released for follow-up requests."""
    registry = ToolRegistry()

    @registry.register(description="OK")
    def ok() -> str:
        return "ok"

    with _started_server(registry, max_handlers=1) as server:
        with _connect(server, timeout=10.0) as sock:
            pad = "x" * (8 * 1024 * 1024 + 1024)
            body = dumps_line({"id": "big", "type": "manifest", "token": server.token, "pad": pad})
            sock.settimeout(10.0)
            sock.sendall(body)
            frame = _read_frame(sock, timeout=5.0)
            assert frame["success"] is False
            assert "exceeded" in frame["error"]
        # Wait for the slot release.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            with server._handler_threads_lock:
                if not server._active_handler_threads:
                    break
            time.sleep(0.01)
        # Subsequent connection succeeds (slot released).
        with _connect(server) as sock2:
            _send_frame(sock2, {"id": "m1", "type": "manifest", "token": server.token})
            assert _read_frame(sock2)["success"] is True


def test_slow_sender_request_split_across_multiple_recvs_succeeds() -> None:
    """A valid request frame split across multiple ``recv`` calls is accepted."""
    registry = ToolRegistry()

    @registry.register(description="Quick")
    def quick() -> str:
        return "ok"

    with _started_server(registry) as server, _connect(server) as sock:
        body = dumps_line({"id": "e1", "type": "execute", "token": server.token, "tool": "quick", "params": {}})
        # Send in three small chunks, under the 1 s floor.
        chunks = [body[:10], body[10:30], body[30:]]
        for chunk in chunks:
            sock.sendall(chunk)
            time.sleep(0.05)
        frame = _read_frame(sock)
    assert frame["success"] is True


# ---------------------------------------------------------------------------
# Regression tests for phase-2 review findings (F1-F22 in synthesis doc)
# ---------------------------------------------------------------------------


def test_malformed_json_request_returns_structured_error_not_silent_close() -> None:
    """F2/F21: invalid JSON → structured error frame, not silent EOF + traceback."""
    registry = ToolRegistry()
    with _started_server(registry) as server, _connect(server) as sock:
        sock.sendall(b"not-valid-json\n")
        frame = _read_frame(sock, timeout=3.0)
    assert frame["success"] is False
    assert "invalid JSONL" in frame["error"] or "invalid" in frame["error"].lower()


def test_cancelled_error_from_tool_body_returns_response() -> None:
    """F1: ``asyncio.CancelledError`` from inside the tool body produces a response.

    Without the explicit catch, CancelledError (BaseException subclass) escaped
    the handler. The client would see EOF.
    """
    import asyncio

    registry = ToolRegistry()

    @registry.register(description="Raises CancelledError directly")
    async def cancels() -> ToolResult:
        raise asyncio.CancelledError("simulated cancel from body")

    with _started_server(registry) as server, _connect(server) as sock:
        _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "cancels", "params": {}})
        frame = _read_frame(sock, timeout=5.0)

    assert frame["type"] == "response"
    assert frame["success"] is False
    assert "cancelled" in frame["error"].lower()


def test_out_of_band_violation_no_synthetic_delay_always_flags() -> None:
    """F18: violation byte arriving without synthetic delay always sets the flag.

    Pre-fix: 3/20 iterations had the warning log fire but the response missing
    ``protocolViolation: true``. With the late-drain fix, 100% of iterations
    should flag it. Run 20 iterations to be confident.
    """
    registry = ToolRegistry()

    @registry.register(description="Quick")
    def quick() -> str:
        return "ok"

    with _started_server(registry) as server:
        for _ in range(20):
            with _connect(server) as sock:
                _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "quick", "params": {}})
                # No synthetic delay — send violation byte immediately on a
                # fresh socket. The decoder-leftover path handles bytes in
                # the same recv; this path tests bytes that arrive after.
                sock.sendall(b"X")
                frame = _read_frame(sock, timeout=3.0)
            assert frame.get("protocolViolation") is True, (
                f"iteration missed the violation flag: {frame}"
            )


def test_thread_constructor_failure_releases_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """F19: a Thread() constructor that raises (e.g. MemoryError) releases the slot.

    Pre-fix: the BaseException handler only wrapped t.start(); construction
    failure leaked the semaphore slot. After 256 such failures, server was
    dead-locked at capacity.
    """
    import libharness.pi.server as server_mod

    registry = ToolRegistry()
    real_init = threading.Thread.__init__
    fail_remaining = [1]

    def failing_init(self: threading.Thread, *args: Any, **kwargs: Any) -> None:
        if kwargs.get("name") == "bridge-handler" and fail_remaining[0] > 0:
            fail_remaining[0] -= 1
            raise MemoryError("simulated constructor failure")
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(server_mod.threading.Thread, "__init__", failing_init)

    with _started_server(registry) as server:
        baseline = server._handler_slots._value  # type: ignore[attr-defined]
        # Trigger the failing accept.
        with contextlib.suppress(OSError):
            s = socket.create_connection((server.endpoint.host, server.endpoint.port), timeout=2.0)
            s.close()
        # Give the server thread a moment to process and recover.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if server._handler_slots._value == baseline:  # type: ignore[attr-defined]
                break
            time.sleep(0.01)
        assert server._handler_slots._value == baseline, (  # type: ignore[attr-defined]
            f"slot leaked: baseline={baseline}, after={server._handler_slots._value}"  # type: ignore[attr-defined]
        )


def test_close_immediately_after_start_does_not_hang() -> None:
    """F20: close called right after start does NOT hang on shutdown().

    Pre-fix: if close ran before serve_forever() entered, shutdown() blocked
    on ``__is_shut_down`` forever. We now wait for ``_serve_entered`` (with
    timeout) before calling shutdown().
    """
    registry = ToolRegistry()
    for _ in range(30):
        server = PythonToolServer(registry)
        t0 = time.monotonic()
        server._start_sync()
        server._close_sync()
        elapsed = time.monotonic() - t0
        assert elapsed < 3.0, f"close after immediate start hung: {elapsed}s"


def test_serve_thread_start_failure_does_not_leave_unclosable_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F3: if the serve thread fails to start, _http_server is NOT published."""
    import libharness.pi.server as server_mod

    registry = ToolRegistry()
    real_start = threading.Thread.start

    def failing_start(self: threading.Thread) -> None:
        if self.name == "pi-bridge-server":
            raise RuntimeError("simulated serve thread start failure")
        real_start(self)

    monkeypatch.setattr(server_mod.threading.Thread, "start", failing_start)

    server = PythonToolServer(registry)
    with pytest.raises(RuntimeError, match="simulated serve thread start failure"):
        server._start_sync()
    # _http_server must NOT be published when serve thread fails.
    assert server._http_server is None
    # close should be a clean no-op (no hang).
    t0 = time.monotonic()
    server._close_sync()
    assert time.monotonic() - t0 < 1.0


def test_close_actively_shuts_handler_sockets_to_unblock_handlers() -> None:
    """F5: close() shuts active handler sockets, waking cooperative handlers fast."""
    registry = ToolRegistry()
    tool_entered = threading.Event()

    @registry.register(description="Waits for cancel")
    async def waiter(ctx: ToolContext) -> ToolResult:
        tool_entered.set()
        for _ in range(500):
            if ctx.cancelled:
                return ToolResult.text("saw-cancel")
            await _async_sleep(0.01)
        return ToolResult.text("never")

    server = PythonToolServer(registry)
    server._start_sync()
    sock = socket.create_connection((server.endpoint.host, server.endpoint.port), timeout=2.0)
    try:
        _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "waiter", "params": {}})
        assert tool_entered.wait(3.0)
        # Close with cooperative handler — should be fast, NOT 120 s (default timeout_ms).
        t0 = time.monotonic()
        server._close_sync()
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"close didn't wake cooperative handler: {elapsed}s"
    finally:
        with contextlib.suppress(OSError):
            sock.close()


def test_params_non_dict_rejected() -> None:
    """F8: ``params=[]`` / ``params=0`` / ``params=""`` rejected, NOT coerced to {}."""
    registry = ToolRegistry()

    @registry.register(description="OK")
    def ok() -> str:
        return "ok"

    cases: list[Any] = [[], "", 0, False, "string", [1, 2]]
    for params in cases:
        with _started_server(registry) as server, _connect(server) as sock:
            _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "ok", "params": params})
            frame = _read_frame(sock)
        assert frame["success"] is False, f"params={params!r} should be rejected"
        assert "params" in frame["error"]


def test_params_missing_or_none_accepted_as_empty() -> None:
    """F8: missing or explicit ``None`` for params behaves as empty dict."""
    registry = ToolRegistry()

    @registry.register(description="No-arg")
    def noarg() -> str:
        return "ok"

    with _started_server(registry) as server:
        for params in (None, {}):
            with _connect(server) as sock:
                frame_send = {"id": "e1", "type": "execute", "token": server.token, "tool": "noarg"}
                if params is not None:
                    frame_send["params"] = params  # type: ignore[assignment]
                _send_frame(sock, frame_send)
                frame = _read_frame(sock)
            assert frame["success"] is True


def test_sync_tool_body_has_running_event_loop() -> None:
    """F9: sync tool body invoked inside the per-execute asyncio loop.

    Verifies the compatibility fix: ``asyncio.get_running_loop()`` from a
    sync tool body succeeds because ``registered.call`` is invoked inside
    the asyncio.run coroutine.
    """
    import asyncio

    registry = ToolRegistry()

    @registry.register(description="Probes running loop")
    def probe() -> str:
        loop = asyncio.get_running_loop()
        return f"loop-ok:{type(loop).__name__}"

    with _started_server(registry) as server, _connect(server) as sock:
        _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "probe", "params": {}})
        frame = _read_frame(sock)
    assert frame["success"] is True
    assert frame["data"]["content"][0]["text"].startswith("loop-ok:")


def test_close_safe_from_inside_tool_body_no_self_join_runtime_error() -> None:
    """F10/F22: tool body calling close() doesn't self-join its own handler thread."""
    registry = ToolRegistry()
    close_completed = threading.Event()
    close_error: list[BaseException] = []
    captured_server: list[PythonToolServer] = []

    @registry.register(description="Calls close from inside")
    def suicidal() -> str:
        try:
            captured_server[0]._close_sync()
        except BaseException as exc:  # noqa: BLE001
            close_error.append(exc)
            raise
        finally:
            close_completed.set()
        return "ok"

    server = PythonToolServer(registry)
    server._start_sync()
    captured_server.append(server)
    sock = socket.create_connection((server.endpoint.host, server.endpoint.port), timeout=2.0)
    try:
        _send_frame(sock, {"id": "e1", "type": "execute", "token": server.token, "tool": "suicidal", "params": {}})
        assert close_completed.wait(5.0)
    finally:
        with contextlib.suppress(OSError):
            sock.close()
    assert not close_error, f"close from handler raised: {close_error}"


def test_concurrent_close_calls_are_safe() -> None:
    """F11: two threads calling close() concurrently don't race on server_close()."""
    registry = ToolRegistry()
    server = PythonToolServer(registry)
    server._start_sync()
    errors: list[BaseException] = []

    def close_call() -> None:
        try:
            server._close_sync()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=close_call) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
    assert not errors, f"concurrent close raised: {errors}"


def test_manifest_with_in_band_violation_bytes_flagged(caplog: pytest.LogCaptureFixture) -> None:
    """F16: extra bytes after a manifest frame are also flagged as protocol violation."""
    import logging

    caplog.set_level(logging.WARNING, logger="libharness.pi.server")
    registry = ToolRegistry()

    @registry.register(description="One")
    def one() -> str:
        return "one"

    with _started_server(registry) as server, _connect(server) as sock:
        body = dumps_line({"id": "m1", "type": "manifest", "token": server.token}) + b"XYZ"
        sock.sendall(body)
        frame = _read_frame(sock, timeout=3.0)

    assert frame["success"] is True
    assert frame.get("protocolViolation") is True
    assert any("protocol violation" in rec.getMessage() for rec in caplog.records)


def test_harness_passes_bridge_write_timeout_to_server() -> None:
    """F13: PiPythonHarness threads bridge_write_timeout through to the server."""
    from libharness.pi import PiPythonHarness

    registry = ToolRegistry()
    h = PiPythonHarness(registry, bridge_write_timeout=0.25, bridge_max_handlers=8)
    assert h.server.bridge_write_timeout == 0.25
    assert h.server.max_handlers == 8


# ---------------------------------------------------------------------------
# Internal helpers used by tests above
# ---------------------------------------------------------------------------


async def _async_sleep(seconds: float) -> None:
    """``asyncio.sleep`` re-exported so non-asyncio tests can ``await`` it.

    Used inside async tool bodies; we can't ``await asyncio.sleep`` directly
    from a sync test, but the tool body itself runs inside the per-execute
    ``asyncio.run`` loop, so this works fine.
    """
    import asyncio

    await asyncio.sleep(seconds)
