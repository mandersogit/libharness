---
status: "In progress"
created: "2026-05-16"
---

# Phase 3 review synthesis

Three independent adversarial reviewers (2 codex gpt-5.5 xhigh, 1 Opus subagent) reviewed the phase-3 rpc.py rewrite. All three say "not ready as-is." This doc consolidates and tracks fixes before commit.

| ID      | Scope                     | Verdict                            |
| ------- | ------------------------- | ---------------------------------- |
| codex-A | generalist                | not ready (2 CRITICAL, 1 HIGH)     |
| codex-B | concurrency focus         | not ready (2 CRITICAL, 1 HIGH)     |
| Opus    | full breadth + reproducers | not ready (1 CRITICAL, 7 HIGH)    |

Inputs: `/tmp/phase3-review-codex-A.md`, `/tmp/phase3-review-codex-B.md`, `/tmp/phase3-review-opus.md`.

## Tier-1 — must fix before commit

### F1. Dict-merge response key collision (Opus C1, codex-A H1, codex-B M3)

**Reviewers:** Opus CRITICAL (reproduced), codex-A HIGH, codex-B MODERATE.

**Bug.** `_handle_extension_ui_request` builds the response frame as `{"type": "extension_ui_response", "id": req_id, **response}`. Python dict-literal: later `**unpack` OVERRIDES earlier keys. A UI handler returning `{"id": "evil", "type": "fake"}` silently overrides the framework's keys → wrong response id, wrong response type. Silent protocol corruption.

**Fix.** Spread `response` first: `{**response, "type": "extension_ui_response", "id": req_id}`. Trivial.

### F2. UI gate set AFTER event dispatch (all three reviewers)

**Reviewers:** codex-A CRITICAL, codex-B CRITICAL, Opus M6 (race-narrow but real).

**Bug.** `_handle_extension_ui_request` calls `_enqueue_event` + `_dispatch_event` BEFORE setting `_ui_response_pending`. Event handlers can block. During the window, caller-thread `_send_sync()` sees the gate as clear and writes a normal request → wire violates the half-duplex contract. Pi may wedge.

**Fix.** For response-bearing methods, set `_ui_response_pending` IMMEDIATELY after validating `req_id`, BEFORE event dispatch. Notification-only methods don't need the gate.

### F3. `_close_sync` deadlock behind blocked stdin writer (codex-A C2, codex-B C2, Opus H1/H7)

**Reviewers:** codex-A CRITICAL, codex-B CRITICAL, Opus HIGH (3 related findings).

**Bug.** Three-way deadlock under blocked stdin:
- Caller A holds `_send_lock`, blocked in `proc.stdin.write` (pi not draining).
- Reader-owned UI response write blocks on `_send_lock`.
- `_close_sync` blocks on `_send_lock` to close stdin.
- Pi waits for UI response (which is blocked).

Close never reaches SIGTERM/kill. Forever-deadlock unless externally SIGKILL'd.

**Fix.** `_close_sync` escalates terminate/kill BEFORE acquiring `_send_lock`:
1. Set `_closing`, fatal state (no lock needed).
2. Try `_send_lock.acquire(timeout=2.0)`. If fails:
3. Terminate/kill the subprocess FIRST (this unblocks the writer with `BrokenPipeError`).
4. Retry `_send_lock.acquire(timeout=1.0)`. The writer should have released by now.
5. If still fails: log error, skip stdin.close (process is gone), continue cleanup.

This is the v4 plan's original design that the implementation deviated from.

### F4. `_in_dispatch.value` leaks True in `_handle_extension_ui_request` (Opus H4)

**Reviewers:** Opus HIGH.

**Bug.** `_handle_extension_ui_request` sets `self._in_dispatch.value = True` (line 805) but has NO `finally` to reset. Compare `_dispatch_event` which DOES use try/finally. The leftover True persists; any future reader-thread code that calls `send()` outside a handler would spuriously raise `ReentrantRPCError`.

**Fix.** Wrap the UI handler invocation in try/finally that resets `_in_dispatch.value = False`.

### F5. UI gate retry timeout uses config not caller (Opus H2)

**Reviewers:** Opus HIGH.

**Bug.** `_write_stdin_normal`'s `_ui_response_condition.wait_for(..., timeout=max(0.1, self.config.request_timeout))` uses `request_timeout` (default 30s), NOT the caller's per-call `timeout` argument. A `send(..., timeout=2.0)` could wait 30s before raising. Caller deadline silently ignored.

**Fix.** Thread the caller's effective deadline through `_send_sync` → `_write_stdin_normal`. Compute `deadline = monotonic() + timeout`; the wait uses `min(deadline - monotonic(), config.request_timeout)`.

### F6. Restart after close silently broken (codex-A H2, codex-B M2, Opus M8)

**Reviewers:** codex-A MODERATE, codex-B MODERATE, Opus MODERATE.

**Bug.** `_close_sync` sets `_fatal_error` to `PiRpcProcessError("Pi RPC client closed")`. `_start_sync` clears `_closing` and `_close_complete` but does NOT clear `_fatal_error` or `_stdin_closed`. A restart launches a new subprocess successfully; first send fails with fatal-state error.

**Fix.** Make single-use explicit. `_start_sync` raises `RuntimeError("PiRpcClient is single-use; create a new instance")` if `_close_complete` is set. This matches the v4 plan's "restart unsupported" semantics. Direct users of `PiRpcClient` get a clear error.

### F7. Async shims block the asyncio event loop (codex-A MODERATE, codex-B MODERATE, Opus M2)

**Reviewers:** all three MODERATE.

**Bug.** Every `async def` shim directly calls the sync core. No `await` point. `asyncio.wait_for(client.send(...), timeout=2)` can't interrupt the blocking `Future.result(timeout)`. Tests using `asyncio.wait_for` look flaky.

**Fix.** Use `asyncio.to_thread(self._send_sync, ...)` in the async shims. The asyncio side observes proper async semantics; the underlying sync core still blocks in its own thread. Phase 4 drops the shims anyway, but this fixes phase-3-era live tests.

## Tier-2 — fix if time permits

### F8. Partial startup failure after Popen not cleaned up (codex-B MODERATE)

**Bug.** If the 200 ms startup probe finds the subprocess exited, `_start_sync` raises but doesn't close pipes, join reader threads, or clear `self.process`.

**Fix.** On startup failure, run a minimal cleanup: close stdin, wait/reap process, set `self.process = None`.

### F9. Reader-thread caches `self.process.stdout` (Opus H5)

**Bug.** `self.process` can become None during close-from-callback. Next loop iteration: `AttributeError`. Caught by outer `except` and swallowed but noisy.

**Fix.** Cache `stdout = self.process.stdout` after entry assertion; use cached reference in the loop.

### F10. `_close_sync` second-caller wait can silently return on timeout (Opus H6)

**Bug.** `_close_complete.wait(timeout=10.0)` returns False silently if first caller's close hangs longer than 10s. Caller may believe close succeeded.

**Fix.** Either unbounded wait (with active unblock fixing F3) or raise on timeout.

## Tier-3 — defer with documentation

### F11. Hook watchdog from phase-3 contract is missing (codex-A MINOR, codex-B MINOR)

**Decision.** The plan called for `threading.Timer` watchdog logging when event/UI dispatch exceeds `hook_warn_threshold_ms`. **Defer to phase 4** — adds a new config knob; standalone diagnostic; no correctness risk.

### F12. Event handler non-None return warning missing (codex-A MINOR, codex-B MINOR, Opus M11)

**Decision.** Add a one-liner warning to `_dispatch_event`. Cheap.

### F13. Stderr reader silent exit (Opus M5)

**Decision.** Add debug log. Cheap.

### F14. `_default_ui_response` redundant if/return (Opus m5)

**Decision.** Cosmetic; defer.

### F15. Magic 0.2s startup wait (Opus m9)

**Decision.** Counter-intuitive but documented in v4 plan as "brief startup wait so subprocess crashes during launch surface as PiRpcProcessError." Leave as is.

### F16. `_request_counter` not atomic (Opus m3)

**Decision.** Uuid suffix ensures uniqueness; counter is debug-only. Acceptable.

### F17. UI gate retry test coverage thin (Opus H3, codex-A MODERATE, codex-B MINOR)

**Decision.** Add the missing T1/T2/T3 tests as part of this fix pass.

## Out of scope (defer to phase 4)

- Drop async shims entirely.
- Drop per-execute `asyncio.run` in server.py.
- Async-tool rejection at registration.
- Helper-thread reentrancy detection.

## Plan

1. Apply F1–F7 to `src/libharness/pi/rpc.py`.
1. Apply F12, F13 (cheap diagnostic improvements).
1. Add F17's missing UI gate retry tests.
1. `make all` on both venvs; 3-run flake check.
1. Commit via commit-plans.
