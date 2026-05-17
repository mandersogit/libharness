"""Regression tests for three v8 bugs cherry-picked into libharness.pi.

The bugs were surfaced by the threads-rewrite Claude's adversarial review on
the parallel branch and verified to exist in v8 source pre-port (see
``dev-notes/2026-05-17-claude-recommendations-for-v8-port.md`` Checks 1/4/7).
Each test below pins the post-fix behavior so a future "vendor a newer v8"
operation won't silently re-introduce the regression.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import pytest

from libharness.pi.jsonl import dumps_line
from libharness.pi.rpc import PiLaunchConfig, PiRpcClient, PiRpcProcessError
from libharness.pi.server import PythonToolServer
from libharness.pi.tools import ToolRegistry

# ---------------------------------------------------------------------------
# Check 1: extension_ui_response frame must not be corrupted by handler dict.
# ---------------------------------------------------------------------------


async def test_extension_ui_response_framework_keys_win() -> None:
    """A malicious or buggy UI handler must not override the framework's id/type.

    Pre-fix wire-construction (``{"type": ..., "id": ..., **response}``) let a
    handler return ``{"id": "EVIL"}`` and silently corrupt the correlation id pi
    sees. The fix spreads ``response`` first so framework keys override.
    """
    config = PiLaunchConfig(pi_command=[sys.executable, "-c", "import time; time.sleep(60)"])
    client = PiRpcClient(config)

    captured: list[dict[str, Any]] = []

    async def capture(frame: dict[str, Any]) -> None:
        captured.append(frame)

    # Monkey-patch the wire-write so we never need a real pi.
    client._send_extension_ui_response = capture  # type: ignore[method-assign]

    def attacker(_req: dict[str, Any]) -> dict[str, Any]:
        return {"id": "EVIL", "type": "evil-type", "confirmed": True}

    client.set_extension_ui_handler("confirm", attacker)

    request = {"type": "extension_ui_request", "id": "real-1", "method": "confirm"}
    await client._handle_extension_ui_request(request)

    assert len(captured) == 1
    frame = captured[0]
    assert frame["id"] == "real-1", "handler must not override framework correlation id"
    assert frame["type"] == "extension_ui_response", (
        "handler must not override framework frame type"
    )
    assert frame["confirmed"] is True, "non-framework keys from the handler must pass through"


# ---------------------------------------------------------------------------
# Check 4: execute.params must reject falsy non-dict values, not coerce them.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_params", [[], 0, "", False])
async def test_execute_params_rejects_non_dict_falsy_values(bad_params: Any) -> None:
    """The server must surface a wire-level error for non-dict params, not silently
    treat ``[]`` / ``0`` / ``""`` / ``False`` as an empty dict and dispatch the tool.

    Pre-fix code (``request.get("params") or {}``) coerced every falsy value
    to ``{}`` before the isinstance type check could see it. The fix uses
    ``request.get("params", {})`` plus an explicit None-check.
    """
    registry = ToolRegistry()

    @registry.register(description="Never-called probe")
    async def probe() -> str:
        raise AssertionError("tool should not be dispatched for malformed params")

    server = PythonToolServer(registry)
    endpoint = await server.start()
    try:
        reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
        request = {
            "id": "p1",
            "type": "execute",
            "token": endpoint.token,
            "tool": "probe",
            "params": bad_params,
        }
        writer.write(dumps_line(request))
        await writer.drain()
        frame = json.loads((await reader.readline()).decode("utf-8"))
        assert frame["type"] == "response"
        assert frame["success"] is False, (
            f"expected error for params={bad_params!r}, got success frame {frame!r}"
        )
        assert "params must be an object" in frame.get("error", "")
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# Check 7: startup-probe failure must clean up reader tasks before raising.
# ---------------------------------------------------------------------------


async def test_startup_probe_cleanup_on_pi_exit() -> None:
    """When the startup probe detects pi already exited, the reader tasks must
    be cancelled and awaited (not left dangling on the event loop) and the
    subprocess must be reaped before ``PiRpcProcessError`` is raised.

    Pre-fix code raised immediately, leaking ``_reader_task`` / ``_stderr_task``
    and the unreaped subprocess into the loop's task set. Verified by launching
    a "pi" command that exits non-zero immediately.
    """
    config = PiLaunchConfig(
        pi_command=[sys.executable, "-c", "import sys; sys.exit(1)"],
        startup_timeout=0.5,
    )
    client = PiRpcClient(config)

    with pytest.raises(PiRpcProcessError, match="exited during startup"):
        await client.start()

    # After the failed startup the cleanup helper resets both reader-task
    # slots back to None. The previous tasks were cancelled + awaited as
    # part of that cleanup; we don't have refs to them anymore, which is
    # the point — they're not dangling on the loop.
    assert client._reader_task is None
    assert client._stderr_task is None

    # The subprocess handle should have a definite returncode (was reaped).
    assert client.process is not None
    assert client.process.returncode is not None
