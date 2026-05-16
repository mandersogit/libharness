---
status: In progress
created: '2026-05-16'
---

# Phase 2 review synthesis

Three independent adversarial reviewers (2 codex gpt-5.5 xhigh, 1 Opus subagent) reviewed the phase-2 server.py rewrite. All three say "not ready as-is." This doc consolidates findings and tracks fixes before commit.

## Reviewer roster

| ID      | Scope                      | Verdict                        |
| ------- | -------------------------- | ------------------------------ |
| codex-A | generalist                 | not ready (4 HIGH, 4 MODERATE) |
| codex-B | concurrency focus          | not ready (4 HIGH, 4 MODERATE) |
| Opus    | full breadth + reproducers | not ready (2 HIGH, 4 MODERATE) |

Inputs: `/tmp/phase2-review-codex-A.md`, `/tmp/phase2-review-codex-B.md`, `/tmp/phase2-review-opus.md`.

## Tier-1 — must fix before commit (3+ reviewers OR 2+ HIGH with concrete repro)

### F1. `asyncio.CancelledError` escapes the handler

**Reviewers:** codex-A HIGH, codex-B HIGH.

**Bug.** `_handle_execute` wraps the `asyncio.run(collect_tool_result(...))` call in `except Exception`. `asyncio.CancelledError` inherits from `BaseException` (not `Exception`) since Python 3.8. A generator or async-gen tool body that observes `ctx.cancelled` raises `CancelledError` inside `collect_tool_result`. The exception bypasses `_handle_execute`'s catch and propagates up through `socketserver`, leaving the client with EOF + no response.

**Fix.** Catch `asyncio.CancelledError` explicitly before `except Exception`. Map to the same "tool execution cancelled" response path with `protocolViolation` flag preserved. Do not catch `BaseException` blindly.

### F2. `JsonlDecodeError` uncaught in `handle()`

**Reviewers:** codex-A HIGH, codex-B MODERATE, Opus HIGH (all reproduced).

**Bug.** `_read_request_frame` calls `decoder.feed(chunk)`. Invalid JSON raises `JsonlDecodeError` (subclass of `ValueError`). `handle()`'s `try/except` catches only `TimeoutError | ToolError | OSError` — `JsonlDecodeError` escapes, gets caught by `process_request_thread`'s `except Exception`, prints traceback to stderr. Client receives `b""` with no structured error.

**Fix.** Catch `(JsonlDecodeError, ValueError)` either in `_read_request_frame` (re-raise as `ToolError`) or in `handle()`. Write structured error frame. Add regression test.

### F3. Partial server start hangs `close()`

**Reviewers:** codex-A HIGH, codex-B HIGH, Opus MODERATE.

**Bug.** `_start_sync()` publishes `self._http_server` before the serve thread is running. Two windows:

- `Thread.start()` for `pi-bridge-server` raises → `_http_server` remains set → later `close()` calls `srv.shutdown()` which blocks forever (requires serve loop to be running).
- Race between `Thread.start()` returning and `serve_forever()` actually entering its main loop — `close()` in this window also hangs on `__is_shut_down.wait()`.

**Fix.** Add `_serve_entered: threading.Event` set inside the serve-thread target wrapper. `_close_sync()` waits on it (bounded) before calling `shutdown()`; if never set, skip `shutdown()` and call `server_close()` directly. On `_start_sync` failure (Thread construction or start failure), call `server_close()` directly and clear `_http_server`.

### F4. Out-of-band protocol-violation race

**Reviewers:** Opus HIGH (reproduced 3/20 iterations).

**Bug.** Sidecar's `recv(1)` sets `protocol_violation` asynchronously. Handler reads at response-write time. If the violation byte arrives between tool completion and watcher schedule, the handler writes the response with `protocolViolation` absent (but the warning still logs a moment later).

**Fix.** Before writing the response, do a non-blocking `select.select([self.request], [], [], 0)` to drain any byte already on the wire. Update `protocol_violation` synchronously. Add regression test that sends the byte without any synthetic delay across N iterations.

### F5. Close doesn't close active handler sockets

**Reviewers:** codex-B HIGH, codex-A MODERATE (close-state hygiene).

**Bug.** `_close_sync()` joins handler threads but doesn't shut down their accepted sockets. A handler blocked in initial `recv()` waits up to `timeout_ms` (default 120 s) before its thread exits. Cooperative tools waiting for `ctx.cancelled` keep running because the sidecar `recv(1)` isn't interrupted.

**Fix.** Track active accepted sockets (`_active_sockets: set[socket.socket]`) alongside `_active_handler_threads`. During close: snapshot + `shutdown(SHUT_RDWR)` each socket before joining. Wakes initial-read, sidecar-read, and most blocked writes; gives cooperative tools the cancellation signal.

### F6. Handler joins are bounded per-thread not per-close

**Reviewers:** codex-B HIGH.

**Bug.** `_close_sync()` joins each handler sequentially with `timeout=2.0`. With `max_handlers=256`, worst-case close blocks for 512 s.

**Fix.** Use a single absolute deadline across all handler joins. After socket shutdown (per F5), most handlers exit quickly.

### F7. `threading.Thread(...)` constructor failure leaks a slot

**Reviewers:** Opus MODERATE (reproduced).

**Bug.** `_handler_slots.acquire(blocking=False)` succeeds. Then `threading.Thread(target=..., ...)` is called. If construction raises (`MemoryError` etc.), the BaseException handler around `t.start()` doesn't run — the exception is in the constructor, not the start. Slot leaks. After 256 such failures, server is dead.

**Fix.** Single outer try covering slot acquire + Thread construction + start. Explicit "released" flag to prevent double-release if exception happens after start succeeded.

### F8. `params` accepts falsey non-objects

**Reviewers:** codex-A MODERATE, codex-B MODERATE.

**Bug.** `params = request.get("params") or {}` converts `[]`, `""`, `0`, `False` to `{}`. Malformed clients can call zero-arg tools with `"params": []`.

**Fix.** `params = request.get("params", {})`. Then `if not isinstance(params, dict): raise ToolError(...)`.

### F9. Sync tool bodies have no event loop

**Reviewers:** codex-A MODERATE, codex-B MODERATE.

**Bug.** `registered.call(params, ctx)` is invoked outside `asyncio.run()`. An async-def tool body works (its coroutine is awaited inside `collect_tool_result`); a plain sync tool that calls `asyncio.get_running_loop()` now fails (no loop). Pre-rewrite the async server had a loop available for sync bodies too.

**Fix.** Move `registered.call(params, ctx)` inside the coroutine passed to `asyncio.run`. Or document the compatibility break. Author chooses to move it — preserves behavior during phase 2-3 transition.

### F10. `_close_sync` self-join hazard

**Reviewers:** Opus MINOR (flagged as same hazard as the v4-synthesis harness finding).

**Bug.** If a tool body calls `server._close_sync()` from inside a handler, the snapshot includes the current thread; `t.join(timeout=2.0)` on `current_thread()` raises `RuntimeError`.

**Fix.** Skip `threading.current_thread()` in the join loop.

### F11. `_close_sync` is not safe under concurrent invocation

**Reviewers:** Opus MODERATE.

**Bug.** Two threads calling `_close_sync()` race on `srv.server_close()` (second can raise `OSError`).

**Fix.** Add `_close_lock: threading.Lock`. Inside, check `_http_server is None` to short-circuit; only the first caller does the work.

## Tier-2 — fix but lower priority

### F12. `bridge_write_timeout` only protects "fully blocked at start"

**Reviewers:** codex-A HIGH, codex-B HIGH, Opus MODERATE (acknowledged).

**Bug.** `select.select` checks write-readiness once; `sendall` can still block after a partial write. Final response writes have no timeout at all.

**Decision.** Keep the current best-effort behavior (v4-synthesis explicitly recommended this approach). **Update the docstring to be honest** about what it bounds (and doesn't). Add `bridge_write_timeout` to `PiLaunchConfig` so harness users can configure it (per F13 below). Phase 4+ may revisit with `socket.dup()`-based deadline if needed.

### F13. `bridge_write_timeout` not reachable through `PiPythonHarness`

**Reviewers:** codex-A MODERATE.

**Bug.** `PythonToolServer` accepts the kwarg but `PiPythonHarness` constructs it with no way to pass through.

**Fix.** Add `bridge_write_timeout: float | None = None` to `PiPythonHarness.__init__`; pass through to `PythonToolServer`.

### F14. `PiRpcProcessError` misleading for bridge write timeouts

**Reviewers:** Opus MODERATE.

**Decision.** Phase 3 owns the exception hierarchy. Defer renaming; add an inline comment noting the temporary use. Phase 3 introduces `PiRpcCommandError` / clearer names.

### F15. Per-recv slowloris vulnerability

**Reviewers:** Opus MODERATE.

**Decision.** v4 plan explicitly accepts this ("T1.3 floor"). **Document the slot-holding hazard in the `PythonToolServer` docstring.** Phase 4+ can add a total-deadline option if operationally needed.

### F16. Manifest requests ignore in-band extra bytes

**Reviewers:** codex-B MINOR.

**Fix.** Treat non-empty `in_band_violation_bytes` consistently for manifest too — flag `protocolViolation` on the manifest response. Small change.

## Tier-3 — test improvements

### F17. `test_thread_start_failure_releases_slot_active_entry_and_request` doesn't verify the start branch fired

**Reviewers:** codex-B MINOR, Opus MINOR.

**Fix.** Add an event set in the patched `Thread.start`; assert it fired. Also assert request socket is shut down (`recv(1) == b""` or OSError on peer).

### F18. Out-of-band-race regression test

**Per F4.** Add `test_out_of_band_violation_no_synthetic_delay` that loops 50 iterations sending the violation byte immediately, asserts the flag is set in every response.

### F19. Test for `threading.Thread.__init__` failure (not just `start`)

**Per F7.** Add `test_thread_constructor_failure_releases_slot`.

### F20. Test for `_close_sync` before serve thread entered

**Per F3.** Add `test_close_immediately_after_start_does_not_hang` loop (50 iterations or barrier-driven).

### F21. Test for `JsonlDecodeError`

**Per F2.** `test_malformed_json_request_returns_structured_error_not_silent_close`.

### F22. Test for self-join hazard

**Per F10.** Tool body calls `server._close_sync()` from inside; assert no `RuntimeError`.

## Out of scope (defer to phase 3 or later)

- Per-FRAME deadline for slow attackers (F15) — phase 3+ if operationally needed.
- `socket.dup()`-based bridge_write_timeout (F12) — phase 3+ if `bridge_write_timeout` becomes load-bearing.
- Exception-hierarchy reshuffle (F14) — phase 3 owns this.
- Sleeps in concurrency tests (F-MINOR-codex) — partial fix: convert the worst offenders; full pass deferred.
- `subprocess.run` test polish (PYTHONUNBUFFERED, absolute paths) — minor; defer.
- Reading multiple bytes in the sidecar's violation log — cosmetic; defer.

## Plan

1. Apply F1–F11 + F13 + F16 in `src/libharness/pi/server.py` and (for F13) `src/libharness/pi/harness.py`.
1. Add regression tests F17–F22 in `tests/pi/test_server.py`.
1. Tighten docstring for `bridge_write_timeout` (F12) and `PythonToolServer` (F15).
1. `make all` on both venvs; re-flake-check.
1. Commit via commit-plans.
