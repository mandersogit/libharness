"""Mode-driven fake pi process for tests.

Each instance runs as a subprocess that reads JSONL on stdin and writes JSONL
on stdout. Mode is selected via the ``LIBHARNESS_FAKE_PI_MODE`` environment
variable; default ``basic``.

Synchronization with the test happens via env-var paths:

* ``LIBHARNESS_FAKE_PI_LOG`` — path the process appends an ordered log of
  observed stdin lines to (for ``ui-order``, ``parallel-execute``).
* ``LIBHARNESS_FAKE_PI_BARRIER`` — path the process waits to exist before
  emitting a delayed response (for ``late-response``).
* ``LIBHARNESS_FAKE_PI_DELAY_MS`` — sleep this many ms before each stdin
  read (for ``slow-consumer``).

Modes:

* ``basic`` — what the original ``fake_pi_rpc.py`` did. Backwards-compatible.
* ``ui-order`` — on ``get_state``, emits ``extension_ui_request`` first, then
  records the order of subsequent stdin frames. Used to verify the
  UI-response priority barrier in phase 3.
* ``late-response`` — on ``prompt``, withholds the response frame until the
  barrier file exists. Used for close-during-pending stress in phase 3.
* ``fatal-invalid-json`` — emits a deliberately malformed line after one
  successful exchange. Used to verify fatal-state propagation in phase 3.
* ``parallel-execute`` — emits two ``extension_ui_request`` calls back-to-back
  with distinct ids, then waits for both responses in any order.
* ``slow-consumer`` — delays each stdin read by ``LIBHARNESS_FAKE_PI_DELAY_MS``
  milliseconds. Used for ``bridge_write_timeout`` and slow-consumer tests.

The contract is intentionally narrow — these scenarios exist to give the
phase-3 ``rpc.py`` rewrite deterministic edge-case coverage.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


def _send(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _log_stdin(raw: str) -> None:
    path = os.environ.get("LIBHARNESS_FAKE_PI_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(raw.rstrip("\n") + "\n")


def _wait_for_barrier() -> None:
    path = os.environ.get("LIBHARNESS_FAKE_PI_BARRIER")
    if not path:
        return
    target = Path(path)
    deadline = time.monotonic() + 30.0
    while not target.exists():
        if time.monotonic() > deadline:
            return  # don't hang forever
        time.sleep(0.02)


def _slow_consumer_delay() -> None:
    raw = os.environ.get("LIBHARNESS_FAKE_PI_DELAY_MS")
    if not raw:
        return
    with contextlib.suppress(ValueError):
        time.sleep(float(raw) / 1000.0)


def _iter_stdin() -> Any:
    for raw in sys.stdin:
        if not raw.strip():
            continue
        _log_stdin(raw)
        _slow_consumer_delay()
        yield raw


def _handle_basic(request: dict[str, Any]) -> None:
    typ = request.get("type")
    if typ == "get_state":
        _send({"type": "extension_ui_request", "id": "ui-1", "method": "confirm", "message": "continue?"})
        response = json.loads(sys.stdin.readline())
        if response.get("type") != "extension_ui_response" or response.get("confirmed") is not False:
            _send({"type": "response", "id": request.get("id"), "success": False, "error": "bad UI response"})
        else:
            _send({"type": "response", "id": request.get("id"), "success": True, "data": {"sessionId": "fake", "isStreaming": False}})
    elif typ == "prompt":
        _send({"type": "response", "id": request.get("id"), "success": True, "data": {}})
        _send({"type": "agent_start"})
        _send({"type": "agent_end"})
    elif typ == "set_model":
        _send({"type": "response", "id": request.get("id"), "success": True, "data": {"received": request}})
    else:
        _send({"type": "response", "id": request.get("id"), "success": True, "data": {}})


def _handle_ui_order(request: dict[str, Any]) -> None:
    typ = request.get("type")
    if typ == "get_state":
        # Emit UI request first; the rpc reader should pause normal sends
        # until the UI response is written.
        _send({"type": "extension_ui_request", "id": "ui-priority", "method": "confirm", "message": "go?"})
        # Now wait for the UI response on stdin. Any non-UI frame received
        # before the UI response is a priority-ordering violation.
        for raw in _iter_stdin():
            response = json.loads(raw)
            if response.get("type") == "extension_ui_response":
                _send({"type": "response", "id": request.get("id"), "success": True, "data": {"uiResponseId": response.get("id")}})
                return
            # Wrong order — surface it as a failure response.
            _send({"type": "response", "id": request.get("id"), "success": False, "error": f"out-of-order frame: {response}"})
            return
    elif typ == "prompt":
        _send({"type": "response", "id": request.get("id"), "success": True, "data": {}})
        _send({"type": "agent_end"})
    else:
        _send({"type": "response", "id": request.get("id"), "success": True, "data": {}})


def _handle_late_response(request: dict[str, Any]) -> None:
    typ = request.get("type")
    if typ == "prompt":
        _wait_for_barrier()
        _send({"type": "response", "id": request.get("id"), "success": True, "data": {}})
    else:
        _send({"type": "response", "id": request.get("id"), "success": True, "data": {}})


def _handle_fatal_invalid_json(request: dict[str, Any], state: dict[str, int]) -> None:
    state["count"] += 1
    if state["count"] == 1:
        # First request: respond normally.
        _send({"type": "response", "id": request.get("id"), "success": True, "data": {}})
        return
    # Subsequent request: emit an unterminated/non-JSON line on stdout.
    sys.stdout.write("not valid json at all\n")
    sys.stdout.flush()


def _handle_parallel_execute(request: dict[str, Any]) -> None:
    typ = request.get("type")
    if typ == "get_state":
        _send({"type": "extension_ui_request", "id": "ui-p1", "method": "confirm", "message": "one"})
        _send({"type": "extension_ui_request", "id": "ui-p2", "method": "confirm", "message": "two"})
        seen: set[str] = set()
        for raw in _iter_stdin():
            response = json.loads(raw)
            if response.get("type") == "extension_ui_response":
                seen.add(str(response.get("id")))
            if {"ui-p1", "ui-p2"}.issubset(seen):
                break
        _send({"type": "response", "id": request.get("id"), "success": True, "data": {"received": sorted(seen)}})
    else:
        _send({"type": "response", "id": request.get("id"), "success": True, "data": {}})


def main() -> None:
    mode = os.environ.get("LIBHARNESS_FAKE_PI_MODE", "basic")
    state: dict[str, int] = {"count": 0}
    for raw in _iter_stdin():
        request = json.loads(raw)
        if mode == "basic":
            _handle_basic(request)
        elif mode == "ui-order":
            _handle_ui_order(request)
        elif mode == "late-response":
            _handle_late_response(request)
        elif mode == "fatal-invalid-json":
            _handle_fatal_invalid_json(request, state)
        elif mode == "parallel-execute":
            _handle_parallel_execute(request)
        elif mode == "slow-consumer":
            # Slow-consumer mode behaves like ``basic`` but each read was
            # already delayed by ``_slow_consumer_delay``.
            _handle_basic(request)
        else:
            _send({"type": "response", "id": request.get("id"), "success": False, "error": f"unknown mode {mode!r}"})


if __name__ == "__main__":
    main()
