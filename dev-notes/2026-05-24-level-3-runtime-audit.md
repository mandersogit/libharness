---
status: Partial (Option A landed 2026-05-25; HarnessRuntime audit remains)
created: '2026-05-24'
---

# Level 3 runtime audit — clarify what's process-shared vs harness-owned; close the threaded=False gap

Follow-up to the F8 fix shipped 2026-05-24 (Level 2). That fix folded sync hooks into the owner thread for `threaded=True`, eliminated `hook_executor` from `HarnessRuntime`, and brought the production code path to the 4-thread model the user asked for (MainThread / PiAsyncioLoop / PiAgentHarness-`<id>` / pi-tool\_\*). Two threads of work still need to land before the threading model is fully settled:

1. ~~**Close the `threaded=False` design debt** (HIGH priority — surfaced during F8).~~ **DONE 2026-05-25 via Option A.** `_call` now pumps the harness command queue in `threaded=False` mode; `pump_until(coro_or_future_or_event)` lets tests that bypass the harness public API pump explicitly. Single dispatch code path in `_dispatch_sync_hook`. See "Outcome" section below.
1. **Audit + clarify `HarnessRuntime` scope** (STILL OPEN). Once `hook_executor` is gone, `HarnessRuntime` is "the asyncio loop thread + the shared tool pool." Worth examining whether it should remain user-facing, whether more of its surface should be private, and how it cooperates with per-harness state. See "Decision needed: HarnessRuntime scope clarification" section below.

## In scope

- The `threaded=False` design debt and the two solution shapes (pump-aware MainThread vs deprecate-threaded=False).
- `HarnessRuntime`'s post-Level-2 responsibilities: loop thread, tool executor, lifecycle.
- Test patterns that bypass the harness public API and the implications for the design.

## Out of scope

- Anything tied to specific Tier-2 ergonomic-pass items not on the threading axis (F11, F12, etc. — handled separately).
- The runtime-as-fully-internal question (i.e. removing `HarnessRuntime` from public API) — that's downstream of the audit, not the audit itself.
- F.1 / F.2 / F.3 roadmap bridges (separate workstream).

## Decision needed: `threaded=False` resolution

### Option A: Make MainThread the queue consumer (centralized pumping)

When `harness.prompt_and_wait()` / `harness.start()` / etc. are called on MainThread under `threaded=False`, `_call()` itself pumps the queue. **All** public-API methods get the fix for free — `_call` is the single chokepoint they go through:

```python
def _call(self, operation):
    if self._closed:
        raise RuntimeError(...)
    if self.threaded:
        return self.submit(operation).result()
    # threaded=False: MainThread IS the owner thread; pump the queue.
    if self._core is None:
        self._core = _PiAgentHarnessCore(**self._core_kwargs)
    main_future = concurrent.futures.Future()
    self._commands.put(_OpEnvelope(operation, main_future))
    while not main_future.done():
        item = self._commands.get()
        self._process_item(item)   # _HookCall, _Continuation, _OpEnvelope
    return main_future.result()
```

For white-box tests that bypass the harness API (e.g. `runtime.run_async(agent._async_on_event(...))`), add a single helper on the harness:

```python
def pump_until(self, coro_or_future):
    """Submit `coro` to the loop and pump the harness queue until it
    completes. For threaded=False tests that inject events directly,
    bypassing the harness public API."""
    if isinstance(coro_or_future, concurrent.futures.Future):
        loop_future = coro_or_future
    else:
        loop_future = self.runtime.submit_async(coro_or_future)
    while not loop_future.done():
        item = self._commands.get()
        self._process_item(item)
    return loop_future.result()
```

Affected tests change *one line each*: `runtime.run_async(agent._async_on_event(...))` → `agent.pump_until(agent._async_on_event(...))`. Mechanical.

For raw bridge-socket tests (`_event_request`), refactor the test helper *once* to move socket I/O onto a tiny worker thread and pump the harness queue while waiting for the response future. Tests using it don't change at all (they just gain a harness argument).

**Pros:**

- Restores the "one code path" invariant for sync hook dispatch.
- Matches the user's stated principle ("MainThread should have the same responsibilities as the harness runtime threads would have").
- `_dispatch_sync_hook` loses its `if self.threaded:` branch.
- The centralized pump means changes are concentrated in `_call`, the new `pump_until` helper, and `_event_request` — not scattered across the test suite.

**Cons:**

- Adds a queue-consumption loop to `_call` for `threaded=False`. Conceptually heavier than the current "block on `Future.result()`" but the cost is invisible to callers.
- Bridge-socket tests still need a one-time refactor of the `_event_request` helper (socket I/O on a worker, pump on MainThread). ~30 LOC, contained to the test-helpers module.
- Per-test call-site updates for white-box dispatch tests (~5–10 tests across `test_agent_class.py` / `test_decision_hooks.py`), but each is a one-line search-and-replace.

**Estimated effort:** ~15 LOC in `_call`; ~10 LOC for `pump_until`; drop ~5 LOC from `_dispatch_sync_hook` (its `if self.threaded:` branch); ~30 LOC in `_event_request`; ~10 lines of call-site updates across the affected tests. **~70 LOC total.**

### Option B: Deprecate `threaded=False` entirely

Force all callers — production and tests — to use `threaded=True`. Removes the second dispatch path; everything goes through the owner thread queue.

**Pros:**

- Cleanest end state. No `threaded` flag, no branches, single threading model.
- Tests that need direct access to the agent's internals can construct an `Agent` with `threaded=True` (default) and use its public API; this is more realistic anyway.
- Removes the test-only "no owner thread" mode that's been creating subtle gaps.

**Cons:**

- Largest refactor of the three options. Every test that constructs `threaded=False` needs updating (~15–20 tests across the suite).
- Some tests are tight unit tests of dispatch logic; they'd need to either use `threaded=True` (and accept the owner thread overhead) or be reframed as integration tests.

**Estimated effort:** ~500–800 LOC of test restructuring; minor production-code cleanup as the `threaded` parameter is removed.

### Option C: Accept the asymmetry; document only

Keep `threaded=False` as it is. Add documentation calling out that sync hook dispatch differs between modes. No code change.

**Pros:**

- Zero code change.
- Preserves existing test patterns.

**Cons:**

- Violates the user's stated design principle.
- The asymmetry compounds with adoption: any future user who debugs why sync hooks behave differently in test vs production will hit this.
- Documentation-only fixes for design debt rarely stick over time.

**Recommendation: Option A.** Earlier draft of this doc framed A as "philosophically right but expensive" — that was wrong; the test churn collapses to ~10 lines of one-line search-and-replace once `_call` and `pump_until` centralize the pumping. Total effort is ~70 LOC, mostly in `agent.py` and the `_event_request` test helper. That's smaller than Option B's deprecation refactor (~500–800 LOC of test rewrites) AND lands at the same end state on the dispatch invariant. Option B remains the cleaner *very-long-term* shape (no `threaded` parameter at all) but isn't worth the cost differential right now. Option C is unattractive — it locks in the asymmetry we're trying to fix.

## Decision needed: `HarnessRuntime` scope clarification

Post-Level-2, `HarnessRuntime` owns:

- `loop_thread` (an `AsyncioLoopThread`) — the shared process-wide asyncio loop
- `tool_executor` (a `ThreadPoolExecutor`) — the shared process-wide tool pool

That's it. The third pool (`hook_executor`) was removed in F8. The owner thread isn't in `HarnessRuntime` — it lives on `PiAgentHarness`.

Open questions:

- **Q1.** Is `HarnessRuntime` still the right name? It was originally "the shared infrastructure" but now it's specifically "the asyncio + tool concurrency primitives." Renaming candidates: `LoopRuntime`, `AsyncRuntime`, `SharedRuntime`. Names are cheap; defer unless we hit confusion.
- **Q2.** Should `HarnessRuntime` remain a user-facing class? Today users *can* construct it explicitly for opt-in sharing (the `test_multiple_harnesses_have_distinct_owner_threads_and_shared_loop` test pattern). After the threaded=False fix, the only legitimate user-facing use case is "I want N harnesses sharing one tool pool" — which is rare and probably better served by configuration on the harness itself (`PiAgentHarness(..., tool_pool_size=4)`).
- **Q3.** `get_default_runtime()` returns a process-wide singleton. Keep, or replace with implicit-on-first-harness construction? Today it's an internal-ish factory. Keep until we have a concrete reason to change.
- **Q4.** Lifecycle: when does the default runtime get cleaned up? Today there's a `close_default_runtime()` exported but rarely called. Most test suites rely on process exit. Acceptable for now but should be revisited if we ever ship a library shape that doesn't expect process exit (e.g. notebook usage).

## Outcome — Option A landed (2026-05-25)

**Decision: Option A** — MainThread is the queue consumer in `threaded=False`; harness public methods pump via `_call`; tests bypassing the harness API use `pump_until(target)` to pump explicitly.

**What landed:**

- `agent.py:_call` — when `threaded=False`, enqueues `_OpEnvelope(operation, main_future)` and calls `_pump_until(main_future)`; MainThread becomes the queue consumer.
- `agent.py:pump_until` — public helper accepting `Awaitable` / `Coroutine` / `concurrent.futures.Future` / `threading.Event`. Returns the unwrapped value (or the event's `is_set()` for the Event case).
- `agent.py:_process_item` — extracted from `_owner_loop` so the dispatcher logic is shared between the owner thread (`threaded=True`) and MainThread pump (`threaded=False`).
- `agent.py:_WAKE` — module-level sentinel pushed by `pump_until`'s done-callback to wake the queue consumer when a future resolves without enqueueing anything. Recognized as a no-op by any consumer so stale wakes from prior pumps don't break anything.
- `agent_class.py:_dispatch_sync_hook` — `if self.threaded:` branch removed. Single code path; always enqueues `_HookCall`.
- `tests/pi/test_decision_hooks.py:_event_request` — refactored to do socket I/O on a worker thread and pump on MainThread (via `agent.pump_until(response_future)`). Tests using it gained a `harness` argument; no other change.
- `tests/pi/test_channel_separation.py` — its inline raw-socket test got the same worker-thread+pump treatment.
- White-box dispatch sites (~10 lines): mechanical `runtime.run_async(agent._async_on_event(...))` → `agent.pump_until(agent._async_on_event(...))`.
- `test_cancellation_mid_decision_when_bridge_connection_closes`: now uses `agent.pump_until(exited, timeout=2.0)`.
- `test_bridge_connection_drop_mid_decision_does_not_deadlock`: switched to `threaded=True` (a busy-wait sync hook can't run on MainThread while MainThread also needs to close the socket — see Limitation below).

**Total code change:** ~75 LOC of production + test-helper change; ~12 lines of one-line test-site swaps. Right in the 70-LOC ballpark predicted in the revised Option A estimate.

**Limitation — blocking sync hooks under `threaded=False`:** if a sync hook blocks (busy-wait, `time.sleep`, blocking I/O), MainThread is stuck inside that hook and can't do test-driver work concurrently. Tests that need cancellation-during-blocking-hook must use `threaded=True`. Documented in `docs/AGENT_HOOKS.md`. This is a fundamental property of "MainThread is the runtime thread" in single-threaded mode; not something Option A could fix without a separate worker thread for hooks (which would defeat the unified-dispatch goal).

**Result on F8 dispatch invariant:** single code path holds in production AND in test mode. Consumer varies by *who* (MainThread vs dedicated owner thread); the dispatch logic itself is unified.

## Open — HarnessRuntime scope (still pending)

The threaded=False question is closed. The remaining Level 3 work is the `HarnessRuntime` audit (Q1–Q4 below). Less urgent than the threaded=False fix was — HarnessRuntime's current shape works; the audit is for clarification before external-user-facing API freezes.

## Evidence trail

- `dev-notes/2026-05-17-v8-port-deferred-items.md` § B.F8 (Level 2 detail) and § C.3 (the new threaded=False entry).
- `dev-notes/SESSION-STATE.md` § Current state and § Pending tasks (HIGH-priority threaded=False bullet).
- `[[project-threaded-false-design-debt]]` agent memory entry.
- `src/libharness/pi/agent.py` `_dispatch_sync_hook` branch + Level 2 commit (the code shape that motivated this audit).
- `tests/pi/test_decision_hooks.py` and `tests/pi/test_agent_class.py` — the white-box dispatch tests that constrain Option A.
