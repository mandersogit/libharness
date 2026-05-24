"""Regression: precompiled pi binary works as a drop-in under v8 PiAgentHarness.

Locks in the load-bearing claim from
``dev-notes/2026-05-23-deno-as-pi-runtime-spike.md`` — that the
earendil-works precompiled pi binary resolves bundled-module specifiers
(``@earendil-works/pi-coding-agent``, ``typebox``, undici, …) when used
as the runtime for a libharness-supplied external ``--extension <path>``.

Skipped unless ``PI_PRECOMPILED`` points at a precompiled pi binary
(e.g. the one extracted under ``experiments/precompiled-pi-spike/pi/pi``
by following the dev-note's setup procedure). Uses the faux provider so
it stays offline — the live-LLM corner is covered by the experiment
smoke scripts, not here.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from libharness import _pi_vendor
from libharness.pi import PiAgentHarness, PiLaunchConfig, ToolRegistry, ToolResult


def _resolve_precompiled_binary() -> Path | None:
    """PI_PRECOMPILED env var wins; otherwise fall back to the vendored binary."""
    env = os.environ.get("PI_PRECOMPILED", "")
    if env:
        return Path(env)
    vendored = _pi_vendor.vendored_pi_binary()
    if vendored.exists():
        return vendored
    return None


_PI_BIN = _resolve_precompiled_binary()


@pytest.mark.live
@pytest.mark.skipif(
    _PI_BIN is None,
    reason="no precompiled pi available: set PI_PRECOMPILED to a binary path, "
    "or run `make install-pi` to populate the vendored binary "
    "(see dev-notes/2026-05-24-pi-vendoring-design.md)",
)
def test_precompiled_pi_binary_faux_roundtrip() -> None:
    pi_bin = _PI_BIN
    assert pi_bin is not None, "guarded by skipif above"
    assert pi_bin.exists(), f"precompiled pi path does not exist: {pi_bin}"

    registry = ToolRegistry()
    calls: list[dict[str, Any]] = []

    @registry.register(description="Echo back the given message exactly.")
    def echo(message: str) -> ToolResult:
        calls.append({"message": message})
        return ToolResult.text(f"echo:{message}")

    expected = "compiled pi regression"

    config = PiLaunchConfig(
        pi_command=[str(pi_bin)],
        request_timeout=60,
        no_builtin_tools=True,
    )

    with PiAgentHarness(
        registry,
        config=config,
        fake_provider=True,
        fake_tool_name="echo",
        fake_tool_args={"message": expected},
    ) as h:
        state = h.get_state()
        assert state.get("sessionId"), "pi failed to start a session"
        h.prompt_and_wait("trigger", timeout=60)

    assert calls, "faux provider did not trigger the Python tool"
    assert calls[0].get("message") == expected, f"unexpected tool args: {calls}"
