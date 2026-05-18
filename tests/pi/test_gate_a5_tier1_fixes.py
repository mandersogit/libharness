"""Regression tests for the 7 Tier-1 findings from Gate A.5.

Sources:

- dev-notes/2026-05-17-v8-port-review-synthesis.md (synthesizer output)
- 8 raw reviewer outputs at /tmp/v8-port-review-{codex,opus}-*.md

Each test pins the post-fix behavior for one of F1-F7 plus F20 (folded into
the same commit as F1 per the synthesizer's recommendation).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
from typing import Any

import pytest

from libharness.pi.agent import PiAgentHarness
from libharness.pi.jsonl import dumps_line
from libharness.pi.rpc import PiLaunchConfig, PiRpcClient
from libharness.pi.runtime import AsyncioLoopThread
from libharness.pi.server import PythonToolServer
from libharness.pi.tools import ToolRegistry

# ---------------------------------------------------------------------------
# F1: event.data falsy-coercion (server.py:199 — same family as Phase 5.5).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_data", [[], 0, "", False])
async def test_bridge_event_data_rejects_non_dict_falsy_values(bad_data: Any) -> None:
    """Mirror of the params test: bridge event/notify_event data must surface
    a wire-level error for non-dict falsy values, not silently dispatch."""
    captured: list[Any] = []

    async def handler(event, _cancelled, _wait, _request_id):  # type: ignore[no-untyped-def]
        captured.append(event)
        return None

    server = PythonToolServer(ToolRegistry(), event_handler=handler)
    endpoint = await server.start()
    try:
        reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
        request = {
            "id": "e1",
            "type": "event",
            "token": endpoint.token,
            "event": "tool_call",
            "data": bad_data,
        }
        writer.write(dumps_line(request))
        await writer.drain()
        frame = json.loads((await reader.readline()).decode("utf-8"))
        assert frame["type"] == "response"
        assert frame["success"] is False, (
            f"expected error for data={bad_data!r}, got success {frame!r}"
        )
        assert "event.data must be an object" in frame.get("error", "")
        # Handler must NOT have been dispatched.
        assert captured == [], f"handler dispatched despite bad data: {captured!r}"
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# F2: bridge envelope `event` must match inner `data.type` (server.py:198-203).
# ---------------------------------------------------------------------------


async def test_bridge_envelope_mismatched_inner_type_rejected() -> None:
    """If the inner `data.type` disagrees with the envelope `event`, the
    dispatch must be rejected loudly, not silently confused.
    """
    fired: list[Any] = []

    async def handler(event, _cancelled, _wait, _request_id):  # type: ignore[no-untyped-def]
        fired.append(event)
        return None

    server = PythonToolServer(ToolRegistry(), event_handler=handler)
    endpoint = await server.start()
    try:
        reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
        # Envelope says "tool_call"; inner says "session_compact".
        request = {
            "id": "mismatch-1",
            "type": "event",
            "token": endpoint.token,
            "event": "tool_call",
            "data": {"type": "session_compact", "extra": "stuff"},
        }
        writer.write(dumps_line(request))
        await writer.drain()
        frame = json.loads((await reader.readline()).decode("utf-8"))
        assert frame["type"] == "response"
        assert frame["success"] is False
        assert "does not match" in frame.get("error", "")
        assert fired == [], "handler must not dispatch on mismatched envelope"
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()


async def test_bridge_envelope_matching_inner_type_passes() -> None:
    """When envelope and inner agree, dispatch proceeds; envelope wins on the
    final event["type"].
    """
    fired: list[Any] = []

    async def handler(event, _cancelled, _wait, _request_id):  # type: ignore[no-untyped-def]
        fired.append(dict(event))
        return None

    server = PythonToolServer(ToolRegistry(), event_handler=handler)
    endpoint = await server.start()
    try:
        reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
        request = {
            "id": "match-1",
            "type": "event",
            "token": endpoint.token,
            "event": "tool_call",
            "data": {"type": "tool_call", "tool_name": "x"},
        }
        writer.write(dumps_line(request))
        await writer.drain()
        frame = json.loads((await reader.readline()).decode("utf-8"))
        assert frame["type"] == "response"
        assert frame["success"] is True
        assert len(fired) == 1
        assert fired[0]["type"] == "tool_call"
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# F3: UI handler exception must not kill the RPC reader (rpc.py:417-438).
# ---------------------------------------------------------------------------


async def test_ui_handler_exception_does_not_kill_reader() -> None:
    """A raising UI handler must fall through to the framework default,
    not propagate up and tear down the reader task.
    """
    config = PiLaunchConfig(pi_command=[sys.executable, "-c", "import time; time.sleep(60)"])
    client = PiRpcClient(config)

    captured: list[dict[str, Any]] = []

    async def capture(frame: dict[str, Any]) -> None:
        captured.append(frame)

    client._send_extension_ui_response = capture  # type: ignore[method-assign]

    def raising_handler(_req: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("simulated UI handler bug")

    client.set_extension_ui_handler("confirm", raising_handler)

    request = {"type": "extension_ui_request", "id": "ui-1", "method": "confirm"}
    # Should NOT raise.
    await client._handle_extension_ui_request(request)

    assert len(captured) == 1
    frame = captured[0]
    assert frame["id"] == "ui-1"
    assert frame["type"] == "extension_ui_response"
    # Default for "confirm" is {"confirmed": False}.
    assert frame.get("confirmed") is False

    # Subsequent call must still work.
    captured.clear()
    await client._handle_extension_ui_request(
        {"type": "extension_ui_request", "id": "ui-2", "method": "confirm"}
    )
    assert len(captured) == 1
    assert captured[0]["id"] == "ui-2"


# ---------------------------------------------------------------------------
# F4: Event subscriber exception must not kill the reader (rpc.py:410-415).
# ---------------------------------------------------------------------------


async def test_event_subscriber_exception_does_not_kill_reader() -> None:
    """A raising subscriber must not block other subscribers from receiving
    the event, and must not raise out of _dispatch_event."""
    config = PiLaunchConfig(pi_command=[sys.executable, "-c", "import time; time.sleep(60)"])
    client = PiRpcClient(config)

    recorded: list[Any] = []

    def raising_handler(_event: Any) -> None:
        raise RuntimeError("simulated subscriber bug")

    def recording_handler(event: Any) -> None:
        recorded.append(event["type"])

    client.on_event(raising_handler)
    client.on_event(recording_handler)

    # Should NOT raise.
    await client._dispatch_event({"type": "agent_start"})
    assert recorded == ["agent_start"], "good handler must run despite bad sibling"

    # Second dispatch — both handlers still installed; recorder still fires.
    await client._dispatch_event({"type": "agent_end"})
    assert recorded == ["agent_start", "agent_end"]


# ---------------------------------------------------------------------------
# F5: malformed bridge request must log a warning, not silently die.
# ---------------------------------------------------------------------------


async def test_malformed_bridge_request_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Sending invalid JSON to the bridge must produce a WARNING log entry
    with cause attached, even though no wire frame is written back."""
    server = PythonToolServer(ToolRegistry())
    endpoint = await server.start()
    try:
        with caplog.at_level(logging.WARNING, logger="libharness.pi.server"):
            reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
            writer.write(b"this is not json\n")
            await writer.drain()
            # Server should close the connection with no wire frame.
            tail = await reader.read()
            assert tail == b"", f"expected empty response, got {tail!r}"
            writer.close()
            await writer.wait_closed()
        # The log must mention "malformed bridge request".
        matched = [
            record for record in caplog.records if "malformed bridge request" in record.message
        ]
        assert matched, f"no malformed-request log; saw {[r.message for r in caplog.records]!r}"
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# F6: large bridge frames (>64 KiB) must dispatch, not silently truncate.
# ---------------------------------------------------------------------------


async def test_large_bridge_frame_dispatches() -> None:
    """Send a single bridge frame ~1 MiB; assert it is dispatched. Pre-fix,
    StreamReader's 64 KiB default would raise LimitOverrunError and the
    frame would silently disappear.
    """
    captured: list[Any] = []

    async def handler(event, _cancelled, _wait, _request_id):  # type: ignore[no-untyped-def]
        captured.append(event)
        return {"ack": True}

    server = PythonToolServer(ToolRegistry(), event_handler=handler)
    endpoint = await server.start()
    try:
        reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
        big_blob = "x" * (1 * 1024 * 1024)  # 1 MiB payload.
        request = {
            "id": "big-1",
            "type": "event",
            "token": endpoint.token,
            "event": "tool_call",
            "data": {"type": "tool_call", "blob": big_blob},
        }
        writer.write(dumps_line(request))
        await writer.drain()
        frame = json.loads((await reader.readline()).decode("utf-8"))
        assert frame["type"] == "response"
        assert frame["success"] is True
        assert len(captured) == 1
        assert captured[0]["blob"] == big_blob
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# F7a: AsyncioLoopThread.start() under concurrent callers spawns exactly one.
# ---------------------------------------------------------------------------


def test_asyncio_loop_thread_start_is_thread_safe() -> None:
    """N concurrent callers of start() must observe exactly one loop thread.
    Pre-fix, the lock was released before _ready.wait() so two callers could
    both construct and start a Thread; the second would stomp self._thread.
    """
    loop_thread = AsyncioLoopThread(thread_name="GateA5-F7a")
    observed_idents: list[int] = []
    observed_lock = threading.Lock()

    def caller() -> None:
        loop_thread.start()
        # Touch the loop accessor — should be the same loop for every caller.
        _ = loop_thread.loop
        with observed_lock:
            assert loop_thread.thread is not None
            observed_idents.append(loop_thread.thread.ident or -1)

    try:
        threads = [threading.Thread(target=caller) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        # All 8 callers must see the SAME thread ident.
        assert len(set(observed_idents)) == 1, (
            f"expected exactly one ident, got {set(observed_idents)!r}"
        )
    finally:
        loop_thread.stop(timeout=5.0)


# ---------------------------------------------------------------------------
# F7b: PiAgentHarness.start_owner_thread() concurrent callers exact-one.
# ---------------------------------------------------------------------------


def test_pi_agent_harness_owner_thread_start_is_thread_safe() -> None:
    """Concurrent start_owner_thread callers must end up with one owner thread.
    Pre-fix, racing callers could double-set _owner_thread and the second
    call to _owner_ready.set_result would hit InvalidStateError.
    """
    # threaded=True but defer start so we can race start_owner_thread() ourselves.
    harness = PiAgentHarness(start_owner_thread=False)
    try:
        observed_idents: list[int] = []
        observed_lock = threading.Lock()

        def caller() -> None:
            harness.start_owner_thread()
            assert harness._owner_thread is not None
            with observed_lock:
                observed_idents.append(harness._owner_thread.ident or -1)

        threads = [threading.Thread(target=caller) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert len(set(observed_idents)) == 1, (
            f"expected exactly one owner ident, got {set(observed_idents)!r}"
        )
    finally:
        harness.close()
