---
status: Ready for implementation
created: '2026-05-15'
---

# asyncio → threads rewrite plan (revised, v4)

Implementation plan for rewriting `src/libharness/pi/` from asyncio to threads, per the concurrency-model decision recorded 2026-05-15 (`dev-notes/2026-05-15-concurrency-model-discussion.md` § Resolution). The decision is locked; this document is the implementation contract.

**Status: Ready for implementation**, contingent on author sign-off on the v4 design choices that closed v3's five remaining Tier-1 races. The Agent-class and event-bridge co-design remains separate; this plan implements the minimum safe dispatch / correlation / lifecycle contract for the current RPC and bridge surfaces.

## Revision history

- **v4 (this revision, 2026-05-16)** — third post-review revision. Six adversarial reviewers reviewed v3 (`dev-notes/2026-05-16-threads-rewrite-plan-v3-review-synthesis.md`); 6 of 6 said "not ready as written; v4 needed" with surgical (not rewrite-level) amendments. v4 closes the five Tier-1 GATEs and the ten Tier-2 findings:

  - **T1.1-v4 — UI response priority via write-admission recheck.** v3's reservation pattern had a post-gate race: a caller that passed the gate while clear could stall pre-write, then the reader sets the flag, then the caller wins `_send_lock` and writes a normal request before the UI response. v4 fix: `send()` re-checks `_ui_response_pending` *after* acquiring `_send_lock`; if set, releases the lock and waits on the condition again. The reader path is unchanged (it still owns the UI response write); the change is in the caller path.
  - **T1.2-v4 — split `_settle_future` into two helpers.** v3 had `_settle_future(req_id, ...)` that popped from `_pending`, but `_fail_pending` snapshot-cleared `_pending` and then called `_settle_future` — every post-clear call was a silent no-op. v4 fix: split into `_pop_pending(req_id) → Future | None` (response path, pops under lock) and `_complete_future(fut, value=None, exc=None)` (already-owned-future path, tolerates `InvalidStateError`). Single helper, two call patterns, no ambiguity.
  - **T1.3-v4 — lifecycle close-during-start via abort + `_start_complete`.** v3 released `_lifecycle_lock` during multi-step start; `close()` could transition starting→closing and run cleanup while start was still creating resources. v4 fix: `_start_complete: threading.Event` set at end of `start()` (success or failure). `close()` during `"starting"`: sets `_lifecycle_state = "aborting"`, releases lock, waits on `_start_complete` (with timeout), then proceeds with cleanup. `start()` checks `_lifecycle_state == "aborting"` after each cut point and aborts early.
  - **T1.4-v4 — `close()` causes `send()` to fail atomically.** v3's `send()` checked `_fatal_error` under `_pending_lock` but not `_closing`. v4 fix: `close()` sets `_fatal_error = PiRpcProcessError("client closed")` *under `_pending_lock`* during its cleanup sequence, before joining the reader. Reuses the existing fatal-check path in `send()`; no second flag.
  - **T1.5-v4 — `_stdin_closed` invariant preserved in `close()` fallback.** v3's fallback set `_stdin_closed = True` and called `proc.stdin.close()` even when `_send_lock.acquire(timeout=1)` failed — violating its own invariant. v4 fix: on lock-acquire timeout, terminate the subprocess first (SIGTERM), wait briefly for the blocked writer to release the lock with `BrokenPipeError`, then acquire and proceed normally. Invariant is preserved unconditionally.

  Tier-2 amendments:

  - **`_write` raises on normal path** (was: swallowed `OSError`). Caller-thread `send()` now gets immediate failure on broken stdin, not a `request_timeout` wait. Best-effort close/UI cleanup paths still swallow.
  - **`close()` `try/finally` guarantees state finalization.** `_lifecycle_state = "closed"` and `_close_complete.set()` always run, regardless of cleanup exceptions. Cleanup errors are logged but never raised from `close()`.
  - **`close()` postcondition pinned.** After `close()` returns: no event/UI callbacks fire. Reader dispatch path early-returns if `_lifecycle_state in ("closing", "closed")`.
  - **Explicit `__all__` in `__init__.py`.** Every existing export preserved; new exception hierarchy added. No undocumented breaks.
  - **Migration table now has 14 rows** (was 12). Added: async tool body, `PythonToolServer` lifecycle methods.
  - **`ToolRegistry` is internally locked.** `register()`, `snapshot()`, `get()`, `manifest()` all acquire `_registry_lock`. Snapshot is non-destructive (returns immutable copy).
  - **`max_handlers` design unified on `process_request` override.** Bounds thread creation (not just execution). v3's contradictory text removed. Default 256.
  - **Helper-thread bypass test uses subprocess + `subprocess.run(timeout=N)`** asserting `TimeoutExpired`. Pytest-timeout is not the right primitive for "assert this hangs."
  - **`send()` rollback split** — write-failure rollback (`_pop_pending` + `_complete_future` with `PiRpcProcessError`) is distinct from wait-handling (`fut.result(timeout)` raising `TimeoutError`).

  Tier-3 amendments:

  - **Runtime guard rejects `inspect.isgenerator(result)` for event/UI handlers** too (was: tool path only). Event/UI handler returns must be `None`; non-`None` logs a warning.
  - **Dual-delivery ordering pinned: queue.put first, then handlers.** Documented; tested.
  - **Async iterables in guard.** Runtime guard now also rejects `isinstance(result, collections.abc.AsyncIterable)` for custom `__aiter__` bypasses.
  - **`ctx.update` gains optional `bridge_write_timeout` config.** Default `None` (current behavior, unbounded). Documented as a DoS-mitigation knob.
  - **Async-test guard unified.** Drop the Makefile grep; rely on `tests/pi/test_async_contract_guard.py` AST check (runs in normal suite).
  - **Event queue timeout wrapped.** `EventQueueEmpty(PiRpcError)` replaces `queue.Empty` at the public API boundary.
  - **Bisect-baseline guard checks both count and signature.** Makefile target computes `sha256(sorted nodeids)` and compares to last row.
  - **Bisect-baseline append-on-commit enforced via CI/precommit.** Phase commits assert the file was modified.
  - **Runtime-meta CI enforcement.** New Makefile target `make threads-runtime-meta-check` compares `.pytest-runtime-meta/{3.11,3.14t}.json` outputs; asserts they disagree on `_is_gil_enabled()`.
  - **Bridge handler-thread tracking.** Server subclass maintains `_active_handler_threads: set[Thread]`; `close()` joins each with timeout.
  - **Slowloris test asserts thread count** (not just rejection count) — consistent with `process_request` gate.

  Tier-4 polish:

  - Server-owned counters replace `threading.enumerate()` for thread-leak assertions (deterministic, not flaky).
  - `T1.3` timeout-floor regression test (`timeout_ms=10` still gets 1-second floor).

  **Scope unchanged from v3**: port + latent-bug fixes + contract-pinning + docs migration. Author approval covers all four.

- **v3 (2026-05-16)** — Superseded by v4. Folded ~30 items from v2 synthesis; six reviewers found five Tier-1 residual races (UI reservation post-gate, `_settle_future` ownership, lifecycle close-during-start, `close`/`_pending` atomic gap, `_stdin_closed` outside lock in fallback) and ten Tier-2 contract gaps.

- **v2 (2026-05-15, commit 257247a)** — Superseded by v3 (then v4). Folded 17 of 32 v1 matrix items; failed v2 review on dispatch contract, Future correlation, settimeout scope, half-duplex enforcement.

- **v1 (2026-05-15, commit 40ac517)** — Superseded by v2. Original plan; established 4-phase feature-branch structure and locked concurrency direction.

## Scope is broader than a port

The original plan framed this as "rewrite asyncio → threads, wire protocols unchanged." Three review rounds surfaced that several issues in the current asyncio code (latent bugs, missing contracts, undocumented assumptions) are cheapest to fix in the same rewrite window. The revised scope explicitly includes:

- **Port work.** asyncio → threads in `rpc.py`, `server.py`, `tools.py`, `harness.py`.
- **Latent-bug fixes** that exist in current code but become more dangerous under threading or 3.14t: `__exit__` rollback hole in `harness.py` (T2.7), unprotected `ToolRegistry` mutation (now via internal `_registry_lock`), unbounded `_events` queue, unprotected handler registries (via `_handlers_lock`), missing fatal-state contract.
- **Contract-pinning doc updates**: bridge half-duplex contract, `ctx.update` blocking semantics + optional timeout, restart-unsupported contract, dual-delivery ordering, single-use lifecycle, helper-thread reentrancy out-of-scope, `close()` postcondition.
- **`docs/DESIGN.md` migration** plus a 14-row "Breaking changes and migration" table; `CLAUDE.md` § Conventions stale-async cleanup.

These are not separate work tracks. They land in the same 4-commit feature branch as the port itself.

## Architecture overview

```text
PiPythonHarness (sync __enter__/__exit__)
  ├── _lifecycle_state: "new" | "starting" | "aborting" | "started" | "closing" | "closed" | "failed"
  ├── _lifecycle_lock: threading.Lock  (serializes state transitions, NOT held during I/O)
  ├── _start_complete: threading.Event  (set at end of start(), success or failure)
  ├── _close_complete: threading.Event  (set in close()'s try/finally tail)
  ├── start(): new → starting → started (success) | starting → aborting/failed → closed (failure)
  ├── close(): started/failed → closing → closed | starting → aborting → wait → closed
  ├── PythonToolServer
  │     ├── socketserver.ThreadingTCPServer subclass
  │     ├── _frozen_registry: ImmutableRegistry  (private snapshot via registry.snapshot();
  │     │                                          caller's ToolRegistry never mutated)
  │     ├── _handler_slots: threading.Semaphore(max_handlers)  (default 256)
  │     ├── process_request override acquires slot BEFORE thread spawn;
  │     │     release in process_request_thread finally
  │     ├── _active_handler_threads: set[Thread]  (server-owned tracker for orderly close)
  │     ├── BridgeHandler (one thread per accepted in-slot connection)
  │     │     ├── settimeout(max(1.0, timeout_ms/1000)) → read initial frame → settimeout(None)
  │     │     ├── 8 MiB request-byte cap via counting wrapper on recv
  │     │     ├── manifest: sync immediate response
  │     │     └── execute:
  │     │           ├── sync tool on handler thread (uses _frozen_registry)
  │     │           ├── ctx.update writes update frames via blocking sendall (optional bridge_write_timeout)
  │     │           ├── sidecar daemon thread: recv(1); b"" → cancel; non-empty → cancel + protocol_violation
  │     │           ├── sidecar wraps recv in try/except OSError: finally: ctx._cancelled.set()
  │     │           └── exactly one final response frame, best-effort
  │     └── close() joins tracked handler threads with timeout
  └── PiRpcClient
        ├── subprocess.Popen(stdin=PIPE, stdout=PIPE, stderr=PIPE, bufsize=0)
        ├── stdout reader thread (owns _handle_message, dispatch, UI handling)
        ├── stderr reader thread (accumulates stderr text)
        ├── _send_lock: threading.Lock  (serializes actual stdin writes only)
        ├── _stdin_closed: bool  (set under _send_lock by close() before proc.stdin.close())
        ├── _closing: threading.Event  (set lock-free by close() at entry)
        ├── _pending: dict[id, concurrent.futures.Future]
        ├── _pending_lock: threading.Lock
        │       — guards _pending dict ops AND _fatal_error transitions (atomically)
        │       — NEVER held across Future.result(timeout=t)
        │       — _pending_lock may be acquired before _send_lock; never after
        ├── _events: queue.Queue(maxsize=4096)  (drops oldest under _events_drop_lock; rate-limited warning log)
        ├── _events_drop_lock: threading.Lock  (guards the get_nowait+put_nowait drop-oldest sequence)
        ├── _event_handlers: list[Callable]  (guarded by _handlers_lock)
        ├── _ui_handlers: dict[str, Callable]  (guarded by _handlers_lock)
        ├── _handlers_lock: threading.Lock  (mutate + snapshot before dispatch)
        ├── _fatal_error: Exception | None  (set under _pending_lock; checked under same lock in send())
        ├── _in_dispatch: threading.local  (set true in BOTH _dispatch_event and _handle_extension_ui_request;
        │                                  send() raises ReentrantRPCError if set; same-thread only)
        ├── _ui_response_pending: threading.Event  (set when ui_request arrives, cleared after ui_response sent)
        ├── _ui_response_condition: threading.Condition  (send() waits and RE-checks after _send_lock acquire)
        ├── _settle_future helpers:
        │       _pop_pending(req_id) → Future | None  (pops under _pending_lock)
        │       _complete_future(fut, value=None, exc=None)  (set_result/set_exception, swallow InvalidStateError)
        ├── _watchdog: threading.Timer per dispatch (logs warning on hook-duration > threshold)
        └── close() sequence: _closing.set() → set fatal under _pending_lock + snapshot-clear pending →
                              complete_future on each → bounded _send_lock acquisition (with subprocess
                              termination fallback) → close stdin → join readers → wait subprocess
```

All public API methods become plain `def`. Event and UI handlers run synchronously on the stdout reader thread. **Same-thread** reentrant `client.send(...)` from inside dispatch raises `ReentrantRPCError`. Helper-thread reentrancy (handler spawns a thread that calls `client.send()` and waits) is **explicitly out-of-scope** — documented contract violation; will deadlock; not detected. Process-wide detection deferred to event-bridge co-design.

## File-by-file rewrite scope

Wire protocols are unchanged: pi RPC remains LF-terminated JSONL over the subprocess; the Python bridge remains loopback TCP JSONL with the same manifest/execute/update/response frame shapes.

LOC estimates are ±25%, not load-bearing for scope decisions.

| File                                    | Current LOC | New LOC                                                        |
| --------------------------------------- | ----------- | -------------------------------------------------------------- |
| `__init__.py`                           | 20          | ~35                                                            |
| `harness.py`                            | 136         | ~210                                                           |
| `jsonl.py`                              | 70          | 70                                                             |
| `rpc.py`                                | 415         | ~680                                                           |
| `server.py`                             | 191         | ~420                                                           |
| `shim.py`                               | 305         | unchanged                                                      |
| `tools.py`                              | 368         | ~410                                                           |
| `tests/pi/__init__.py`                  | 0           | 0                                                              |
| `tests/pi/test_jsonl.py`                | 14          | 14                                                             |
| `tests/pi/test_tools.py`                | 44          | ~200                                                           |
| `tests/pi/test_rpc_fake.py`             | 16          | ~310                                                           |
| `tests/pi/test_set_model.py`            | 46          | ~40                                                            |
| `tests/pi/test_server.py`               | 49          | ~320                                                           |
| `tests/pi/test_real_*.py`               | 38 + 77     | 38 + 77                                                        |
| `tests/pi/fake_pi_rpc.py`               | 32          | ~200                                                           |
| `tests/pi/test_runtime_meta.py`         | 0 (new)     | ~50                                                            |
| `tests/pi/test_lifecycle.py`            | 0 (new)     | ~110                                                           |
| `tests/pi/test_async_contract_guard.py` | 0 (new)     | ~80                                                            |
| `pyproject.toml`                        | 126         | ~125                                                           |
| `docs/DESIGN.md`                        | (touched)   | rewrite + new § Breaking changes (14 rows)                     |
| `CLAUDE.md`                             | (touched)   | drop "Async-first"                                             |
| `dev-notes/rewrite-bisect-baseline.md`  | 0 (new)     | machine-readable append-only                                   |
| `Makefile`                              | (touched)   | + `threads-rewrite-phase4-guard`, `threads-runtime-meta-check` |

Per-file change descriptions appear in the detail block below.

### Change per file

- **`__init__.py`** — explicit `__all__` listing every export. Preserved (no change): `PiRpcClient`, `PiPythonHarness`, `PythonToolServer`, `PiLaunchConfig`, `BridgeEndpoint`, `ToolRegistry`, `ToolContext`, `ToolResult`, `ToolSpec`, `ToolError`, `PiRpcError`. New: `PiRpcCommandError` (was the old `PiRpcError` semantic; carries failed-command attributes), `PiRpcProcessError` (newly exported; covers process/reader/close failures), `ReentrantRPCError` (new; covers same-thread reentrant send), `EventQueueEmpty` (new; replaces `queue.Empty` at the public API boundary for `next_event(timeout=...)` and `wait_for_event(timeout=...)`). `PiRpcError` is the abstract base; `PiRpcCommandError`, `PiRpcProcessError`, `ReentrantRPCError`, `EventQueueEmpty` all subclass it for `except PiRpcError:` to catch all. Documented in `docs/DESIGN.md` § Failure modes.

- **`harness.py`** — `__aenter__/__aexit__` → `__enter__/__exit__`. New: `_lifecycle_state`, `_lifecycle_lock`, `_start_complete`, `_close_complete`. State machine with seven states (see § Architecture). `start()` transitions `new → starting → started` (or `starting → aborting → failed → closed`); checks `_lifecycle_state == "aborting"` after each cut point. `close()` paths: from `"started"`/`"failed"` → `closing → closed` directly; from `"starting"` → set `"aborting"`, release lock, `_start_complete.wait(timeout)`, then transition to `"closing" → "closed"`; from `"closed"`/`"closing"` → wait on `_close_complete` and return. Restart attempt (`start()` from `"closed"` or `"failed"`) raises `RuntimeError("harness cannot be restarted; create a new instance")`. `close()` body wrapped in `try: ... finally: _lifecycle_state = "closed"; _close_complete.set()`. Partial-start regression test parametrized over five real cut points (server bind, tempdir creation, shim write, faux-provider write, `PiRpcClient.start`).

- **`jsonl.py`** — unchanged code. Docstring note: `StrictJsonlDecoder` is not thread-safe; one instance per stream.

- **`rpc.py`** — full rewrite of async machinery; wire protocol unchanged. Adds `_pending_lock`, `_stdin_closed`, `_closing`, `_fatal_error`, `_in_dispatch`, `_ui_response_pending`, `_ui_response_condition`, `_handlers_lock`, `_events_drop_lock`, `_pop_pending` + `_complete_future` helper pair, watchdog `threading.Timer`, bridge `bridge_write_timeout` config in `PiLaunchConfig`. `prompt_and_wait` rewritten to `threading.Event` + `done.wait(timeout)`; covered by standalone test. Async event/UI handlers rejected at registration; event/UI dispatch also runtime-checks `isawaitable(result) or isasyncgen(result) or isgenerator(result) or isinstance(result, AsyncIterable)`. Non-`None` event handler returns logged as warnings. Reentrant `send()` from same-thread dispatch raises `ReentrantRPCError`. Helper-thread reentrancy explicitly documented as out-of-scope. New: `EventQueueEmpty(PiRpcError)` exception wrapping `queue.Empty` at public API boundary.

- **`server.py`** — full rewrite to `ThreadingTCPServer` + custom subclass that overrides `process_request` to acquire `_handler_slots` *before* thread spawn (closes the v3 "limits execution but not thread creation" gap). Tracks `_active_handler_threads: set[Thread]` for orderly close. Handler gains scoped `settimeout(max(1.0, timeout_ms/1000))` → read initial frame → `settimeout(None)` (T1.3 v3 preserved). Max-bytes counting wrapper on `recv` (8 MiB). Sidecar `recv(1)` distinguishes `b""` (cancel only) from non-empty byte (cancel + `_protocol_violation` flag + warning log); wrapped in `try/except OSError: finally: ctx._cancelled.set()`. Optional `bridge_write_timeout` applied via `socket.settimeout` before each `ctx.update` `sendall` and reset to `None` after (default `None` = no timeout). `PythonToolServer` no longer mutates caller's `ToolRegistry`: takes private snapshot via `registry.snapshot()` at `start()`. `close()` joins tracked handler threads with timeout; explicit `server_close()` after `shutdown()`.

- **`shim.py`** — TS template only; no Python concurrency. Unchanged.

- **`tools.py`** — drop async + async-gen + sync-gen branches in `collect_tool_result`. Drop `asyncio.Event` in `ToolContext`. Reject `async def`, `async-gen`, and `sync-gen` handlers at decoration via `inspect.iscoroutinefunction` / `inspect.isasyncgenfunction` / `inspect.isgeneratorfunction`. Add runtime `inspect.isawaitable(result) or inspect.isasyncgen(result) or inspect.isgenerator(result) or isinstance(result, collections.abc.AsyncIterable)` check in `collect_tool_result`. `ctx.update` becomes sync; docstring states "synchronous; blocks until bridge write returns or `bridge_write_timeout` elapses; default no timeout." `ToolRegistry` gains internal `_registry_lock: threading.Lock` guarding `register()`, `snapshot()`, `get()`, `manifest()` — protects against FT-era race between caller-thread `register()` and server-thread `snapshot()`. `ToolRegistry.snapshot() → ImmutableRegistry` returns a deep-copied frozen view; caller's registry remains mutable for future harness instances.

- **`tests/pi/test_tools.py`** — extends with: async-def-rejection (decoration), sync-streaming-via-ctx.update, sync-gen-rejection (function and wrapper-returned generator object), descriptor-bypass (lambda-returning-coro, `functools.wraps`-of-async, raw `staticmethod`), `AsyncIterable`-instance bypass, `ToolRegistry.snapshot()` immutability + thread-safe snapshot+register concurrent stress, `ToolRegistry._registry_lock` correctness under 100× concurrent register/snapshot on 3.14t.

- **`tests/pi/test_rpc_fake.py`** — rewrite as sync. New tests: `prompt_and_wait` standalone, reentrant `send()` from event handler raises `ReentrantRPCError`, reentrant `send()` from UI handler raises `ReentrantRPCError`, **UI response priority barrier test** (pause normal `send()` post-gate, inject UI request, assert UI response wins on the wire — covers T1.1-v4 write-admission recheck), helper-thread bypass deadlocks (subprocess-based; `subprocess.run(..., timeout=5)` asserts `TimeoutExpired`), close-during-pending stress (N caller threads + concurrent close + late responses → no `InvalidStateError`, all callers fail with `PiRpcProcessError`), close-during-send-precheck causes immediate raise not timeout (T1.4-v4 atomicity), invalid-JSON sets fatal and propagates to subsequent `send()` via `__cause__`, late response after fail does not crash reader (T1.2-v4 `_pop_pending` + `_complete_future` correctness), event-queue overflow drops oldest with rate-limited warning, `EventQueueEmpty` raised by `next_event(timeout=t)` and `wait_for_event(timeout=t)`, dual-delivery ordering: queue-then-handlers.

- **`tests/pi/test_set_model.py`** — rewrite as sync.

- **`tests/pi/test_server.py`** — `socket.create_connection` + bridge helper functions for frame I/O. New tests: handler timeout on partial bytes (T1.3 v3 preserved), small `timeout_ms` still gets 1-second floor (T1.3 floor regression), long-running tool does NOT spuriously cancel (T1.3 settimeout reset proof), half-duplex protocol-violation byte logged + cancellation flagged (T1.4 v3), slowloris bounded — open `max_handlers + 2` connections with `max_handlers=4`; **assert peak `_active_handler_threads` count ≤ max_handlers** (covers v3's "process_request gate" claim with a thread-count test); registry-snapshot rejection of late `register()` on snapshot, concurrent-tool sentinel cross-talk (each tool emits distinct sentinels, each shim connection observes only its own), wire-shape round-trip — unit-level for `ToolSpec.to_manifest()` optional fields + integration-level for `ToolResult.to_wire()` non-default fields, daemon-shutdown cleanliness with uncooperative tool (**out-of-process via `subprocess.run`**), partial-start cleanup parametrized over five real cut points (server bind, tempdir, shim write, faux-provider write, `PiRpcClient.start`), `bridge_write_timeout` honored when set (slow consumer + finite timeout → `ctx.update` raises after timeout).

- **`tests/pi/test_real_pi_integration.py`** — drop `async/await`; rewrite the inline `async def echo` tool body as sync `def echo` with sync `ctx.update`. `with` not `async with`. Same `@pytest.mark.live`.

- **`tests/pi/test_real_llm.py`** — same shape: drop `async/await`, sync harness use, same assertions.

- **`tests/pi/fake_pi_rpc.py`** — extends from immediate-response fixture to mode-driven. Modes: `basic`, `ui-order` (emits `extension_ui_request` after first prompt; records stdin line order to a sidecar file for the test to inspect), `late-response` (delays response until barrier signaled via env var), `fatal-invalid-json` (emits malformed line after N events), `parallel-execute` (emits two concurrent tool calls with distinct sentinels), `slow-consumer` (stops reading stdin for N seconds before resuming). Per-test, the scenario is selected via env var. Synchronization contracts (barriers, ack files) documented inline.

- **`tests/pi/test_runtime_meta.py`** — new; uses `getattr(sys, "_is_gil_enabled", lambda: True)()` (3.11-safe). Records `sys.version_info`, `platform.python_implementation()`, and `gil_enabled` to `.pytest-runtime-meta/<runtime-tag>.json`. Standalone test in the regular suite.

- **`tests/pi/test_lifecycle.py`** — new. Tests for harness lifecycle state machine: double-close from two threads (one closes, other waits on `_close_complete` then returns; teardown runs exactly once), concurrent `start()` while another `start()` in flight (second raises), `start()` after `close()` raises `RuntimeError`, `start()` failure transitions to `failed → closed` cleanly, **concurrent close-during-start at each cut point** (close during server-bind, tempdir, shim write, faux-provider write, `PiRpcClient.start`; assert `start()` returns with abort path, no resource leak, final state `"closed"`). All tests use `@pytest.mark.timeout(10)` for hang protection.

- **`tests/pi/test_async_contract_guard.py`** — new. Walks `tests/` with `ast.NodeVisitor`; asserts: no `async def test_*` functions, no `async def` fixture functions, no `@pytest.mark.asyncio` or `@pytest_asyncio.fixture` decorators, no `import pytest_asyncio` or `from pytest_asyncio import` statements. Runs in regular suite (not just commit-time); replaces the v3 Makefile `grep` guard.

- **`pyproject.toml`** — remove `pytest-asyncio` dev dep and `asyncio_mode = "auto"` ini option (phase 4 only). Add `pytest-timeout>=2.0` (hang-canary safety for the lifecycle and dispatch tests).

- **`docs/DESIGN.md`** — rewrite §§ ToolRegistry (snapshot-not-freeze, `_registry_lock`), ToolContext (sync `ctx.update`, optional `bridge_write_timeout`, blocking semantics), PiRpcClient (sync methods, `ReentrantRPCError` for same-thread reentry, helper-thread out-of-scope, dual-delivery queue-then-handlers, `_events` overflow drops oldest with rate-limited warnings, fatal-state contract, `_closing` postcondition: no callbacks after close), PiPythonHarness (lifecycle state machine including abort path, restart unsupported, partial-start rollback, `close()` always finalizes in `finally`), Failure modes (`PiRpcError` hierarchy with `PiRpcCommandError`, `PiRpcProcessError`, `ReentrantRPCError`, `EventQueueEmpty`). Update architecture sketch. Pin half-duplex bridge contract in § Bridge protocol (with code-level enforcement note). Document `_ui_response_pending` reservation + recheck pattern with rationale. Document `max_handlers` as bounding thread creation (`process_request` override). **Add new § "Breaking changes and migration"** — 14-row table (see § Breaking changes below).

- **`CLAUDE.md`** — § Conventions: replace "Async-first: prefer asyncio over threads" with "Threading-first: `def` for tool functions and `on_*` hooks; `ctx.update` is synchronous and blocking with optional timeout; restart is unsupported (single-use harness); helper-thread reentrancy is unsupported." Spot-check § Architecture for stale async references.

- **`dev-notes/rewrite-bisect-baseline.md`** — new. Machine-readable format, one line per phase commit: `<commit-sha>\t<test-count>\t<sha256-of-sorted-nodeids>`. Initial row = current `main` HEAD's count + signature. CI/Makefile target enforces append-on-commit.

- **`Makefile`** — new targets: `threads-rewrite-phase4-guard` (asserts `pytest-asyncio` not in installed deps, AST guard test passes, current `pytest --collect-only -qq | grep -c '::'` matches last baseline row count, `sha256` of sorted nodeids matches last baseline row signature) and `threads-runtime-meta-check` (runs both 3.11 and 3.14t test_runtime_meta.py via `LIBHARNESS_VENV` override, compares `.pytest-runtime-meta/*.json`, asserts they disagree on `_is_gil_enabled()`). Pre-commit hook on phase branches asserts `git diff --cached --name-only` includes `dev-notes/rewrite-bisect-baseline.md` for each phase commit.

Total: ~1900 LOC touched, ~1300 genuinely new logic (rpc.py + server.py + tools.py + lifecycle.py + fake_pi_rpc.py expansions + new test files). Rest is mechanical / test rewrite / doc work.

## Sequenced phases

Realistic constraint: `rpc.py` and `server.py` are independently asyncio-rooted but their consumers (`harness.py`, the tests) currently `await` them. The rewrite cannot be strictly bottom-up while keeping every commit green. **Recommendation: four-commit feature branch, green only at the end.** Intermediate commits are buildable but `pytest -m "not live"` is not green between phases 1 and 4. Each commit appends to `dev-notes/rewrite-bisect-baseline.md` so test-count + signature bisecting remains possible.

### Phase 1 — `tools.py` + `test_runtime_meta.py` + `test_async_contract_guard.py`

Smallest surface, no I/O. Foundation for `server.py` (uses `ImmutableRegistry`).

- `ToolContext._cancelled: asyncio.Event` → `threading.Event`.
- `ctx.update` becomes sync. Docstring + behavior: "synchronous; blocks until bridge write returns or `bridge_write_timeout` elapses; default `None` = no timeout."
- `collect_tool_result` becomes sync. Drops `inspect.isasyncgen` and `inspect.isawaitable` (function-level) and `inspect.isgenerator` branches.
- Runtime guard (new): `if inspect.isawaitable(result) or inspect.isasyncgen(result) or inspect.isgenerator(result) or isinstance(result, collections.abc.AsyncIterable): raise ToolError(...)`. Single check, all four classes.
- `ToolRegistry.register` rejects `async def`, `async-gen`, sync-gen handlers at decoration.
- `ToolRegistry._registry_lock: threading.Lock`. `register()`, `snapshot()`, `get()`, `manifest()` all acquire. Internal lock protects FT-era race between caller-thread `register()` and server-thread `snapshot()`.
- `ToolRegistry.snapshot() → ImmutableRegistry`: deep-copies the registered tools dict and metadata under `_registry_lock`; returns a frozen view (read-only `get()` + `manifest()` only).
- Add `tests/pi/test_runtime_meta.py`: 3.11-safe via `getattr(sys, "_is_gil_enabled", lambda: True)()`.
- Add `tests/pi/test_async_contract_guard.py`: AST walker.
- Add `pytest-timeout>=2.0` to dev deps.
- Seed initial row in `dev-notes/rewrite-bisect-baseline.md`.

### Phase 2 — `server.py` + extended `fake_pi_rpc.py`

Full rewrite to `ThreadingTCPServer`. Tests in `test_server.py` rewritten. Largest single delta.

- `ThreadingTCPServer` subclass overrides `process_request` to acquire `_handler_slots` *before* `super().process_request` (thread spawn). Release in `process_request_thread`'s `finally`. Closes the v3 "limits execution but not thread creation" gap.
- `_handler_slots = threading.Semaphore(max_handlers)`, default 256. Tests configure `max_handlers=4` for slowloris coverage.
- `_active_handler_threads: set[Thread]` populated in `process_request_thread` entry, removed in `finally`. `PythonToolServer.close()` snapshots and joins each with timeout.
- Handler entry: `request.settimeout(max(1.0, timeout_ms/1000))` → read initial frame within 8 MiB byte cap → `request.settimeout(None)`. Tool execution and sidecar `recv(1)` see a blocking socket.
- Sidecar disconnect-watcher (per execute): wraps `recv(1)` in `try/except OSError: finally: ctx._cancelled.set()`. Distinguishes `b""` (cancel only) from non-empty byte (cancel + sets `ctx._protocol_violation` + logs `"bridge protocol violation: received %r after request frame"`).
- Optional `bridge_write_timeout` (from `PiLaunchConfig`): if set, `ctx.update` applies `self.request.settimeout(bridge_write_timeout)` before each `sendall` and `settimeout(None)` after. Default `None` preserves "blocking forever" contract.
- `PythonToolServer.start()` accepts the live `ToolRegistry`, calls `registry.snapshot()` to obtain `ImmutableRegistry`, stores privately. Caller's registry never mutated.
- Final-response write failure after `cancelled` is set logs `logger.info("tool %s ran to completion after cancellation, result discarded", tool_name)` (F.9).
- Handler-thread leak honesty (T2.6): uncooperative tool body remains on its handler thread until completion. `daemon_threads = True` is process-exit safety only.
- Extended `fake_pi_rpc.py` scenarios: `ui-order`, `late-response`, `fatal-invalid-json`, `parallel-execute`, `slow-consumer`.
- Regression tests added per § Change per file.

### Phase 3 — `rpc.py` + `test_rpc_fake.py` expansion

Full rewrite to `subprocess.Popen` + reader threads. Tests in `test_rpc_fake.py` and `test_set_model.py` rewritten.

- `Popen(..., stdin=PIPE, stdout=PIPE, stderr=PIPE, bufsize=0)`. Two daemon reader threads. Use `proc.stdout.read(4096)` (the buffered path), not raw-fd `os.read` (v2 keep).
- Add `_closing: threading.Event`, set lock-free by `close()` at entry.
- Add `_pending_lock: threading.Lock` for atomic `_fatal_error` + `_pending` transitions.
- Add `_handlers_lock: threading.Lock` for `_event_handlers` list + `_ui_handlers` dict.
- Add `_in_dispatch: threading.local` wrapping BOTH `_dispatch_event` AND `_handle_extension_ui_request`.
- Add `_ui_response_pending: threading.Event` + `_ui_response_condition: threading.Condition` for write-admission with recheck.
- Add `_pop_pending(req_id) → Future | None` and `_complete_future(fut, value=None, exc=None)` helpers.
- Add `_events_drop_lock: threading.Lock` for atomic drop-oldest + put in `_put_event`.
- Add `_watchdog: threading.Timer` per dispatch invocation (warns on hook duration > `hook_warn_threshold_ms`, default 5000).
- New `EventQueueEmpty(PiRpcError)` exception class; `next_event(timeout=t)` and `wait_for_event(timeout=t)` wrap `queue.Empty` → `EventQueueEmpty`.
- Reject coroutine event/UI handlers at registration (`on_event` / `set_extension_ui_handler`); runtime-check handler return for `isawaitable | isasyncgen | isgenerator | AsyncIterable` (parallel to tool guard). Non-`None` event handler returns logged as warning.
- `prompt_and_wait` uses `threading.Event` + `done.wait(timeout)`. Standalone test.
- Stdin invariant: `if self._closing.is_set(): return; with self._send_lock: if not self._stdin_closed: ...write...`. `_stdin_closed` set under `_send_lock` by `close()` *before* `proc.stdin.close()`. Writers swallow `BrokenPipeError | ValueError | OSError` only in close/UI cleanup paths; **normal request path raises `PiRpcProcessError`** (T1.6-v4).
- Hook dispatch contract documented out-of-scope for helper-thread reentry (`ReentrantRPCError` message says "handlers must not synchronously wait on any work that touches the client, regardless of thread").
- `_send_lock` rollback path documented: `_pop_pending` is called outside `_send_lock` (after release); `_complete_future` is called outside both locks.

### Phase 4 — `harness.py` + lifecycle state machine + docs + cleanup

- `async def start/close` → `def start/close`.
- `__aenter__/__aexit__` → `__enter__/__exit__`.
- Lifecycle state machine (full spec in § Design answers § 9): seven states, `_lifecycle_lock` serializes transitions but NOT held during I/O. `_start_complete` event for abort-from-close path.
- `start()` rollback per cut point. `close()` wrapped in `try: ... finally: _lifecycle_state = "closed"; _close_complete.set()`.
- Live tests (`test_real_pi_integration.py`, `test_real_llm.py`) rewritten as sync. Inline `async def echo` tool body in `test_real_pi_integration.py` rewritten as sync.
- Remove `pytest-asyncio` dev dep + `asyncio_mode = "auto"` ini option.
- Phase-4 commit guards via Makefile target `threads-rewrite-phase4-guard`:
  - `tests/pi/test_async_contract_guard.py` passes (AST walker; replaces v3's grep).
  - `pytest --collect-only -qq | grep -c '::'` matches last baseline row count.
  - `sha256` of sorted nodeids matches last baseline row signature.
  - `pytest -m "not live"` passes after `pytest-asyncio` is dropped.
- Pre-commit hook on phase branches asserts `dev-notes/rewrite-bisect-baseline.md` was modified.
- `docs/DESIGN.md` migration (see § Change per file). Add new § "Breaking changes and migration" (14-row table).
- `CLAUDE.md` § Conventions update.
- New Makefile target `threads-runtime-meta-check`.
- Validate: `make all` + `make test-live` on both venvs.

## Design answers

### 1. Subprocess I/O and close behavior (`rpc.py`)

`PiRpcClient` uses `subprocess.Popen` with unbuffered pipes:

```python
subprocess.Popen(
    argv,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    bufsize=0,
)
```

Two daemon reader threads. Stdout reader owns JSONL decoding and message dispatch. Stderr reader appends to `_stderr_chunks`. Both are daemon but explicitly joined during close.

**Read path.** `proc.stdout.read(4096)` (buffered). Not raw-fd `os.read` — that introduces a close-path race with `Popen.__exit__` and has no benefit for stream pipes (which are unbuffered regardless of `bufsize=0`).

**Stdin write path (T1.5/T1.6-v4):**

```python
def _write_stdin(self, payload: bytes, *, allow_during_close: bool = False) -> None:
    """Write a frame to stdin.

    Normal request path (allow_during_close=False): raises PiRpcProcessError
    immediately on broken pipe / closing client. Caller (send()) is responsible
    for rolling back the inserted Future via _pop_pending + _complete_future.

    Best-effort path (allow_during_close=True): swallows OSError; used by
    _send_extension_ui_response and the close-path SIGTERM signal frame.
    """
    if not allow_during_close and self._closing.is_set():
        raise PiRpcProcessError("rpc client is closing")
    with self._send_lock:
        if self._stdin_closed:
            if allow_during_close:
                return
            raise PiRpcProcessError("rpc client stdin closed")
        try:
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            if allow_during_close:
                return
            raise PiRpcProcessError("stdin write failed") from exc
```

**Close sequence (T1.5-v4):**

```python
def close(self) -> None:
    # Step 1: mark closing lock-free so new send() callers fail fast.
    if self._closing.is_set():
        # Already closing; wait for completion.
        self._close_complete.wait(timeout=10)
        return
    self._closing.set()
    # Step 2: set fatal under _pending_lock atomically with snapshot-clear.
    # This is the same critical section send() checks. Race-tight.
    self._set_fatal(PiRpcProcessError("rpc client closed"))
    # Step 3: try to acquire _send_lock cleanly (bounded).
    acquired = self._send_lock.acquire(timeout=1.0)
    try:
        if not acquired:
            # A writer is blocked in stdin.write/flush.
            # Terminate the subprocess to unblock the writer with BrokenPipeError.
            # The writer will then release _send_lock as its except path runs.
            self.process.terminate()
            try:
                self.process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            # Now retry _send_lock acquisition (writer has released).
            acquired = self._send_lock.acquire(timeout=2.0)
            # If still not acquired, something is very wrong; force-close anyway.
        if acquired:
            self._stdin_closed = True
            try:
                self.process.stdin.close()
            except OSError:
                pass
    finally:
        if acquired:
            self._send_lock.release()
    # Step 4: terminate subprocess if still running.
    if self.process.poll() is None:
        self.process.terminate()
        try:
            self.process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
    # Step 5: join reader threads (bounded).
    self._stdout_reader_thread.join(timeout=1.0)
    self._stderr_reader_thread.join(timeout=1.0)
    # Step 6: signal completion.
    self._close_complete.set()
```

The key invariant: `_stdin_closed = True` is **only** set inside `with self._send_lock:`. The fallback path achieves this by terminating the subprocess to make the blocked writer release the lock, then re-acquiring before mutating the flag.

`_fail_pending` is **not** called by `close()` directly — `_set_fatal` handles both the fatal-set and the pending snapshot-clear atomically under `_pending_lock`. See § 3.

### 2. Bridge server (`server.py`)

The server uses `socketserver.ThreadingTCPServer` with two key overrides: `process_request` acquires a handler slot *before* spawning the handler thread, and `process_request_thread` releases it and tracks the thread set.

```python
class PythonToolServer(ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler_cls, *, registry, token, timeout_ms,
                 max_handlers=256, bridge_write_timeout=None):
        super().__init__(addr, handler_cls, bind_and_activate=True)
        self._frozen_registry = registry.snapshot()  # private; caller's registry untouched
        self._token = token
        self._timeout_ms = timeout_ms
        self._bridge_write_timeout = bridge_write_timeout
        self._handler_slots = threading.Semaphore(max_handlers)
        self._active_handler_threads: set[threading.Thread] = set()
        self._handler_threads_lock = threading.Lock()

    def process_request(self, request, client_address):
        if not self._handler_slots.acquire(blocking=False):
            self._reject_capacity(request)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._handler_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        t = threading.current_thread()
        with self._handler_threads_lock:
            self._active_handler_threads.add(t)
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._handler_threads_lock:
                self._active_handler_threads.discard(t)
            self._handler_slots.release()

    def close(self) -> None:
        self.shutdown()
        with self._handler_threads_lock:
            handlers = list(self._active_handler_threads)
        for t in handlers:
            t.join(timeout=2.0)
        self.server_close()
```

**Per-handler hardening:**

```python
class BridgeHandler(BaseRequestHandler):
    def handle(self) -> None:
        # T1.3: timeout scoped to initial-frame read only
        self.request.settimeout(max(1.0, self.server._timeout_ms / 1000.0))
        try:
            request = self._read_request_with_byte_cap(max_bytes=8 * 1024 * 1024)
        finally:
            self.request.settimeout(None)  # restore blocking for sidecar + sendall
        if not self._validate_token(request):
            return
        # ... dispatch manifest or execute ...
```

**Half-duplex contract enforcement (T1.4-v3 preserved):**

```python
def _watch_disconnect(self, ctx: ToolContext) -> None:
    try:
        data = self.request.recv(1)
        if data == b"":
            # Clean EOF: client closed without protocol violation.
            ctx._cancelled.set()
        else:
            # Bridge contract: client sends no further bytes after request frame.
            # Any byte here is a violation.
            ctx._cancelled.set()
            ctx._protocol_violation = True
            logger.warning(
                "bridge protocol violation: received %r after request frame; "
                "cancelling tool and flagging response.",
                data,
            )
    except OSError:
        # Socket reset / closed; treat as cancellation.
        ctx._cancelled.set()
    finally:
        # Defensive: always set, even if recv raised something unexpected.
        ctx._cancelled.set()
```

**`ctx.update` with optional bridge_write_timeout:**

```python
def update(self, value: Any) -> None:
    if self._cancelled.is_set():
        raise _ToolCancelled()
    result = normalize_tool_value(value)
    frame = dumps_line({"type": "update", "result": result.to_wire()})
    timeout = self._server._bridge_write_timeout
    if timeout is not None:
        self._request.settimeout(timeout)
    try:
        self._request.sendall(frame)
    except socket.timeout as exc:
        raise PiRpcProcessError("ctx.update timeout") from exc
    finally:
        if timeout is not None:
            self._request.settimeout(None)
```

Default `bridge_write_timeout=None` preserves the current "blocking forever" contract. Setting a value bounds `ctx.update` for DoS protection against slow consumers; on timeout, the tool sees `PiRpcProcessError` and can choose to retry or abort.

### 3. Request/response correlation (`rpc.py`)

The pending-future map and fatal state are protected by **one** lock (`_pending_lock`):

```python
self._pending_lock = threading.Lock()
self._pending: dict[str, Future[JsonObject]] = {}
self._fatal_error: Exception | None = None
```

**Two helpers, two call patterns (T1.2-v4):**

```python
def _pop_pending(self, req_id: str) -> Future[JsonObject] | None:
    """Used by response path: pop the future out of _pending under lock."""
    with self._pending_lock:
        return self._pending.pop(req_id, None)

def _complete_future(
    self,
    fut: Future[JsonObject],
    *,
    value: JsonObject | None = None,
    exc: Exception | None = None,
) -> None:
    """Used by everyone after they already own a Future (no pop)."""
    try:
        if exc is not None:
            fut.set_exception(exc)
        else:
            fut.set_result(value)
    except InvalidStateError:
        pass  # already completed by another path; benign
```

**Call patterns:**

- **Response path** (`_handle_message`): `fut = _pop_pending(req_id); if fut: _complete_future(fut, value=msg)`.
- **Send rollback** (write failed): `fut = _pop_pending(req_id); if fut: _complete_future(fut, exc=PiRpcProcessError(...))`.
- **Fatal/close** (`_set_fatal`): snapshot-clear `_pending` under lock (yields a `list[(id, Future)]`); release lock; iterate calling `_complete_future(fut, exc=...)`. **Does not** call `_pop_pending` — it already owns the futures from the snapshot.

**`send()` atomicity (T1.1-v4 recheck + T1.4-v4 fatal under lock):**

```python
def send(self, payload: JsonObject) -> JsonObject:
    if getattr(self._in_dispatch, "value", False):
        raise ReentrantRPCError(...)
    # T1.1-v4: UI response priority gate, with recheck after lock acquisition.
    self._wait_ui_response_clear()
    req_id = self._next_id()
    payload = {**payload, "id": req_id}
    fut: Future[JsonObject] = Future()
    # T1.4-v4: fatal check + pending insertion atomic.
    with self._pending_lock:
        if self._fatal_error is not None:
            raise PiRpcProcessError("rpc client is in fatal state") from self._fatal_error
        self._pending[req_id] = fut
    # Write outside _pending_lock (rule: never hold _pending_lock across blocking I/O).
    try:
        self._write_stdin(dumps_line(payload), allow_during_close=False)
    except PiRpcProcessError as exc:
        # Write failed; roll back the future and re-raise.
        popped = self._pop_pending(req_id)
        if popped is not None:
            self._complete_future(popped, exc=exc)
        raise
    # T1.6/Tier-2: timeout-handling distinct from write-handling.
    try:
        return fut.result(timeout=self.config.request_timeout)
    except FutureTimeout:
        popped = self._pop_pending(req_id)
        if popped is not None:
            self._complete_future(popped, exc=PiRpcProcessError("send timeout"))
        raise PiRpcProcessError("send timeout") from None

def _wait_ui_response_clear(self) -> None:
    """Block while a UI response is in flight. Used by send() before write."""
    with self._ui_response_condition:
        while self._ui_response_pending.is_set():
            if not self._ui_response_condition.wait(timeout=self.config.request_timeout):
                raise PiRpcProcessError("UI response pending longer than request_timeout")

def _set_fatal(self, exc: Exception) -> None:
    """Atomically set fatal and capture pending futures, then settle them."""
    logger.exception("reader/close fatal", exc_info=exc)
    with self._pending_lock:
        if self._fatal_error is not None:
            return  # already fatal; idempotent
        self._fatal_error = exc
        pending_snapshot = list(self._pending.values())
        self._pending.clear()
    for fut in pending_snapshot:
        self._complete_future(fut, exc=PiRpcProcessError("rpc client fatal") from exc)
```

The **T1.1-v4 write-admission recheck** lives in `_wait_ui_response_clear`'s `while` loop: if `send()` is admitted (event clear), proceeds past `_wait_ui_response_clear`, then the reader thread sets the event, `send()` will not notice — UNLESS `_send_lock` acquisition forces a recheck. The actual recheck happens at the stdin-write point: `_write_stdin` is wrapped to verify the gate one more time inside the lock.

Actually, simpler v4 approach: hold the UI-gate check ATOMICALLY with `_send_lock` acquisition. `_write_stdin` (normal path) acquires `_ui_response_condition` first, rechecks `_ui_response_pending`, only then acquires `_send_lock`. Reader-thread UI-response write bypasses (`allow_during_close=False, bypass_ui_gate=True`). The two locks are layered: ui_condition first, then `_send_lock`. Caller-thread rolling back a write needs neither lock. This closes the race because the UI-condition recheck is inside the lock's critical section.

**Lock-order rules (v4 final, no contradictions):**

1. `_pending_lock` is **never** held across `Future.result(timeout)` or any blocking I/O.
1. `_send_lock` is **never** held across `Future.result(timeout)` or any blocking computation other than `proc.stdin.write/flush`.
1. `_ui_response_condition` may be held while acquiring `_send_lock` (caller-thread send path; UI-condition first).
1. `_pending_lock` and `_send_lock` are **never** held simultaneously in any path.
1. `_handlers_lock` is **never** held during handler invocation (snapshot under lock, release, then iterate).

**Dual-delivery ordering (v4 pinned):**

```python
def _put_event(self, event: dict) -> None:
    # Queue first (with bounded-size drop-oldest), then handlers.
    self._enqueue_event(event)
    self._dispatch_event(event)
```

A queue consumer therefore sees an event no later than a handler does. Under sustained overflow, queue consumers may miss events that handlers still see — divergence is documented in `docs/DESIGN.md`.

**`_events` overflow (rate-limited per v3 keep):**

```python
def _enqueue_event(self, event: dict) -> None:
    try:
        self._events.put_nowait(event)
    except queue.Full:
        with self._events_drop_lock:
            try:
                self._events.get_nowait()  # drop oldest
            except queue.Empty:
                pass
            try:
                self._events.put_nowait(event)
            except queue.Full:
                pass  # consumer way behind; give up on this event
            self._drop_count += 1
            self._maybe_log_drop()
```

### 4. Cancellation

Cancellation remains cooperative. Threading cannot inject cancellation into arbitrary Python code.

- `ToolContext._cancelled: threading.Event` (was `asyncio.Event`).
- Pi abort → shim closes connection → sidecar `recv(1)` returns `b""` → sets event.
- Bridge protocol violation → sidecar `recv(1)` returns non-empty → sets event + `_protocol_violation`.
- `ctx.update` checks event before write; raises internal `_ToolCancelled` if set.
- Tool body checks `ctx.cancelled` at convenient points; if it doesn't, runs to completion.

**Honest handler-leak semantics (T2.6 v2 keep):** uncooperative tool body remains on its handler thread until completion. `daemon_threads = True` is process-exit safety only; no in-process cancellation. Tests verify process-exit cleanliness **out-of-process** via `subprocess.run`.

**Final-response write after cancellation:**

```python
# In the handler's response-write path:
try:
    self._send_final_response(result)
except (BrokenPipeError, OSError):
    if ctx._cancelled.is_set():
        logger.info(
            "tool %s ran to completion after cancellation, result discarded",
            tool_name,
        )
    else:
        raise
```

### 5. Tool registry and handler shapes

Tool authoring is sync-only.

**Registration-time rejection:**

- `inspect.iscoroutinefunction(fn)` → `ToolError`
- `inspect.isasyncgenfunction(fn)` → `ToolError`
- `inspect.isgeneratorfunction(fn)` → `ToolError`

**Runtime rejection** (`collect_tool_result`):

```python
def collect_tool_result(value: Any, ctx: ToolContext) -> ToolResult:
    if (
        inspect.isawaitable(value)
        or inspect.isasyncgen(value)
        or inspect.isgenerator(value)
        or isinstance(value, collections.abc.AsyncIterable)
    ):
        raise ToolError(
            "sync tool returned an awaitable, async-generator, generator, or async-iterable. "
            "libharness only supports sync tools. See docs/DESIGN.md § Breaking changes."
        )
    return normalize_tool_value(value)
```

This closes the descriptor-bypass class (lambda returning coroutine, `functools.wraps`-of-async, raw `staticmethod`, callable instance with async `__call__`, sync wrapper returning generator object, custom `AsyncIterable` from `__aiter__`).

**Same runtime guard applied to event/UI handlers** (parallel — see § 6).

**`ToolRegistry` internal locking:**

```python
class ToolRegistry:
    def __init__(self) -> None:
        self._registry_lock = threading.Lock()
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, name: str, fn: Callable, ...) -> None:
        self._reject_async_handlers(fn)  # decoration-time guard
        with self._registry_lock:
            self._tools[name] = RegisteredTool(...)

    def get(self, name: str) -> RegisteredTool | None:
        with self._registry_lock:
            return self._tools.get(name)

    def manifest(self) -> list[dict]:
        with self._registry_lock:
            return [t.spec.to_manifest() for t in self._tools.values()]

    def snapshot(self) -> ImmutableRegistry:
        """Deep-copy a frozen view under lock."""
        with self._registry_lock:
            return ImmutableRegistry(dict(self._tools))
```

Caller's `ToolRegistry` is **never mutated by the harness**. Late `register()` succeeds locally but is invisible to a running server.

### 6. Hook, event, and UI dispatch

All event/UI callbacks run synchronously on the stdout reader thread. v4 fully closes the v3-residual races.

**Handler registries are protected:**

```python
self._handlers_lock = threading.Lock()
self._event_handlers: list[EventHandler] = []
self._ui_handlers: dict[str, UiHandler] = {}
self._fallback_ui_handler: UiHandler | None = None
```

`on_event()`, `set_extension_ui_handler()`, unsubscribe all mutate under lock. Dispatch snapshots under lock, releases, then iterates.

**Registration rejects coroutine functions and runtime checks return values** (parallel to tool path):

```python
def on_event(self, handler: EventHandler) -> Callable[[], None]:
    if inspect.iscoroutinefunction(handler) or inspect.isasyncgenfunction(handler):
        raise TypeError("async event handlers are not supported; see docs/DESIGN.md")
    with self._handlers_lock:
        self._event_handlers.append(handler)
    return lambda: self._unsubscribe(handler)

def _dispatch_event(self, event: dict) -> None:
    self._in_dispatch.value = True
    watchdog = self._make_watchdog(event)
    try:
        with self._handlers_lock:
            handlers = list(self._event_handlers)
        for h in handlers:
            try:
                result = h(event)
                self._check_handler_return(result, h)
            except Exception:
                logger.exception("event handler %r raised", h)
    finally:
        watchdog.cancel()
        self._in_dispatch.value = False

def _check_handler_return(self, result: Any, handler: Callable) -> None:
    if (
        inspect.isawaitable(result)
        or inspect.isasyncgen(result)
        or inspect.isgenerator(result)
        or isinstance(result, collections.abc.AsyncIterable)
    ):
        raise TypeError(
            f"event/UI handler {handler!r} returned awaitable/async-gen/gen/async-iter; "
            "see docs/DESIGN.md § Breaking changes."
        )
    if result is not None:
        logger.warning(
            "event handler %r returned non-None (%r); return value is ignored",
            handler, type(result).__name__,
        )
```

**The reader sets `_in_dispatch.value = True` around BOTH `_dispatch_event` AND `_handle_extension_ui_request`.** `client.send()` checks this thread-local at entry and raises `ReentrantRPCError` for same-thread reentry.

**Helper-thread reentrancy** (`threading.local` doesn't propagate to spawned threads) is **explicitly out-of-scope**. The `ReentrantRPCError` message and `docs/DESIGN.md` both say: *"handlers must not synchronously wait on any work that touches the client, regardless of which thread the work runs on."* Test: subprocess-based — handler spawns a worker that calls `client.get_state()` and joins; `subprocess.run(..., timeout=5)` asserts `TimeoutExpired`.

**UI request handling — write-admission with recheck (T1.1-v4):**

```python
def _handle_extension_ui_request(self, message: dict) -> None:
    with self._ui_response_condition:
        self._ui_response_pending.set()
    try:
        # Both event dispatch AND UI handling marked as "in dispatch"
        self._dispatch_event(self._wrap_as_event(message))
        self._in_dispatch.value = True
        try:
            with self._handlers_lock:
                handler = self._ui_handlers.get(message["component"]) or self._fallback_ui_handler
            response = handler(message) if handler else self._default_ui_response(message)
            self._check_handler_return(response, handler or "default_ui")
        finally:
            self._in_dispatch.value = False
        # Reader-owned UI response write: bypasses the UI gate it just set.
        self._write_stdin_ui_response(message["id"], response)
    finally:
        with self._ui_response_condition:
            self._ui_response_pending.clear()
            self._ui_response_condition.notify_all()

def _write_stdin_ui_response(self, message_id: str, response: dict) -> None:
    # No UI-gate wait; reader owns this write.
    frame = dumps_line({"type": "extension_ui_response", "id": message_id, **response})
    if self._closing.is_set():
        return  # close path; skip best-effort
    with self._send_lock:
        if self._stdin_closed:
            return
        try:
            self.process.stdin.write(frame)
            self.process.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass  # best-effort
```

Ordinary `send()` uses `_wait_ui_response_clear()` (with timeout) before any write, AND the gate-check is **layered** so `send()` cannot interleave:

```python
def send(self, payload: dict) -> dict:
    if getattr(self._in_dispatch, "value", False):
        raise ReentrantRPCError(...)
    # ... fatal/pending insertion under _pending_lock ...
    # NOW write; the recheck is inside _write_stdin (normal path):
    self._write_stdin_normal(payload_bytes, req_id)
    # ... wait on Future ...

def _write_stdin_normal(self, payload: bytes, req_id: str) -> None:
    # T1.1-v4 layered locking: ui_condition first, then _send_lock.
    # Recheck the UI gate INSIDE the condition, after acquiring it,
    # to close the race where the gate became set after the initial wait.
    with self._ui_response_condition:
        while self._ui_response_pending.is_set():
            if not self._ui_response_condition.wait(timeout=self.config.request_timeout):
                raise PiRpcProcessError("UI gate stuck")
        # We hold ui_condition. Acquire _send_lock under it.
        # Reader-owned UI response write does NOT acquire ui_condition,
        # so this layered acquisition is deadlock-free.
        if self._closing.is_set():
            raise PiRpcProcessError("rpc client is closing")
        with self._send_lock:
            if self._stdin_closed:
                raise PiRpcProcessError("stdin closed")
            try:
                self.process.stdin.write(payload)
                self.process.stdin.flush()
            except (BrokenPipeError, ValueError, OSError) as exc:
                raise PiRpcProcessError("stdin write failed") from exc
```

Why this closes the v3-residual race: a caller acquires `_ui_response_condition`, then re-checks `_ui_response_pending` under it. If the reader is mid-UI-handling, the reader has either (a) not yet acquired the condition to set the flag — and will block trying to set it (since the caller holds the condition's lock), or (b) already set the flag — and the caller sees it inside the condition and waits. Either way: no caller-thread normal write can interleave between the reader's flag-set and the reader's UI-response write.

Reader-owned UI-response write does NOT acquire `_ui_response_condition` (see `_write_stdin_ui_response` above), so no deadlock.

**Hook watchdog (T4):**

```python
def _make_watchdog(self, event: dict) -> threading.Timer:
    threshold = self.config.hook_warn_threshold_ms / 1000.0
    t = threading.Timer(
        threshold,
        lambda: logger.warning(
            "hook dispatch for event %r has been running >%dms; "
            "long-running hooks block event delivery and may stall pi.",
            event.get("type"), self.config.hook_warn_threshold_ms,
        ),
    )
    t.daemon = True
    t.start()
    return t
```

### 7. Freethreading-specific position

Idiomatic threaded code, no FT-only branches. Same code runs on 3.11 (GIL) and 3.14t (no GIL).

**Grounded wins:**

1. Parallel bridge `execute` requests can truly run CPU-bound Python tool bodies in parallel under 3.14t.
1. RPC reader, bridge handlers, and tool threads parse JSON and execute Python concurrently.

**Where FT doesn't help:**

- Stdin writes serialized by `_send_lock`.
- Pi is a separate process.
- I/O-bound tool code already releases GIL on 3.11.

**Runtime-meta test** (E.m3 + 3.11-safe): `tests/pi/test_runtime_meta.py::test_runtime_is_what_we_think` records `sys.version_info` + `platform.python_implementation()` + `getattr(sys, "_is_gil_enabled", lambda: True)()` to `.pytest-runtime-meta/<runtime-tag>.json`. CI step (`make threads-runtime-meta-check`) compares 3.11 vs 3.14t outputs and asserts they disagree on `_is_gil_enabled()`.

### 8. Test suite

The suite becomes sync pytest. `pytest-asyncio` is removed in phase 4 after every async test/fixture is converted.

Test infrastructure (v4):

- `fake_pi_rpc.py` becomes mode-driven. Each test names its scenario.
- Bridge tests use local helpers for `socket.create_connection`, JSONL frame writes, response reads.
- Hang-canaries use `@pytest.mark.timeout(N)`.
- Daemon-shutdown cleanliness is out-of-process.
- Helper-thread bypass deadlock test is **subprocess-based** with `subprocess.run(..., timeout=5)` asserting `TimeoutExpired`.
- AST guard (`test_async_contract_guard.py`) runs in regular suite; replaces v3's Makefile grep.
- Phase-4 Makefile guard checks: AST guard passes, collect-only count matches baseline, sha256 of sorted nodeids matches baseline.

### 9. Cleanup gotchas and lifecycle state

**Client states** (`PiRpcClient`):

| State      | Meaning                                                     |
| ---------- | ----------------------------------------------------------- |
| `new`      | Constructed, not started.                                   |
| `starting` | Process launch in progress.                                 |
| `started`  | Process and reader threads running.                         |
| `closing`  | Close in progress; new sends fail with `PiRpcProcessError`. |
| `closed`   | Terminal normal state.                                      |
| `failed`   | Startup or reader fatal before close.                       |

**Harness states** mirror client states plus an extra `aborting` state used when `close()` is called during `"starting"`: `start()` checks `_lifecycle_state == "aborting"` after each cut point and aborts early; `close()` waits on `_start_complete` before running cleanup.

**Lifecycle transitions:**

```text
new ──start()──→ starting ──┬── (success) ──→ started ──close()──→ closing ──→ closed
                            │
                            ├── (start failure) ──→ failed ──close()──→ closing ──→ closed
                            │
                            └── (close during start) ──→ aborting ──→ failed ──→ closing ──→ closed
                                                                ↑
                                                                ├── start() checks state after each
                                                                │   cut point; if "aborting", raises
                                                                │   internally and start() returns
                                                                │   via try/except → close() finishes
```

`close()` is idempotent. Concurrent `close()` from another thread waits on `_close_complete` and returns. `start()` is allowed only from `"new"`; any other state raises `RuntimeError`. `start()` after `close()` raises `RuntimeError("harness cannot be restarted; create a new instance")`.

**`close()` is always-finalizing:**

```python
def close(self) -> None:
    with self._lifecycle_lock:
        state = self._lifecycle_state
        if state in ("closed", "closing"):
            # Concurrent close; wait for the in-flight one to finish.
            done_event = self._close_complete
        elif state == "starting":
            # Abort the start.
            self._lifecycle_state = "aborting"
            done_event = None  # will run cleanup ourselves after waiting
        elif state in ("started", "failed"):
            self._lifecycle_state = "closing"
            done_event = None
        else:  # "new"
            self._lifecycle_state = "closed"
            self._close_complete.set()
            return

    if done_event is not None:
        done_event.wait(timeout=10.0)
        return

    # Wait for any in-progress start to abort.
    if not self._start_complete.wait(timeout=10.0):
        logger.warning("close: start did not complete within timeout; proceeding")

    # Now run cleanup. Guarantee finalization in finally.
    try:
        self._cleanup()  # closes server, pi, tempdir, etc.
    except Exception:
        logger.exception("close: cleanup raised; final state will still be 'closed'")
    finally:
        with self._lifecycle_lock:
            self._lifecycle_state = "closed"
        self._close_complete.set()
```

**Partial-start failure injection points** (parametrized regression test):

1. `PythonToolServer.start()` bind failure (port in use).
1. Tempdir / artifact root creation failure.
1. Bridge shim write failure.
1. Faux-provider shim write failure (only when `fake_provider=True`).
1. `PiRpcClient.start()` failure after server start (e.g. missing pi binary).

Each path must clean up the server, terminate any partial pi process, and remove the tempdir unless `keep_temp=True`. Real failure modes (not just monkey-patches): port already bound, pi binary missing, faux extension path missing.

**Resource cleanup inventory:**

| Resource                | Mitigation                                                       |
| ----------------------- | ---------------------------------------------------------------- |
| Pi process              | bounded `wait` → `terminate` → `wait` → `kill`                   |
| stdin pipe              | best-effort close under `_send_lock` (or after SIGTERM fallback) |
| Reader threads          | explicit join with bounded timeout                               |
| Pending Futures         | settled by `_set_fatal` under `_pending_lock` snapshot-clear     |
| Bridge listener socket  | `shutdown()` + `server_close()`                                  |
| Bridge handler threads  | server-tracked `_active_handler_threads`; bounded join           |
| Watcher threads         | best-effort join; finalizer sets `_cancelled`                    |
| Tempdir                 | `tempfile.TemporaryDirectory.cleanup()` in `_cleanup`            |
| Caller's `ToolRegistry` | never mutated (private snapshot pattern)                         |
| `_lifecycle_state`      | always finalizes to `"closed"` via `try/finally`                 |

### 10. Concrete regression tests to write

40 tests, each tagged. Hang-canaries use `@pytest.mark.timeout(N)`. Cross-runtime tests are required.

**Original 10 (kept, refined):**

1. Bridge handler leak when pi disconnects mid-stream. Tool calls `ctx.update` 10×; pi closes after frame 3; assert handler returns, sidecar joins, server-owned `_active_handler_threads` count returns to baseline.
1. Subprocess SIGKILL externally. Kill pi from outside; `client.close()` returns within bounded time; reader threads exit; pending requests fail with `PiRpcProcessError`.
1. Pending request abandoned at close. Send + never receive response + `close()` → caller's `send()` raises `PiRpcProcessError` via `_set_fatal` → `_complete_future` chain.
1. Concurrent execute requests with sentinels. Two parallel tools; each emits N distinct sentinels (`a1..a10`, `b1..b10`); each connection observes only its own.
1. Concurrent prompt and event delivery. Caller calls `client.prompt(...)` while reader delivers event; no deadlock.
1. `async def` tool registration → `ToolError` at decoration.
1. Restart raises. `close()` then `start()` raises `RuntimeError`.
1. **Out-of-process daemon-shutdown cleanliness** with uncooperative tool. `subprocess.run([sys.executable, "-c", script], timeout=10)` returns 0; tool body blocks on `Event.wait()` without polling cancelled.
1. Bridge token mismatch under racing connections.
1. Reader thread observes invalid JSON. `_set_fatal` atomically clears pending; subsequent `send()` raises `PiRpcProcessError` with `__cause__` set.

**v2/v3-kept (refined):**

1. `prompt_and_wait` standalone (T2.5).
1. Reentrant `client.send()` from event handler raises `ReentrantRPCError`. `@pytest.mark.timeout(5)`.
1. Bridge handler timeout on partial bytes (T1.3).
1. **Tiny `timeout_ms=10` still gets 1-second floor** (T1.3 floor regression).
1. Half-duplex protocol violation byte → logged + cancellation flagged (T1.4).
1. Slowloris bounded — `max_handlers=4`, open `max_handlers+2`, **assert peak `_active_handler_threads` count ≤ max_handlers**, then close one, assert next connection succeeds.
1. `ToolRegistry.snapshot()` immutability — take snapshot, mutate original, snapshot unchanged.
1. Wire-shape unit test for `ToolSpec.to_manifest()` optional fields.
1. Wire-shape integration test for `ToolResult.to_wire()` non-default fields via shim.
1. Descriptor-bypass detection (× 6 sub-cases): lambda-returning-coro, `functools.wraps`-of-async, raw `staticmethod`, callable instance with async `__call__`, sync wrapper returning generator, custom `AsyncIterable` from `__aiter__`. Each raises at execute time.
1. Sync-gen rejection at registration. `def f(): yield 1` raises `ToolError` at decoration.
1. Partial-start cleanup parametrized over five real cut points.
1. UI-event-before-UI-response ordering AND UI response priority vs racing prompt (T1.1-v4 barrier test).
1. Stdin write race during `close()`. Concurrent close + writer; no exception escapes.
1. Slow pi consumer blocks `ctx.update` (default no timeout).
1. Runtime-meta records actual runtime (E.m3, 3.11-safe).

**New in v4:**

1. **Reentrant `send()` from UI handler** raises `ReentrantRPCError`. `@pytest.mark.timeout(5)`.
1. **Helper-thread bypass deadlock — subprocess-based.** `subprocess.run(..., timeout=5)` asserts `TimeoutExpired`.
1. **Long-running tool does NOT spuriously cancel.** Tool sleeps `2 * timeout_ms` with no client disconnect; `ctx.update` works, no spurious cancellation, no `socket.timeout`.
1. **`_pending` multi-thread stress.** N caller threads + concurrent close + late responses → no `InvalidStateError`, all callers fail with `PiRpcProcessError`, no hangs.
1. **Late response after `_set_fatal` does not crash reader** (T1.2-v4 `_complete_future` swallows `InvalidStateError`).
1. **Fatal between fatal-check and pending-add causes immediate raise** (T1.4-v4 atomicity). Barrier-driven.
1. **`close()` with stuck writer.** Subprocess stops reading stdin + writer blocked in `flush` + concurrent `close()`. Assert close returns within 5s; pending callers fail immediately; SIGTERM fired.
1. **Lifecycle state machine:** concurrent close from two threads → one closes, other waits + returns; one teardown.
1. **Lifecycle state machine:** concurrent close-during-start at each cut point (5 sub-cases). Use barriers in fake `start()` cut points; concurrent thread calls `close()`; assert start aborts, no resource leak, final state `"closed"`.
1. **Event handler descriptor-bypass detection** — same six cases as tool path; all raise `TypeError` at dispatch.
1. **Non-`None` event handler return** logs a warning (does not raise).
1. **Concurrent `ToolRegistry.register()` + `snapshot()` on 3.14t.** 100 iterations × 2 threads; no corruption, snapshot self-consistent.
1. **Hook duration warning.** 6-second `time.sleep` in `on_event` + `hook_warn_threshold_ms=5000` → warning logged.
1. **Dual-delivery ordering: queue-then-handlers.** A handler that blocks on a barrier; a parallel `next_event()` consumer; verify consumer observes the event after queue.put but before the handler returns.
1. **Top-level exception catch.** `try: ...; except libharness.pi.PiRpcError: ...` catches all four subclasses (`PiRpcCommandError`, `PiRpcProcessError`, `ReentrantRPCError`, `EventQueueEmpty`). Verify all importable from `libharness.pi`.
1. **AST contract guard test.** `tests/pi/test_async_contract_guard.py` walks `tests/`; asserts no async test fns, no async fixtures, no `@pytest.mark.asyncio`, no `pytest_asyncio` imports.
1. **`bridge_write_timeout` honored** when set. `PythonToolServer(bridge_write_timeout=0.1)` + slow consumer → `ctx.update` raises `PiRpcProcessError` after 0.1s. Default `None` (test #25) still blocks indefinitely.
1. **`EventQueueEmpty` raised** (not `queue.Empty`) by `next_event(timeout=0.1)` on empty queue. Same for `wait_for_event`.
1. **`make threads-runtime-meta-check`** compares 3.11 vs 3.14t artifacts; CI asserts they disagree on `_is_gil_enabled()`.
1. **Bisect-baseline guard.** `make threads-rewrite-phase4-guard` fails if (a) AST guard fails, (b) collect-only count ≠ baseline, (c) sha256 of sorted nodeids ≠ baseline.

## Tradeoffs summary (decision register)

Every row reflects the v4 plan; *(v4)* marks changes from v3.

| Decision                             | Picked                                                                                               |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| Server framework                     | `ThreadingTCPServer` with `process_request` override                                                 |
| Handler-slot gate                    | `process_request` BEFORE thread spawn; bounds thread creation *(v4 unified)*                         |
| Handler-slot default                 | 256                                                                                                  |
| Server-owned handler thread tracking | `_active_handler_threads: set[Thread]` *(v4)*                                                        |
| Registry lifecycle                   | private `ImmutableRegistry` snapshot (caller registry never mutated)                                 |
| Registry internal locking            | `_registry_lock` guards `register`/`get`/`manifest`/`snapshot` *(v4)*                                |
| Initial-frame timeout                | scoped `settimeout(max(1.0, timeout_ms/1000))` → read → `settimeout(None)`                           |
| `ctx.update` timeout                 | default `None` (blocking); optional `bridge_write_timeout` config *(v4)*                             |
| Half-duplex enforcement              | sidecar distinguishes `b""` vs non-empty byte (cancel + flag)                                        |
| Tool authoring                       | sync-only                                                                                            |
| Tool runtime guard                   | rejects awaitable, async-gen, generator, `AsyncIterable` *(v4 extended)*                             |
| RPC subprocess reads                 | `proc.stdout.read(4096)`                                                                             |
| RPC close sequence                   | bounded `_send_lock` w/ SIGTERM fallback; `_stdin_closed` invariant preserved *(v4)*                 |
| `close()` finalization               | `try/finally`: state always reaches `"closed"`, `_close_complete` always set *(v4)*                  |
| Pending map                          | `dict[id, Future]` + `_pending_lock`                                                                 |
| Future settlement                    | `_pop_pending(req_id)` + `_complete_future(fut, ...)`; two helpers, two call patterns *(v4 split)*   |
| Fatal atomicity                      | `_fatal_error` set + pending snapshot-clear in one `_pending_lock` critical section                  |
| `send()` rollback                    | write-failure path separate from wait-handling timeout *(v4)*                                        |
| `_write_stdin` normal path           | raises `PiRpcProcessError` on broken pipe / closing client *(v4)*                                    |
| `_write_stdin` cleanup path          | swallows `OSError` only when `allow_during_close=True` (close path, UI response) *(v4)*              |
| UI response priority                 | layered locking: `_ui_response_condition` first, then `_send_lock` *(v4 recheck inside condition)*   |
| Handler registries                   | `_handlers_lock` for mutate + snapshot                                                               |
| Reentrant dispatch                   | same-thread `ReentrantRPCError` (covers BOTH event and UI dispatch via `_in_dispatch` wrapping both) |
| Helper-thread reentry                | documented unsupported; will deadlock; subprocess-tested                                             |
| Slow handler observability           | `threading.Timer` watchdog logs warning                                                              |
| Events queue                         | bounded `maxsize=4096`; drop-oldest under `_events_drop_lock`; rate-limited warning log              |
| Dual-delivery ordering               | queue.put first, then handlers *(v4 pinned)*                                                         |
| Lifecycle states                     | seven: `new`/`starting`/`aborting`/`started`/`closing`/`closed`/`failed` *(v4)*                      |
| Close-during-start                   | abort flag + `_start_complete` event; `close()` waits for start to finalize before cleanup *(v4)*    |
| Lifecycle lock scope                 | guards transitions only; NOT held during I/O *(v4)*                                                  |
| Restart                              | unsupported; raises `RuntimeError`                                                                   |
| Public exception base                | `PiRpcError` (abstract) + four subclasses *(v4)*                                                     |
| New exception classes                | `PiRpcCommandError`, `PiRpcProcessError`, `ReentrantRPCError`, `EventQueueEmpty` *(v4)*              |
| Public exports                       | explicit `__all__`; every existing symbol preserved + four new exceptions *(v4)*                     |
| `_events` timeout exception          | `EventQueueEmpty(PiRpcError)` wraps `queue.Empty` at public boundary *(v4)*                          |
| Test guards                          | `test_async_contract_guard.py` AST walker in regular suite *(v4 — drops grep)*                       |
| Phase-4 Makefile guard               | AST + collect-count match + nodeid-signature match *(v4 signature)*                                  |
| Bisect-baseline append               | enforced via pre-commit / CI on phase branches *(v4)*                                                |
| Runtime-meta CI check                | `make threads-runtime-meta-check` compares 3.11 vs 3.14t *(v4)*                                      |
| FT branches in code                  | none                                                                                                 |
| Phase boundaries                     | 4 commits; green at end; bisect-baseline appended each commit                                        |
| Migration docs scope                 | `docs/DESIGN.md` rewrite + 14-row § Breaking changes + `CLAUDE.md` cleanup *(v4)*                    |

## Rejected alternatives

Documented for future revisers; do not relitigate without new evidence.

- *Writer-priority thread for UI responses.* Cleaner priority semantics but adds a thread + queue + failure modes. Layered locking (UI condition before `_send_lock`) achieves the same wire-ordering guarantee without new infrastructure.
- *Hold `_send_lock` across UI handler invocation.* Trivially correct but stalls every writer for arbitrary handler duration. The layered-condition approach stalls only during the actual UI-response write window.
- *Process-wide dispatch-depth counter for helper-thread reentry.* Catches helper-thread reentry but false-positives on unrelated caller sends during slow handlers. The thread-spawned-by-handler vs unrelated-caller distinction requires actual worker-pool tracking; defer to event-bridge.
- *Freeze caller-owned `ToolRegistry` at start.* Catches late registration loudly but mutates user-owned state and leaks the mutation on partial-start failure. Private snapshot + internal `_registry_lock` is non-destructive.
- *Per-commit greenness via dual-mode `collect_tool_result` during phases 1–3.* ~30 LOC of throwaway code. Bisect-baseline approach is the cheaper way to keep `git bisect` useful across the green-only-at-end branch.
- *Raw-fd `os.read(stdout_fd, 4096)`.* Plan claimed it avoided a buffering deadlock; argument applies to writers not readers; `Popen` stdout pipes are unbuffered for streams regardless. Raw-fd reads also race `Popen.__exit__`'s fd close.
- *Pytest-timeout for "assert this hangs."* `pytest.mark.timeout` is a failure mechanism, not a passing assertion. Subprocess-based testing with `subprocess.run(..., timeout=N)` asserting `TimeoutExpired` is the right primitive for "this should hang."
- *Single `_settle_future` polymorphic helper.* v3 used one helper with ambiguous ownership (pop-by-id vs already-owned). Splitting into `_pop_pending` + `_complete_future` matches the actual two call patterns without isinstance checks.
- *Set `_closing` lock-free + separately check in `send()`.* v3's two-step check (`_closing` outside lock, `_fatal_error` inside lock) had a race window. v4 sets `_fatal_error` under `_pending_lock` at close start; `send()`'s existing single critical section handles both.
- *Makefile `grep -rE 'async def test_' tests/`.* AST walker covers grep cases plus async fixtures, `@pytest.mark.asyncio`, `pytest_asyncio` imports. AST is the single source of truth.

## Breaking changes and migration

The rewrite introduces 14 user-visible contract changes. All land in the same merge as the rewrite. The compact table indexes them; full migration prose goes in `docs/DESIGN.md` § Breaking changes and migration.

| #   | Surface             | Old → New                                               |
| --- | ------------------- | ------------------------------------------------------- |
| 1   | client RPC methods  | `await client.X(...)` → `client.X(...)`                 |
| 2   | client context mgr  | `async with PiRpcClient` → `with PiRpcClient`           |
| 3   | harness context mgr | `async with PiPythonHarness` → `with PiPythonHarness`   |
| 4   | tool streaming      | `await ctx.update(...)` → `ctx.update(...)`             |
| 5   | event handler shape | `async def on_event` → `def on_event`                   |
| 6   | UI handler shape    | `async def ui_handler` → `def ui_handler`               |
| 7   | tool body           | sync generator yielding → sync `def` + `ctx.update`     |
| 8   | tool body (async)   | `async def tool` → sync `def tool`                      |
| 9   | server context mgr  | `async with PythonToolServer` → `with PythonToolServer` |
| 10  | server lifecycle    | `await server.start()` / `await server.close()` → sync  |
| 11  | restart             | accidental restart worked → restart raises              |
| 12  | late tool register  | sometimes visible → snapshot frozen at start            |
| 13  | reentrant `send()`  | asyncio yielded → `ReentrantRPCError`                   |
| 14  | `_events` queue     | unbounded → bounded (drops oldest)                      |

**Detail:**

- *#1 + #4 (drop `await`).* `client.prompt`, `client.get_state`, `client.set_model`, `prompt_and_wait`, `ctx.update`, and every other previously-`await`-able method becomes plain `def`. Trying to `await` raises `TypeError("object * is not awaitable")` at runtime. `ctx.update` is now blocking; default no timeout, optional `bridge_write_timeout` for DoS mitigation.
- *#2, #3, #9 (sync context managers).* `__aenter__`/`__aexit__` removed; only `__enter__`/`__exit__`. `async with` raises `TypeError` at runtime.
- *#5, #6 (sync handlers).* `on_event(async def ...)` and `set_extension_ui_handler(..., async def ...)` raise `TypeError` at registration. Plus runtime check: handler returning awaitable/async-gen/generator/`AsyncIterable` raises `TypeError` at dispatch.
- *#7 (no sync generators).* Sync-gen tools rejected at registration via `inspect.isgeneratorfunction`. Wrapper-returned generator objects rejected at execute via runtime `inspect.isgenerator(result)`. Migrate by replacing `yield x` with `ctx.update(x)` and `return final_result`.
- *#8 (no async tool bodies).* Async tool functions, async-gen tool functions, and sync wrappers returning awaitables/async-gens/AsyncIterables all rejected. Migrate to sync `def` returning `ToolResult`, using `ctx.update` for streaming.
- *#10 (server sync).* `PythonToolServer.start()` and `close()` are sync. Direct server users (rare; mostly internal) drop `await`.
- *#11 (restart).* `harness.close()` then `harness.start()` raises `RuntimeError("harness cannot be restarted; create a new instance")`. Enforced by lifecycle state machine.
- *#12 (late tool register).* `PythonToolServer` takes a snapshot at `start()`. Caller's `ToolRegistry` is never mutated and stays usable for future harness instances. Late `register()` succeeds locally but isn't visible to the running server.
- *#13 (reentrant `send()`).* `client.send(...)` from inside an event/UI handler raises `ReentrantRPCError` (subclass of `PiRpcError`). Helper-thread variant (handler spawns a thread that calls `send()`) is **not detected** and will deadlock — documented contract violation.
- *#14 (events queue bounded).* `_events = queue.Queue(maxsize=4096)`. On full: drop oldest, log warning rate-limited. `next_event(timeout=t)` / `wait_for_event(timeout=t)` now raise `EventQueueEmpty(PiRpcError)` instead of `queue.Empty`. Subscribers via `on_event` handlers always see every event (dual-delivery preserved, divergence under overflow documented).

**Exception hierarchy change** (not numbered separately):

- `PiRpcError` becomes the abstract base.
- `PiRpcCommandError` (new) carries the failed-command attributes (was the old `PiRpcError` semantic). Existing `except PiRpcError:` catches via base — no breaking change for that case. Callers that want narrower handling switch to `except PiRpcCommandError:`.
- `PiRpcProcessError` (newly exported) covers process/reader/close failures.
- `ReentrantRPCError` (new) covers same-thread reentrant `send()` from inside dispatch.
- `EventQueueEmpty` (new) replaces `queue.Empty` at the public boundary.

## Critical files (read in order)

1. `dev-notes/2026-05-16-threads-rewrite-plan-v3-review-synthesis.md` (most recent synthesis; informs all v4 changes)
1. `src/libharness/pi/rpc.py` (current asyncio)
1. `src/libharness/pi/server.py`
1. `src/libharness/pi/tools.py`
1. `src/libharness/pi/harness.py`
1. `dev-notes/predecessors/v4/src/pi_python_harness/broker.py` (threads-sync precedent)
1. `tests/pi/fake_pi_rpc.py` (mode-driven scenarios will extend this)
1. `tests/pi/test_rpc_fake.py`, `test_server.py`
1. `dev-notes/2026-05-15-concurrency-model-discussion.md`
1. `dev-notes/2026-05-14-event-bridge-proposal.md` § "Dispatch mechanism" (forward context; not a gating dependency)
1. `docs/DESIGN.md` (sections to be rewritten in phase 4)
1. `CLAUDE.md` § Conventions (drop "Async-first")

## What needs author sign-off

v4 picks design choices on five Tier-1 closures from v3 review + ten Tier-2 cleanups. Before coding starts:

1. **T1.1-v4 UI response priority via layered locking.** `_ui_response_condition` is acquired before `_send_lock` in caller-thread sends; reader-owned UI-response write does NOT acquire the condition. Recheck inside the condition closes the v3 post-gate race. Confirm.
1. **T1.2-v4 Future settlement split.** Two helpers (`_pop_pending`, `_complete_future`), two call patterns. v3's single ambiguous `_settle_future` is gone. Confirm.
1. **T1.3-v4 Lifecycle abort path.** `close()` during `"starting"` sets `_lifecycle_state = "aborting"`, waits on `_start_complete`, then cleans up. `start()` checks state after each cut point. Confirm acceptable that close can wait up to 10 seconds for a slow start.
1. **T1.4-v4 Fatal-under-lock for close.** `close()` sets `_fatal_error = PiRpcProcessError("client closed")` under `_pending_lock` at entry, atomic with snapshot-clear. Reuses existing fatal-check in `send()`. Confirm.
1. **T1.5-v4 `_stdin_closed` invariant preserved in fallback.** On lock-acquire timeout: terminate subprocess first (unblocks writer with `BrokenPipeError`), then acquire lock cleanly. Confirm.
1. **Public exception hierarchy.** `PiRpcError` abstract base; `PiRpcCommandError` (renamed; carries old failed-command attrs); `PiRpcProcessError`, `ReentrantRPCError`, `EventQueueEmpty` new exports. Confirm acceptable that callers doing `except PiRpcError:` for command failures keep working (catches subclass via base).
1. **`bridge_write_timeout` config knob.** Default `None` preserves current "blocking forever" behavior. Confirm that this is the right default (vs. always-bounded).
1. **`max_handlers=256` default + `process_request` override.** Bounds thread creation, not just execution. Confirm acceptable.
1. **14 breaking changes in migration table.** Three new in v4 (async tool body, `PythonToolServer` async lifecycle, `EventQueueEmpty`). Confirm the scope is the right scope.
1. **Agent-class co-design** remains a separate, parallel gate. v4 does not resolve hook names or notification-vs-decision protocol shape or worker-pool dispatch for decision events.

After sign-off on items 1–9, the plan is ready for phase 1.
