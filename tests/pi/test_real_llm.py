"""Real-LLM live test: a real model calls a Python-authored tool through pi.

Requires:

- the sandboxed pi install (``make install-pi``)
- a valid OAuth credential at ``.sandbox/pi-home/.pi/agent/auth.json``
  (``make login`` once to populate)

Both faux-provider tests and this one share the ``live`` marker; this test
additionally skips on missing OAuth credentials so contributors without a
token can still run ``make test-live`` without spurious failures.

The test deliberately makes the model's instruction unambiguous ("Call
the `echo` tool ...") to keep flakiness low, and asserts the tool ran with
the expected argument rather than asserting any specific model wording.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from libharness.pi import PiLaunchConfig, PiPythonHarness, ToolRegistry, ToolResult

PI_CLI = os.environ.get("PI_CLI", "")
AUTH_PATH = Path.home() / ".pi" / "agent" / "auth.json"


def _pi_command() -> list[str]:
    if PI_CLI.endswith(".js"):
        return ["node", PI_CLI]
    if PI_CLI.endswith(".py"):
        return [sys.executable, PI_CLI]
    return [PI_CLI]


@pytest.mark.live
@pytest.mark.skipif(not PI_CLI, reason="set PI_CLI to a Pi CLI for live tests")
@pytest.mark.skipif(
    not AUTH_PATH.exists() or AUTH_PATH.stat().st_size < 100,
    reason=f"no OAuth credentials at {AUTH_PATH}; run `make login`",
)
async def test_real_llm_calls_python_tool() -> None:
    """Round-trip: a real LLM is prompted to call a Python tool; we verify it did."""
    registry = ToolRegistry()
    calls: list[dict[str, Any]] = []

    @registry.register(description="Echo back the given message exactly.")
    def echo(message: str) -> ToolResult:
        calls.append({"message": message})
        return ToolResult.text(f"echo:{message}")

    config = PiLaunchConfig(
        pi_command=_pi_command(),
        offline=False,
        request_timeout=180,
        startup_timeout=10,
        no_builtin_tools=True,
    )

    async with PiPythonHarness(registry, config=config) as harness:
        state = await harness.client.get_state()
        assert state.get("sessionId"), "pi failed to start a session"
        await harness.client.prompt_and_wait(
            'Use the `echo` tool to echo the string "hello from libharness". '
            "Call the tool directly; do not just answer in text.",
            timeout=180,
        )

    assert calls, "the model did not call the echo tool at all"
    assert any("hello from libharness" in (c.get("message") or "") for c in calls), (
        f"echo was called but with unexpected args: {calls}"
    )
