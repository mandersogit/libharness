from __future__ import annotations

import asyncio
import copy
import threading
from typing import Literal

import pytest

from libharness.pi.tools import (
    MANIFEST_PROTOCOL_VERSION,
    ImmutableRegistry,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolResult,
)

# ---------------------------------------------------------------------------
# Pre-existing baseline tests (kept).
# ---------------------------------------------------------------------------


def test_registry_infers_schema_and_context() -> None:
    registry = ToolRegistry()

    @registry.register(description="Search things")
    def search(query: str, limit: int = 5, mode: Literal["fast", "deep"] = "fast", ctx: ToolContext | None = None) -> ToolResult:
        return ToolResult.text(f"{query}:{limit}:{mode}:{ctx is not None}")

    manifest = registry.manifest()
    assert manifest["protocolVersion"] == MANIFEST_PROTOCOL_VERSION == 1
    tool = manifest["tools"][0]
    assert tool["name"] == "search"
    assert tool["parameters"]["properties"]["query"]["type"] == "string"
    assert tool["parameters"]["properties"]["limit"]["type"] == "integer"
    assert tool["parameters"]["properties"]["mode"]["enum"] == ["fast", "deep"]
    assert "query" in tool["parameters"]["required"]
    assert "ctx" not in tool["parameters"]["properties"]


def test_duplicate_names_are_rejected() -> None:
    registry = ToolRegistry()

    @registry.register(name="same")
    def one() -> str:
        return "1"

    with pytest.raises(ToolError):
        @registry.register(name="same")
        def two() -> str:
            return "2"


# ---------------------------------------------------------------------------
# Phase 1 additions (per /tmp/phase1-test-plan.md).
# ---------------------------------------------------------------------------


# ToolContext._cancelled dual-event support


def test_tool_context_cancelled_accepts_threading_event() -> None:
    """ToolContext._cancelled accepts threading.Event (phase 2/3 will use this)."""
    event = threading.Event()
    ctx = ToolContext(tool_call_id="t1", tool_name="x", _cancelled=event)
    assert ctx.cancelled is False
    event.set()
    assert ctx.cancelled is True


def test_tool_context_cancelled_accepts_asyncio_event() -> None:
    """ToolContext._cancelled still works with asyncio.Event (current async server)."""
    event = asyncio.Event()
    ctx = ToolContext(tool_call_id="t1", tool_name="x", _cancelled=event)
    assert ctx.cancelled is False
    event.set()
    assert ctx.cancelled is True


def test_tool_context_cancelled_none() -> None:
    """ToolContext with no _cancelled (default) reports not cancelled."""
    ctx = ToolContext(tool_call_id="t1", tool_name="x")
    assert ctx.cancelled is False


# Snapshot isolation


def test_registry_snapshot_isolated_from_late_register() -> None:
    """snapshot() captures membership at snapshot time; late register() is invisible to the snapshot."""
    registry = ToolRegistry()

    @registry.register(name="first")
    def first() -> str:
        return "1"

    snap = registry.snapshot()
    assert "first" in snap
    assert "second" not in snap

    @registry.register(name="second")
    def second() -> str:
        return "2"

    # Snapshot still only has the first tool.
    assert "second" not in snap
    assert len(snap) == 1
    snap_manifest = snap.manifest()
    assert [t["name"] for t in snap_manifest["tools"]] == ["first"]

    # Live registry has both.
    assert "second" in registry
    live_manifest = registry.manifest()
    assert sorted(t["name"] for t in live_manifest["tools"]) == ["first", "second"]


def test_immutable_registry_manifest_is_defensive_copy() -> None:
    """Mutating a returned manifest dict must NOT alter the snapshot's state.

    Regression for codex/Opus phase-1 review: ``ToolSpec.to_manifest()``
    previously returned ``parameters`` by reference; a caller could mutate
    ``snap.manifest()["tools"][0]["parameters"]`` and corrupt all future
    ``snap.manifest()`` calls. Fix: ``to_manifest`` deep-copies parameters.
    """
    registry = ToolRegistry()

    @registry.register(
        name="leaky",
        parameters={"type": "object", "properties": {"x": {"type": "string"}}},
    )
    def leaky(x: str) -> str:
        return x

    snap = registry.snapshot()
    m1 = snap.manifest()
    m1["tools"][0]["parameters"]["properties"]["x"]["type"] = "MUTATED"
    m1["tools"][0]["parameters"]["properties"]["new_field"] = {"type": "boolean"}
    # Future manifest calls must be unaffected.
    m2 = snap.manifest()
    assert m2["tools"][0]["parameters"]["properties"]["x"]["type"] == "string"
    assert "new_field" not in m2["tools"][0]["parameters"]["properties"]


def test_registry_snapshot_does_not_deepcopy_callable_with_lock() -> None:
    """Snapshot must work when tool functions are callable instances holding locks.

    Regression for codex/Opus phase-1 review: deep-copying the whole
    ``RegisteredTool`` (including ``func``) failed with ``TypeError: cannot
    pickle '_thread.lock' object`` for callable instances containing locks.
    Fix: snapshot copies ``spec.parameters`` only, preserves ``func`` by reference.
    """
    class StatefulTool:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self.calls = 0

        def __call__(self) -> str:
            with self._lock:
                self.calls += 1
                return f"call-{self.calls}"

    registry = ToolRegistry()
    stateful = StatefulTool()
    registry.register(stateful, name="stateful")
    # Must not raise.
    snap = registry.snapshot()
    # Func reference is preserved (not deepcopied — that would clone the lock).
    assert snap.get("stateful").func is stateful


def test_registry_snapshot_manifest_isolated_from_late_metadata_mutation() -> None:
    """Snapshot must freeze ToolSpec.parameters dict; live mutation does NOT leak into snapshot."""
    registry = ToolRegistry()
    initial_schema = {"type": "object", "properties": {"x": {"type": "string"}}, "additionalProperties": False, "required": []}

    @registry.register(name="leaky", parameters=copy.deepcopy(initial_schema))
    def leaky(x: str) -> str:
        return x

    snap = registry.snapshot()
    snap_before = copy.deepcopy(snap.manifest())

    # Mutate the live registry's tool schema in-place.
    live_tool = registry.get("leaky")
    live_tool.spec.parameters["properties"]["x"]["type"] = "integer"
    live_tool.spec.parameters["properties"]["new_field"] = {"type": "boolean"}

    # Snapshot must be unchanged.
    snap_after = snap.manifest()
    assert snap_after == snap_before
    assert snap.get("leaky").spec.parameters["properties"]["x"]["type"] == "string"
    assert "new_field" not in snap.get("leaky").spec.parameters["properties"]

    # Live registry reflects the mutation.
    assert registry.get("leaky").spec.parameters["properties"]["x"]["type"] == "integer"


def test_immutable_registry_has_read_only_public_surface() -> None:
    """ImmutableRegistry exposes get/__contains__/__iter__/__len__/manifest only — no register()."""
    registry = ToolRegistry()

    @registry.register(name="t1")
    def t1() -> str:
        return "x"

    snap = registry.snapshot()
    assert isinstance(snap, ImmutableRegistry)
    assert not hasattr(snap, "register")
    assert not hasattr(snap, "tool")
    # Read-only surface works:
    assert "t1" in snap
    assert len(snap) == 1
    assert snap.get("t1").spec.name == "t1"
    assert list(snap)[0].spec.name == "t1"
    assert snap.manifest()["protocolVersion"] == MANIFEST_PROTOCOL_VERSION


def test_immutable_registry_unknown_tool_raises_tool_error() -> None:
    """ImmutableRegistry.get raises ToolError (not KeyError) for unknown names — matches ToolRegistry.get contract."""
    snap = ToolRegistry().snapshot()
    with pytest.raises(ToolError):
        snap.get("missing")


def test_registry_iter_uses_point_in_time_snapshot() -> None:
    """__iter__ snapshots under lock and releases before iteration: mid-iteration register() does not affect or fail the iterator."""
    registry = ToolRegistry()

    @registry.register(name="a")
    def a() -> str:
        return "a"

    @registry.register(name="b")
    def b() -> str:
        return "b"

    iterator = iter(registry)
    # Mutate during iteration (would raise `RuntimeError: dictionary changed size`
    # if __iter__ exposed a live view).
    @registry.register(name="c")
    def c() -> str:
        return "c"

    seen = list(iterator)
    seen_names = sorted(r.spec.name for r in seen)
    # Iterator yields the point-in-time snapshot: a and b only. "c" was registered
    # after iter() so is invisible to this iterator. (A fresh iter(registry) would
    # see all three.)
    assert seen_names == ["a", "b"]
    assert sorted(r.spec.name for r in registry) == ["a", "b", "c"]


# Concurrency under freethreading (3.14t signal)


def _run_concurrent(threads: list[threading.Thread]) -> None:
    """Start all threads and join them with a reasonable bound."""
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), f"Thread {t.name} did not finish in time"


@pytest.mark.timeout(60)
def test_registry_duplicate_name_is_atomic_under_concurrent_register() -> None:
    """N threads race to register the same name: exactly one wins, others get ToolError."""
    registry = ToolRegistry()
    barrier = threading.Barrier(8)
    results: list[Exception | None] = [None] * 8

    def worker(idx: int) -> None:
        barrier.wait()
        try:
            @registry.register(name="contested")
            def tool() -> str:
                return f"worker-{idx}"
            results[idx] = None
        except ToolError as exc:
            results[idx] = exc

    threads = [threading.Thread(target=worker, args=(i,), name=f"reg-{i}") for i in range(8)]
    _run_concurrent(threads)

    successes = [r for r in results if r is None]
    failures = [r for r in results if isinstance(r, ToolError)]
    assert len(successes) == 1, f"Expected exactly 1 successful registration, got {len(successes)}"
    assert len(failures) == 7
    # Manifest has exactly one tool with that name.
    manifest = registry.manifest()
    assert [t["name"] for t in manifest["tools"]] == ["contested"]


@pytest.mark.timeout(60)
def test_registry_concurrent_unique_registers_have_no_lost_updates() -> None:
    """N threads each register a unique name; final manifest contains all N."""
    registry = ToolRegistry()
    N = 32
    barrier = threading.Barrier(N)

    def worker(idx: int) -> None:
        barrier.wait()
        @registry.register(name=f"tool_{idx:03d}")
        def tool() -> str:
            return f"r{idx}"

    threads = [threading.Thread(target=worker, args=(i,), name=f"reg-{i}") for i in range(N)]
    _run_concurrent(threads)

    manifest = registry.manifest()
    names = sorted(t["name"] for t in manifest["tools"])
    expected = sorted(f"tool_{i:03d}" for i in range(N))
    assert names == expected
    assert len(set(names)) == N  # no duplicates


@pytest.mark.timeout(60)
def test_registry_concurrent_snapshot_and_register_are_consistent() -> None:
    """Writer thread registers unique names; reader thread takes snapshots concurrently.

    Every observed snapshot must be self-consistent (no partial entries, no missing
    schema keys, no duplicate names). On 3.14t, this is the FT-specific race target.
    """
    registry = ToolRegistry()
    N_TOOLS = 50
    N_SNAPSHOTS = 100

    writer_done = threading.Event()
    start_barrier = threading.Barrier(2)
    snapshot_observations: list[list[str]] = []
    write_errors: list[Exception] = []
    read_errors: list[Exception] = []

    def writer() -> None:
        start_barrier.wait()
        try:
            for i in range(N_TOOLS):
                @registry.register(name=f"w_{i:03d}")
                def tool() -> str:
                    return "x"
        except Exception as exc:
            write_errors.append(exc)
        finally:
            writer_done.set()

    def reader() -> None:
        start_barrier.wait()
        try:
            for _ in range(N_SNAPSHOTS):
                snap = registry.snapshot()
                manifest = snap.manifest()
                names = [t["name"] for t in manifest["tools"]]
                # Self-consistency: no duplicates within a snapshot.
                assert len(set(names)) == len(names)
                # Every snapshot row has a complete schema dict.
                for t in manifest["tools"]:
                    assert isinstance(t["parameters"], dict)
                    assert "type" in t["parameters"]
                snapshot_observations.append(names)
                if writer_done.is_set() and len(snapshot_observations) > 5:
                    break
        except Exception as exc:
            read_errors.append(exc)

    threads = [
        threading.Thread(target=writer, name="writer"),
        threading.Thread(target=reader, name="reader"),
    ]
    _run_concurrent(threads)

    assert not write_errors, f"Writer raised: {write_errors}"
    assert not read_errors, f"Reader raised: {read_errors}"
    # At least some snapshots happened.
    assert snapshot_observations
    # Observed counts should be non-decreasing (a later snapshot sees at least
    # as many tools as an earlier one, since the writer is monotonically adding).
    counts = [len(obs) for obs in snapshot_observations]
    for prev, curr in zip(counts, counts[1:], strict=False):
        assert curr >= prev, f"Snapshot counts decreased: {prev} -> {curr}"
    # Final manifest has all N_TOOLS.
    final = sorted(t["name"] for t in registry.manifest()["tools"])
    assert final == sorted(f"w_{i:03d}" for i in range(N_TOOLS))


@pytest.mark.timeout(60)
def test_manifest_is_consistent_during_concurrent_register() -> None:
    """manifest() locks + snapshots before rendering: every manifest is internally consistent.

    Reader-thread assertions are collected via ``read_errors`` and re-raised in
    the main thread after join; bare ``assert`` inside the reader would otherwise
    be swallowed by Python's default thread exception handler (codex phase-1
    review fix #5).
    """
    registry = ToolRegistry()
    N_TOOLS = 30
    start_barrier = threading.Barrier(2)
    writer_done = threading.Event()
    manifests_seen: list[dict] = []
    write_errors: list[Exception] = []
    read_errors: list[Exception] = []

    def writer() -> None:
        try:
            start_barrier.wait()
            for i in range(N_TOOLS):
                @registry.register(name=f"m_{i:03d}")
                def tool() -> str:
                    return "x"
        except Exception as exc:  # noqa: BLE001
            write_errors.append(exc)
        finally:
            writer_done.set()

    def reader() -> None:
        try:
            start_barrier.wait()
            while not writer_done.is_set() or len(manifests_seen) < 5:
                m = registry.manifest()
                # Internal consistency:
                assert m["protocolVersion"] == MANIFEST_PROTOCOL_VERSION
                names = [t["name"] for t in m["tools"]]
                assert len(set(names)) == len(names), f"Duplicate names in manifest: {names}"
                for t in m["tools"]:
                    assert isinstance(t["parameters"], dict)
                manifests_seen.append(m)
                if len(manifests_seen) > 200:
                    break
        except Exception as exc:  # noqa: BLE001
            read_errors.append(exc)

    threads = [
        threading.Thread(target=writer, name="manifest-writer"),
        threading.Thread(target=reader, name="manifest-reader"),
    ]
    _run_concurrent(threads)
    assert not write_errors, f"Writer raised: {write_errors}"
    assert not read_errors, f"Reader raised: {read_errors}"
    assert manifests_seen
