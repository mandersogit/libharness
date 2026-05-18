---
status: Superseded
created: '2026-05-15'
---

# asyncio → threads rewrite plan

> **Superseded 2026-05-17 by `dev-notes/2026-05-17-v8-port-plan.md`.**
>
> The threads-rewrite direction described here was implemented as far as phase 3 on the `threads-rewrite` branch (`origin/threads-rewrite`, also available in sibling checkout `../libharness/`); during implementation, doubts surfaced about the rip-asyncio-out approach — thread inventory ballooned, lock-order complexity grew, and the sync-callback ergonomics for user code felt worse than the alternative. The v8 direction (asyncio core preserved, `Agent` class with notification + decision hooks layered on top) is the new canonical direction.
>
> Body preserved for audit trail. See `dev-notes/2026-05-17-v6-as-base-direction.md` for the direction-shift discussion and `dev-notes/2026-05-17-v8-analysis.md` for the v8 design rationale.

Implementation plan for rewriting `src/libharness/pi/` from asyncio to threads, per the concurrency-model decision recorded 2026-05-15 (`dev-notes/2026-05-15-concurrency-model-discussion.md` § Resolution). The decision is locked; this document is the *how*, not the *whether*.

**Status: Ready for implementation, blocked on Agent-class co-design.** The rewrite proper is independent of the `on_*` hook *names*, but the dispatch *mechanism* (single dispatch site, reader-thread invocation, write-back through `_send_lock`) is prepared by this plan. Before starting the rewrite, agree with the author on the `Agent` class shape so the dispatch site has a stable interface to call into. See § Hook dispatch mechanism (API-agnostic) below for what this plan does and doesn't commit to.

## Architecture overview

```text
PiPythonHarness (sync __enter__/__exit__)
  ├── PythonToolServer
  │     ├── socketserver.ThreadingTCPServer  -- listener
  │     ├── BridgeHandler (one per connection)
  │     │     ├── reads single request frame, validates token
  │     │     ├── manifest:   sync, immediate response
  │     │     └── execute:    runs tool on this handler thread,
  │     │                     emits update frames via ctx.update,
  │     │                     watches socket EOF on a sidecar thread,
  │     │                     final response frame
  │     └── thread accounting (active conns, for orderly close)
  └── PiRpcClient
        ├── subprocess.Popen(stdin=PIPE, stdout=PIPE, stderr=PIPE)
        ├── stdout reader thread  -- demuxes responses / events / UI requests
        ├── stderr reader thread  -- accumulates stderr text
        ├── stdin writes guarded by a single _send_lock
        ├── _pending: dict[id, _ResponseSlot]   (lock-protected)
        ├── _events: queue.Queue                 (already MT-safe)
        ├── event_handlers: list[Callable]       (snapshotted under lock)
        └── (future) hook-dispatch hook point in _handle_message
```

All public API methods (`harness.client.prompt`, `harness.client.get_state`, etc.) become plain `def`. The caller's main thread drives the user prompt/event loop.

## File-by-file rewrite scope

Wire protocols (pi RPC over the subprocess; loopback-TCP JSONL bridge) are unchanged. The rewrite is internal.

| File                         | Current LOC | New LOC   |
| ---------------------------- | ----------- | --------- |
| `__init__.py`                | 20          | ~20       |
| `harness.py`                 | 136         | ~120      |
| `jsonl.py`                   | 70          | 70        |
| `rpc.py`                     | 415         | ~450      |
| `server.py`                  | 191         | ~250      |
| `shim.py`                    | 305         | unchanged |
| `tools.py`                   | 368         | ~330      |
| `tests/pi/__init__.py`       | 0           | 0         |
| `tests/pi/test_jsonl.py`     | 14          | 14        |
| `tests/pi/test_tools.py`     | 44          | ~80       |
| `tests/pi/test_rpc_fake.py`  | 16          | ~30       |
| `tests/pi/test_set_model.py` | 46          | ~40       |
| `tests/pi/test_server.py`    | 49          | ~80       |
| `tests/pi/test_real_*.py`    | 38 + 77     | 38 + 77   |
| `tests/pi/fake_pi_rpc.py`    | 32          | unchanged |
| `pyproject.toml`             | 126         | ~123      |

**Change per file:**

- *`__init__.py`:* unchanged exports.
- *`harness.py`:* `__aenter__/__aexit__` → `__enter__/__exit__`; sync `server.start()` / `pi.start()`; orchestration unchanged.
- *`jsonl.py`:* unchanged. Add docstring note: `StrictJsonlDecoder` is not thread-safe, one per stream.
- *`rpc.py`:* full rewrite of async machinery; wire protocol unchanged.
- *`server.py`:* full rewrite to `ThreadingTCPServer` + custom handler.
- *`shim.py`:* TS template only; no Python concurrency.
- *`tools.py`:* drop async + async-gen branches in `collect_tool_result`; drop `asyncio.Event` in `ToolContext`; reject `async def` at registration; `ctx.update` becomes sync.
- *`tests/pi/test_tools.py`:* adds async-def-rejection + sync-streaming test.
- *`tests/pi/test_rpc_fake.py`:* rewrite as sync.
- *`tests/pi/test_set_model.py`:* rewrite as sync.
- *`tests/pi/test_server.py`:* rewrite as sync + new concurrent-tool and cancellation tests.
- *`tests/pi/test_real_*.py`:* drop async/await; same assertions.
- *`tests/pi/fake_pi_rpc.py`:* already sync (no change).
- *`pyproject.toml`:* remove `pytest-asyncio` dev dep + `asyncio_mode = "auto"` ini option.

Total: ~1100 LOC touched, of which ~600 is genuinely new logic (rpc.py + server.py + tools.py). Rest is mechanical.

## Sequenced phases (single feature branch, green only at the end)

Realistic constraint: `rpc.py` and `server.py` are independently asyncio-rooted but their consumers (`harness.py`, the tests) currently `await` them. The rewrite cannot be strictly bottom-up while keeping every commit green; bridging code to maintain per-commit greenness would be throwaway debt.

**Recommendation: four-commit feature branch, green only at the end.** The intermediate commits are buildable but `pytest -m "not live"` is not green between phases 1 and 4.

If per-commit greenness is a hard requirement: defer `tools.py`'s "drop async-handler branch" until phase 4, leaving `collect_tool_result` dual-mode during phases 1–3 (~30 LOC of throwaway code). Not recommended.

### Phase 1 — `tools.py`

Smallest surface, no I/O. Do first because `server.py` depends on it.

- `ToolContext._cancelled: asyncio.Event` → `threading.Event`.
- `ctx.update` becomes a plain sync `def update(...)`; `_update_callback` becomes a sync callable.
- `collect_tool_result` becomes sync: removes `inspect.isasyncgen` and `inspect.isawaitable`. Keeps `inspect.isgenerator` — see § Tool registry below for the decision to drop sync-gen support too.
- `ToolRegistry.register` rejects `async def` and async-gen handlers at decoration via `inspect.iscoroutinefunction` / `inspect.isasyncgenfunction`. Loud error with remediation message (not-pi-2 pattern).
- Wire shape of `ToolResult`, `ToolSpec`, `RegisteredTool` unchanged.

### Phase 2 — `server.py`

Full rewrite to `ThreadingTCPServer`. Tests in `test_server.py` rewritten. Largest single delta.

### Phase 3 — `rpc.py`

Full rewrite to `subprocess.Popen` + reader threads. Tests in `test_rpc_fake.py` and `test_set_model.py` rewritten.

### Phase 4 — `harness.py` + cleanup

- `async def start/close` → `def start/close`.
- `__aenter__/__aexit__` → `__enter__/__exit__`.
- Live tests (`test_real_pi_integration.py`, `test_real_llm.py`) rewritten as sync.
- Remove `pytest-asyncio` dev dep + `asyncio_mode = "auto"` ini option.
- Validate: `make all` + `make test-live` on both venvs.

## Design answers

### 1. Subprocess I/O (`rpc.py`)

`subprocess.Popen(..., stdin=PIPE, stdout=PIPE, stderr=PIPE, bufsize=0)`. Two daemon reader threads: `_stdout_reader`, `_stderr_reader`. Single write path; `_stdin_lock = threading.Lock()`.

The stdout reader feeds bytes into `StrictJsonlDecoder`, then for each record calls `_handle_message(record)`. **`_handle_message` runs on the reader thread** — same semantics as the asyncio version (where it ran on the event-loop thread). This preserves ordering guarantees.

`bufsize=0` matters: pi emits LF-terminated JSON lines; default block-buffering would deadlock our read loop if pi flushes line-by-line but Python buffers. Use `os.read(fd, 4096)` on `proc.stdout.fileno()` to bypass the `io` buffer layer entirely (this is what asyncio.subprocess does under the hood).

**Cleanup gotchas:**

- **Stdin close → graceful shutdown.** Close `proc.stdin`; `proc.wait(timeout=2.0)`; on timeout `proc.terminate()` (SIGTERM); on second timeout `proc.kill()` (SIGKILL).
- **Reader threads.** Daemon threads, but `.join(timeout=...)` them so stderr text isn't lost. After pi closes stdout, `os.read` returns `b""` and the reader exits.
- **`_handle_message` blocking the reader thread.** Today (asyncio) a long-running event handler stalls the loop. Same in threading: a slow sync hook blocks event delivery. Intentional — see § Hook dispatch.

### 2. Bridge server (`server.py`)

`socketserver.ThreadingTCPServer` with a custom `BaseRequestHandler`. The server subclass takes `ToolRegistry`, token, and timeout_ms in its constructor (attached as instance attrs).

**Tradeoff — `ThreadingTCPServer` vs raw thread-per-conn:**

| Concern                      | `ThreadingTCPServer`              | Raw thread-per-conn         |
| ---------------------------- | --------------------------------- | --------------------------- |
| Boilerplate                  | low (stdlib)                      | medium (manual accept loop) |
| Shutdown                     | `shutdown()` + `server_close()`   | manual flag + join          |
| Thread-leak risk on stop     | `daemon_threads = True` covers it | manual                      |
| Customization of accept loop | limited (must subclass)           | full                        |

**Pick `ThreadingTCPServer`** with `daemon_threads = True` and `allow_reuse_address = True`. Family precedent (v4 `broker.py`) uses `ThreadingHTTPServer`, same shape. `allow_reuse_address` avoids ephemeral-port TIME_WAIT on test restart.

**Update streaming under threading.** The shim opens a fresh TCP connection per `execute`, sends one request frame, then receives 0+ `update` frames followed by exactly one `response` frame *on the same connection*. The handler thread writes all frames serially to its own socket. No lock needed for that one socket.

`ctx.update(value)` becomes:

1. Normalize value to `ToolResult`.
1. Call handler-installed callback → write JSONL frame to `self.request`.
1. Return.

If pi-side aborts mid-stream, the next write raises `BrokenPipeError`; handler catches, sets `ctx._cancelled`, returns. The tool's next `ctx.update` or `ctx.cancelled` poll observes cancellation.

**Disconnect watcher.** Today the asyncio server has a `watch_disconnect` task that reads 1 byte for EOF detection. Keep the same shape under threading — one sidecar daemon thread per execute, doing `sock.recv(1)` and setting the cancelled event on EOF. Sidecar pattern (vs polling inside `ctx.update`) is preserved so cancellation fires even if the tool never calls `ctx.update`.

### 3. Request/response correlation (`rpc.py`)

**Tradeoff — `queue.Queue(maxsize=1)` vs `Event + slot`:**

| Concern                | `queue.Queue`            | `Event + slot`            |
| ---------------------- | ------------------------ | ------------------------- |
| Single response        | works (one put, one get) | works                     |
| Multiple frames per id | natural                  | needs list + lock + event |
| Timeout                | `q.get(timeout=t)`       | `event.wait(timeout=t)`   |
| Cancellation           | sentinel value           | extra event               |

Pi RPC sends exactly one `response` per request id; streaming exists only on the bridge channel, not RPC. So the correlation in `rpc.py._pending` is the single-response case.

**Pick `dict[str, queue.Queue(maxsize=1)]`.** `send()` does `q = queue.Queue(maxsize=1); self._pending[id] = q; ... return q.get(timeout=t)`. On process failure, `_fail_pending` puts a sentinel exception into every queue.

For the bridge (`server.py`): correlation is per-connection, not per-id. The handler writes frames serially. No shared `_pending` map.

### 4. Cancellation

asyncio's `task.cancel()` injects `CancelledError`. Threading has no equivalent. Cancellation in libharness is cooperative today (the tool checks `ctx.cancelled`) and stays cooperative.

Mechanism:

- `ToolContext._cancelled: threading.Event` (replaces `asyncio.Event`).
- `ctx.cancelled` property unchanged from caller's POV — it's a bool.
- Pi abort → shim closes connection → sidecar `recv` returns `b""` → sets event.
- `ctx.update` checks event before write; raises `_ToolCancelled` (internal) if set; handler maps to `success=false, error="tool execution cancelled"` response (best-effort — pi may already have disconnected).
- Tool body checks `ctx.cancelled` at convenient points; if it doesn't, it runs to completion and the result is discarded.

**Cancellation risk inventory:**

- Tool that `time.sleep(3600)` and never checks `ctx.cancelled` — handler thread leaks for an hour. Documented non-issue *for now* (no users); flag in `dev-notes` once users exist.
- Tool that catches `_ToolCancelled` and keeps going — same outcome. Don't make `_ToolCancelled` public; only `ctx.update` raises it internally.
- Sidecar race: cancellation fires *after* tool returns but *before* handler writes response. Acceptable — write either succeeds (pi ignores) or fails (we log).

### 5. Tool registry & handler shapes

Current `collect_tool_result` accepts: sync, async, sync-gen, async-gen. Strip to **sync only**.

**Tradeoff — keep sync-gen or drop:**

|                                            | Keep sync-gen                | Drop sync-gen           |
| ------------------------------------------ | ---------------------------- | ----------------------- |
| Streaming UX                               | `yield ToolResult.text(...)` | `ctx.update(...)`       |
| Mechanism count                            | 2 (yields + ctx.update)      | 1                       |
| Hot-path branches in `collect_tool_result` | +1                           | none                    |
| Precedent (v3)                             | yes                          | no                      |
| Precedent (v4)                             | no                           | yes (`ctx.update` only) |

**Drop sync-gen. Keep `ctx.update` only.** v4 (closest non-asyncio precedent) is `ctx.update`-only; current tests already use `ctx.update`; one mechanism is simpler than two. If we revisit, sync-gen is ~6 lines.

**`async def` rejection:** at decoration time, raise `ToolError` with a remediation message pointing at `dev-notes/2026-05-15-concurrency-model-discussion.md`. Not-pi-2 pattern.

### 6. Hook dispatch mechanism (API-agnostic)

The rewrite prepares for, but does not commit to, the Agent class. Shape:

```text
pi (subprocess) → stdout → reader thread → _handle_message → _dispatch_event
                                                                  ↓
                                                     _hook_dispatcher(event)
                                                                  ↓
                                                      (later: agent.on_X(event))
                                                                  ↓
                                                      result (or None)
                                                                  ↓
                                                      (later: response frame back to pi)
```

What the rewrite commits to:

1. **Single dispatch site** — `_dispatch_event` is the only place where a pi-originated event becomes a Python call. Inside, hooks are called synchronously, in order: (a) legacy `_event_handlers` list, then (b) future `_hook_dispatcher` slot (initially `None`).
1. **Which thread runs the hook** — the reader thread. Same as today (asyncio event-loop thread). Slow hooks block event delivery *intentionally*: decision events need a synchronous result for pi to advance.
1. **Result write-back path** — `_send_lock`-guarded `proc.stdin.write(dumps_line(...))`. The `_send_extension_ui_response` machinery is the existing pattern.

What this plan does NOT commit to:

- Specific hook names (`on_tool_call`, `on_agent_end`, etc.) — that's the Agent-class co-design.
- Notification-vs-decision event protocol shape — that's `dev-notes/2026-05-14-event-bridge-proposal.md`.

**Long-running hook caveat:** if a hook blocks for 10s, the reader doesn't drain pi's stdout for 10s. Pi's pipe buffer is ~64KB; a chatty agent could fill it and pi would block. Future event-bridge proposal may dispatch notification-only events to a worker queue; decision events must stay synchronous. Documented constraint, not solved in this rewrite.

### 7. Freethreading-specific opportunities

The rewrite produces code that runs on 3.11 (GIL) and 3.14t (FT) without conditionals. FT speedups are bonuses on top of identical code. Honest accounting:

**Grounded wins:**

1. **Parallel tool dispatch** when pi calls `executionMode: "parallel"` tools concurrently. Two `execute` bridge calls → two handler threads. Under 3.11 they take turns at the GIL. Under 3.14t they run truly in parallel if the tool body is CPU-bound Python. Concrete user-visible win. Harness changes nothing — the threading is already there — freethreading unlocks it.
1. **Bridge reader + RPC reader running concurrently.** Today asyncio coroutines on one loop; threaded version is two threads. Under 3.14t, while one is JSON-parsing and the other is reading bytes, they parallelize. Marginal but free.

**Speculative — do not optimize without measurement:**

- Lock-free `_event_handlers` snapshot (`list(self._event_handlers)` once before iterating is plenty).
- Per-tool `RegisteredTool` immutability for cache-line friendliness on FT. Overkill.

**Where the GIL doesn't matter:**

- Single stdin writer (serialized by `_send_lock` anyway).
- Pi subprocess (different process; not GIL-affected at all).
- User's tool body when I/O-bound (GIL released during blocking I/O on 3.11 too).

**Position: idiomatic threaded code, no micro-optimization for FT, no FT-only branches.** Document the two real wins; resist lock-free schemes that regress on 3.11.

### 8. Test suite rewrite

| Test file                              | Survives?             |
| -------------------------------------- | --------------------- |
| `tests/pi/test_jsonl.py`               | as-is                 |
| `tests/pi/test_tools.py`               | extends               |
| `tests/pi/fake_pi_rpc.py`              | unchanged             |
| `tests/pi/test_rpc_fake.py`            | rewrite-sync          |
| `tests/pi/test_set_model.py`           | rewrite-sync          |
| `tests/pi/test_server.py`              | rewrite-sync + extend |
| `tests/pi/test_real_pi_integration.py` | rewrite-sync          |
| `tests/pi/test_real_llm.py`            | rewrite-sync          |

**Change per test file:**

- *`test_jsonl.py`:* pure data tests; no rewrite.
- *`test_tools.py`:* add async-def-rejection test + sync-streaming test exercising `ctx.update`.
- *`fake_pi_rpc.py`:* already a sync subprocess script; nothing to do.
- *`test_rpc_fake.py`:* `def`, `with PiRpcClient(...) as client:`, no `await`.
- *`test_set_model.py`:* same shape as `test_rpc_fake.py` rewrite.
- *`test_server.py`:* `socket.create_connection` + `sock.makefile('rb').readline()` for frame I/O; add concurrent-tool + cancellation tests (regression for parallel handlers and pi-side abort).
- *`test_real_pi_integration.py`:* drop async/await, `with` not `async with`; same `pytest.mark.live`.
- *`test_real_llm.py`:* same shape as the integration rewrite.

`pytest-asyncio` dev dep + `asyncio_mode = "auto"` ini option dropped in phase 4 (not earlier — intermediate commits still have asyncio tests).

### 9. Cleanup gotchas (resource leak inventory)

| Resource                     | Risk                       |
| ---------------------------- | -------------------------- |
| `subprocess.Popen`           | zombie                     |
| Stdout/stderr reader threads | leak                       |
| `_pending` queues            | callers blocked in `get()` |
| Bridge listener socket       | TIME_WAIT on port          |
| Bridge listener thread       | leak                       |
| Bridge handler threads       | leak                       |
| Disconnect-watcher threads   | leak                       |
| Temp dir                     | leak                       |

**Mitigation per resource:**

- *`subprocess.Popen`:* `stdin.close()` → `wait(timeout=2)` → SIGTERM → `wait(timeout=2)` → SIGKILL → `wait()`.
- *Stdout/stderr reader threads:* `join(timeout=1)` after subprocess exits; log a warning if not joined (this should never happen since `os.read` returns `b""` on EOF).
- *`_pending` queues:* `_fail_pending(exc)` puts a sentinel exception into every queue so blocked `get()` callers unblock with an error.
- *Bridge listener socket:* `allow_reuse_address = True` avoids ephemeral-port TIME_WAIT on test restart; close socket on shutdown.
- *Bridge listener thread:* `server.shutdown()` signals the accept loop to exit; then `thread.join`.
- *Bridge handler threads:* `daemon_threads = True` guarantees process exit even on misbehaving handlers; explicit join with timeout for orderly shutdown when the harness is closed cleanly.
- *Disconnect-watcher threads:* `join(timeout=0.1)` in the handler's `finally`; tolerate the leak if the tool body is still running (threads cannot be force-killed).
- *Temp dir:* already managed by `tempfile.TemporaryDirectory.cleanup()` in `harness.close()`.

**Exception safety:** `__exit__` runs `close()` even if `start()` raised partway. Today's `__aexit__` does this unconditionally; same shape in threaded version. Add a regression test that mocks `PiRpcClient.start` to raise *after* `PythonToolServer.start` succeeds and asserts no thread leaks.

**Do not add** `if sys._is_gil_enabled()` branches at import time. The user's runtime dictates; we run identically on both.

### 10. Concrete regression tests to write

Each is a test the rewrite must pass; some have current asyncio analogues, several are new:

1. **Bridge handler leak when pi disconnects mid-stream.** Tool calls `ctx.update` 10 times; pi closes after frame 3; assert handler returns, sidecar joins, no thread leak.
1. **Subprocess SIGKILL externally.** Kill pi from outside; `client.close()` returns within bounded time; reader threads exit; pending requests fail with `PiRpcProcessError`.
1. **Pending request abandoned at close.** Send a request, never receive response, `close()` the client; caller's `send()` raises from the `_pending` sentinel.
1. **Concurrent execute requests.** Two `executionMode: "parallel"` tools; pi opens two concurrent bridge connections; both complete; results don't cross-talk.
1. **Concurrent prompt and event delivery.** Caller thread calls `client.prompt(...)` while reader thread is delivering an event; no deadlock.
1. **`async def` tool registration.** Decoration raises `ToolError` immediately, not at first call.
1. **Stale state across restart.** `harness.close()` then `harness.start()` again raises (current behavior). Add a regression test.
1. **Long-running tool ignoring cancellation.** Cancel via socket close; tool body keeps running; handler returns to pool; assert process can still exit (daemon threads). The "we accept the leak" case — test verifies shutdown isn't broken.
1. **Bridge token mismatch under racing connections.** Sync version of the existing asyncio test.
1. **Reader thread observes invalid JSON from pi.** Today `JsonlDecodeError` propagates from `_handle_message`; in threaded version, catch on reader thread, fail all pending requests, exit cleanly. Test feeds invalid JSON via fake-pi, asserts clean shutdown.

## Tradeoffs summary (decision register)

| Decision               | Picked                                |
| ---------------------- | ------------------------------------- |
| Server framework       | `ThreadingTCPServer`                  |
| Daemon threads         | yes                                   |
| `_pending` correlation | `dict[id, Queue(maxsize=1)]`          |
| Disconnect detection   | sidecar thread `recv(1)`              |
| Tool authoring         | sync + `ctx.update` only              |
| Async-def handling     | reject at decoration                  |
| Subprocess reader      | one thread per stream, daemon         |
| Stdin buffering        | `bufsize=0`, raw `os.read` for stdout |
| FT-specific code paths | none                                  |
| Test suite             | sync pytest, drop `pytest-asyncio`    |
| Phase boundaries       | 4 commits, green at end               |

**Rejected alternative + rationale per decision:**

- *Server framework:* `ThreadingTCPServer` (stdlib) vs raw thread-per-conn — picked stdlib for boilerplate savings and family precedent (v4 uses `ThreadingHTTPServer`, same shape).
- *Daemon threads:* yes vs non-daemon + explicit join — daemon guarantees process exit even on misbehaving handlers; we still join explicitly for clean shutdown.
- *`_pending` correlation:* `dict[id, Queue(maxsize=1)]` vs `Event + slot` — simpler for the one-shot case; same lock cost. Streaming-update case doesn't apply on the RPC channel (only on the bridge channel).
- *Disconnect detection:* sidecar thread `recv(1)` vs `select.select` inside `ctx.update` — sidecar fires even if the tool never calls `ctx.update`.
- *Tool authoring:* sync + `ctx.update` only vs keeping sync-gen — one streaming mechanism is simpler; v4 precedent is `ctx.update`-only. Sync-gen is ~6 lines if we revisit.
- *Async-def handling:* reject at decoration vs wrap with `asyncio.run` — not-pi-2 precedent. Loud + early failure with a remediation message; no silent shape mismatch.
- *Subprocess reader:* one thread per stream (daemon) vs single thread with `select` on both fds — simpler; stderr is rarely chatty enough for the per-stream cost to matter.
- *Stdin buffering:* `bufsize=0` + raw `os.read` on stdout fd vs default buffering — avoids the deadlock case where pi line-flushes and Python block-buffers.
- *FT-specific code paths:* none vs conditional shortcuts — keep code single-source. Resist `if sys._is_gil_enabled()` branches; we run identically on both runtimes.
- *Test suite:* sync pytest, drop `pytest-asyncio` vs hybrid (keep some async tests) — aligned with the locked concurrency decision; hybrid is debt.
- *Phase boundaries:* 4 commits, green at the end of the feature branch vs green per-commit with bridging code — bridging code is throwaway debt for ~30 LOC across 3 intermediate commits.

## Critical files (read in order)

1. `src/libharness/pi/rpc.py` (current asyncio version)
1. `src/libharness/pi/server.py`
1. `src/libharness/pi/tools.py`
1. `src/libharness/pi/harness.py`
1. `dev-notes/predecessors/v4/src/pi_python_harness/broker.py` (the threads-sync precedent we're closest to)
1. `dev-notes/2026-05-15-concurrency-model-discussion.md` § "What this implies for tool authoring and event hooks" (the API-shape reference)
1. `dev-notes/2026-05-14-event-bridge-proposal.md` (for context on what the dispatch site will eventually serve, *not* as a gating dependency)
