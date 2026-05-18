"""Regression tests for ergonomic-pass cluster 3: shim generation hardening.

Covers A.2 / F27 (the TS shim's ``DECISION_EVENTS`` array is generated from
Python's :class:`AgentHookSurface._DECISION_EVENT_NAMES` so the two surfaces
cannot drift) and F36 (the TS bridge wire frame in ``bridgeCall`` /
``bridgeNotify`` puts framework keys *after* the user-supplied payload spread
so a payload containing ``type``/``id``/``token`` cannot silently corrupt the
frame — mirror of the Python-side Phase 5.5 fix at ``rpc.py``).

Sources:
- ``dev-notes/2026-05-17-v8-port-deferred-items.md`` § A.2, F36.
- ``dev-notes/2026-05-17-claude-recommendations-for-v8-port.md`` § Check 1.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from libharness.pi.hook_surface import AgentHookSurface
from libharness.pi.shim import (
    PRODUCTION_TS_SHIM,
    _render_decision_events_ts_array,
    write_bridge_shim,
)

# ---------------------------------------------------------------------------
# A.2 / F27: DECISION_EVENTS TS array is generated from the Python frozenset.
# ---------------------------------------------------------------------------


def _extract_decision_events_from_ts(text: str) -> list[str]:
    """Return the string-literal event names found inside the TS array."""

    match = re.search(
        r"const DECISION_EVENTS = \[(.+?)\] as const;",
        text,
        re.DOTALL,
    )
    assert match is not None, "DECISION_EVENTS array not found in generated TS"
    body = match.group(1)
    return re.findall(r'"([a-z_]+)"', body)


def test_generated_shim_decision_events_matches_python_source(tmp_path: Path) -> None:
    """The generated shim's ``DECISION_EVENTS`` array must equal the Python frozenset.

    Eliminates A.2 / F27 — a new decision event added to Python no longer needs
    a hand-edit on the TS side; future edits to ``_DECISION_EVENT_NAMES`` flow
    through automatically.
    """

    out = write_bridge_shim(tmp_path / "shim.ts")
    text = out.read_text(encoding="utf-8")

    ts_events = _extract_decision_events_from_ts(text)
    py_events = sorted(AgentHookSurface._DECISION_EVENT_NAMES)

    # Order is significant: the renderer pins a stable sorted order so the
    # generated TS is byte-deterministic across runs / machines.
    assert ts_events == py_events
    assert len(ts_events) == len(set(ts_events)), "TS event list has duplicates"


def test_render_decision_events_ts_array_is_sorted_and_deterministic() -> None:
    """Two renders must be byte-identical (no set-iteration nondeterminism)."""

    first = _render_decision_events_ts_array()
    second = _render_decision_events_ts_array()
    assert first == second

    # The body should be lines of the form `  "name",`, sorted ascending.
    names = re.findall(r'"([a-z_]+)"', first)
    assert names == sorted(names)
    assert names == sorted(AgentHookSurface._DECISION_EVENT_NAMES)


def test_production_ts_shim_contains_placeholder_not_hardcoded_array() -> None:
    """The raw TS template must reference ``__DECISION_EVENTS__``, not an inline list.

    Guards against a future edit that re-inlines the array and reintroduces the
    drift vector.
    """

    assert "__DECISION_EVENTS__" in PRODUCTION_TS_SHIM

    # The hardcoded event names from before the fix must NOT appear in the raw
    # template (they only appear once `write_bridge_shim` interpolates).
    raw_event_block_re = re.compile(
        r'const DECISION_EVENTS = \[\s*"[a-z_]+"',
        re.MULTILINE,
    )
    assert not raw_event_block_re.search(PRODUCTION_TS_SHIM), (
        "PRODUCTION_TS_SHIM appears to inline event-name string literals; "
        "the array must be generated from AgentHookSurface._DECISION_EVENT_NAMES"
    )


# ---------------------------------------------------------------------------
# F36: bridgeCall / bridgeNotify wire frames spread the payload BEFORE the
# framework keys so a user-supplied payload cannot override `type`/`id`/`token`.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("function_name", ["bridgeCall", "bridgeNotify"])
def test_bridge_wire_frame_framework_keys_win(function_name: str) -> None:
    """Each bridge function must spread the payload BEFORE the framework keys.

    Mirror of the Python-side Phase 5.5 fix at ``rpc.py`` (extension_ui_response).
    In TypeScript, object-literal spread is last-wins: framework keys *after*
    the payload spread cannot be silently overridden by a user-supplied
    ``type``/``id``/``token``.
    """

    # Locate the function body in the embedded TS template.
    pattern = re.compile(
        rf"function {function_name}\b.*?\bconst request = \{{(?P<body>[^}}]+)\}};",
        re.DOTALL,
    )
    match = pattern.search(PRODUCTION_TS_SHIM)
    assert match is not None, f"could not find request-frame literal in {function_name}"

    body = match.group("body").strip()

    # The safe shape spreads `payload` first, then frames `id`, `type`, `token`.
    assert "...payload" in body, f"{function_name} request frame missing `...payload` spread"

    spread_index = body.index("...payload")
    # All three framework keys must appear AFTER the spread.
    for framework_key in ("id", "type", "token"):
        # We search for the bare identifier as a key — guard against substring
        # matches by requiring a word boundary on both sides.
        key_match = re.search(rf"\b{framework_key}\b", body[spread_index:])
        assert key_match is not None, (
            f"{function_name} request frame missing framework key {framework_key!r} "
            f"after `...payload` spread"
        )

    # The unsafe shape would have `...payload` as the LAST element of the
    # object literal — explicitly reject that.
    trailing = body[spread_index:].rstrip().rstrip(",").strip()
    assert trailing != "...payload", (
        f"{function_name} request frame ends with `...payload` — user payload "
        f"would override framework keys. Move the spread to the front."
    )


# ---------------------------------------------------------------------------
# F19: _decision_timeouts_for_manifest filters to open gates.
# ---------------------------------------------------------------------------


def test_decision_timeouts_for_manifest_filters_to_open_gates() -> None:
    """Pre-fix: every entry in `_decision_timeouts_ms` shipped in the manifest,
    even for events whose gates were closed (no `decide_X` hook defined).
    Post-fix: only events with an open gate get a manifest timeout."""
    from libharness.pi import Agent, AgentEvent

    class SelectiveTimeoutAgent(Agent):
        # Two events with timeouts, but only one has a decide hook.
        _decision_timeouts_ms = {  # type: ignore[assignment]
            "tool_call": 5000,
            "session_compact": 10000,
        }

        def decide_tool_call(self, event: AgentEvent) -> object | None:
            _ = event
            return None

    timeouts = SelectiveTimeoutAgent._decision_timeouts_for_manifest()
    assert "tool_call" in timeouts, "tool_call has decide_X; gate is open"
    assert "session_compact" not in timeouts, (
        "session_compact has no decide_X; gate is closed — must not be in manifest"
    )
    assert timeouts["tool_call"] == 5000


def test_decision_timeouts_for_manifest_with_global_default_filters_to_open_gates() -> None:
    """When the global `_decision_timeout_ms` is set, only open-gate events
    pick it up."""
    from libharness.pi import Agent, AgentEvent

    class GlobalTimeoutAgent(Agent):
        _decision_timeout_ms = 3000

        def decide_tool_call(self, event: AgentEvent) -> object | None:
            _ = event
            return None

    timeouts = GlobalTimeoutAgent._decision_timeouts_for_manifest()
    # tool_call (open gate) gets the global default; the other 18 decision
    # events without hooks do NOT (their gates are closed).
    assert timeouts == {"tool_call": 3000}


# ---------------------------------------------------------------------------
# F24: failed start() nulls self.process so retry works.
# ---------------------------------------------------------------------------


async def test_failed_startup_allows_retry() -> None:
    """After a failed `start()`, calling `start()` again must not raise
    'PiRpcClient is already started' — `self.process` was nulled by cleanup."""
    import sys

    from libharness.pi.rpc import PiLaunchConfig, PiRpcClient, PiRpcProcessError

    config = PiLaunchConfig(
        pi_command=[sys.executable, "-c", "import sys; sys.exit(1)"],
        startup_timeout=0.5,
    )
    client = PiRpcClient(config)
    # First start fails.
    with pytest.raises(PiRpcProcessError, match="exited during startup"):
        await client.start()
    # Process slot is cleared.
    assert client.process is None
    # Second start fails the same way — not with "already started".
    with pytest.raises(PiRpcProcessError, match="exited during startup"):
        await client.start()
    assert client.process is None


# ---------------------------------------------------------------------------
# F33: close() unblocks next_event() / wait_for_event() waiters.
# ---------------------------------------------------------------------------


async def test_close_unblocks_pending_next_event_waiter() -> None:
    """A coroutine awaiting `next_event()` must unblock with a PiRpcError when
    the client is closed concurrently, rather than hanging on the queue."""
    import asyncio
    import sys

    from libharness.pi.rpc import PiLaunchConfig, PiRpcClient, PiRpcError

    config = PiLaunchConfig(
        pi_command=[sys.executable, "-c", "import time; time.sleep(60)"],
        startup_timeout=0.5,
    )
    client = PiRpcClient(config)
    # Don't actually start pi — we don't need a real subprocess for this test.
    # Just trigger close() to fire the close_event; next_event() should see it.

    async def waiter() -> str:
        try:
            await client.next_event()
            return "got event"
        except PiRpcError as exc:
            return f"raised: {exc.error}"

    # Closing without ever starting (`process is None`) short-circuits in
    # `close()` early — but `_closed = True` and `_close_event.set()` still
    # happen first. So a waiter started after close() raises immediately.
    waiter_task = asyncio.create_task(waiter())
    # Give the waiter time to enter `next_event()`.
    await asyncio.sleep(0.05)
    await client.close()
    result = await asyncio.wait_for(waiter_task, timeout=2.0)
    assert "PiRpcClient" in result or "closed" in result, (
        f"expected close-related error, got: {result!r}"
    )


async def test_next_event_on_closed_client_raises_immediately() -> None:
    """A `next_event()` call on an already-closed client raises immediately."""
    from libharness.pi.rpc import PiLaunchConfig, PiRpcClient, PiRpcError

    client = PiRpcClient(PiLaunchConfig())
    await client.close()
    with pytest.raises(PiRpcError, match="closed"):
        await client.next_event()


# ---------------------------------------------------------------------------
# F34: caller-supplied duplicate request IDs are rejected.
# ---------------------------------------------------------------------------


async def test_duplicate_request_id_rejected() -> None:
    """If a caller supplies an `id` already present in `_pending`, `send()`
    must reject the collision rather than overwrite the future (which would
    orphan the prior caller)."""
    import asyncio
    import sys

    from libharness.pi.rpc import PiLaunchConfig, PiRpcClient, PiRpcError

    # Use a long-running fake pi so we get a real subprocess but no responses.
    config = PiLaunchConfig(
        pi_command=[sys.executable, "-c", "import time; time.sleep(60)"],
        startup_timeout=0.1,
    )
    client = PiRpcClient(config)
    await client.start()
    try:
        # Manually insert a pending future under id "X" without ever sending.
        loop = asyncio.get_running_loop()
        client._pending["X"] = loop.create_future()

        # send() with the same id should reject.
        with pytest.raises(PiRpcError, match="duplicate request id"):
            await client.send({"type": "ping", "id": "X"})
    finally:
        await client.close()
