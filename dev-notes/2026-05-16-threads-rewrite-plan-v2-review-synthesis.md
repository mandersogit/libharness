---
status: In co-design
created: '2026-05-16'
---

# Threads-rewrite plan v2 review — synthesis

Consolidated, deduplicated findings from fourteen independent adversarial reviews of `dev-notes/2026-05-15-threads-rewrite-plan.md` (v2). Companion to the v1 synthesis at `dev-notes/2026-05-15-threads-rewrite-plan-review-synthesis.md`. Ordered by severity × corroboration count, with options and recommendations baked in so this doc can drive a single-pass v3 revision.

## Reviewer roster

Fourteen independent passes across three orchestration routes and three model lanes.

| ID     | Route         | Model           | Reasoning | Scope               | Verdict                                       |
| ------ | ------------- | --------------- | --------- | ------------------- | --------------------------------------------- |
| A1     | codex direct  | gpt-5.4         | xhigh     | generalist          | not ready; v3                                 |
| A2     | codex direct  | gpt-5.4         | xhigh     | generalist          | not ready                                     |
| A3     | codex direct  | gpt-5.4         | xhigh     | concurrency         | needs third design pass                       |
| A4     | codex direct  | gpt-5.4         | xhigh     | API / migration     | not ready as API plan                         |
| A5     | codex direct  | gpt-5.4         | xhigh     | lifecycle / cleanup | not implementation-ready                      |
| A6     | codex direct  | gpt-5.4         | xhigh     | tests / CI          | not adequate                                  |
| B1     | codex direct  | gpt-5.5         | xhigh     | generalist          | not ready; Tier 1 all need tightening         |
| B2     | codex direct  | gpt-5.5         | xhigh     | generalist          | not ready                                     |
| B3     | codex direct  | gpt-5.5         | xhigh     | concurrency         | ready with amendments, no v3 round needed     |
| B4     | codex direct  | gpt-5.5         | xhigh     | API / migration     | not ready as API plan                         |
| B5     | codex direct  | gpt-5.5         | xhigh     | lifecycle / cleanup | not implementation-ready                      |
| B6     | codex direct  | gpt-5.5         | xhigh     | tests / CI          | not adequate                                  |
| C1 (E) | Opus subagent | claude-opus-4-7 | n/a       | generalist          | ready with surgical fixes, no v3 round needed |
| C2 (F) | Opus subagent | claude-opus-4-7 | n/a       | generalist          | ready with the flagged changes (F1/F2/F6)     |

**Consensus verdict.** 12 of 14 say "not ready as written, amendments before phase 1." 2 of 14 (C1 Opus E and B3 codex-gpt-5.5 S1) say "amendments are surgical, no third pass needed." Nobody says "ready as-is."

**Detail:**

- *gpt-5.4 round (A1–A6):* 30 distinct findings, ~12 marked CRITICAL/HIGH. Strong on the dispatch deadlock, Future race, and registry-freeze rollback angles. Test framework gaps surfaced most thoroughly by A6.
- *gpt-5.5 round (B1–B6):* 47 distinct findings, ~19 CRITICAL/HIGH. Notably more findings per reviewer (+30%) and more cross-cutting analysis (B3 weaves fatal/pending/Future races; B5 connects `close()` deadlock to reader-blocked state). User's claim that "5.5 is significantly smarter" is borne out by the data: same big-ticket items, plus B5 / B6's broader coverage of lifecycle and test-infrastructure gaps.
- *Opus round (C1 + C2):* both flagged the helper-thread bypass on T1.1 — the only Opus-exclusive finding (helper threads spawned by handlers bypass the `threading.local` guard). C2 added the watchdog-on-hook-duration recommendation (F6) and the `max(1.0, ...)` floor regression (F2). C1 had the sharpest single catch — the sidecar spurious-cancel from T1.3's broad `settimeout`.

## Tier 1 — v1 GATE findings v2 was supposed to close

Spoiler: 3 of 4 are not closed by v2. Only T1.2(b) Future-vs-Queue gets a clean pass; the other three need significant rework.

### T1.1 — Dispatch contract: under-fixed in two ways

**Corroboration:** 7 of 14 reviewers flagged the UI-handler hole (CRITICAL): A1.1, A2.2, A3.2, B1.1, B2.1, B2.2, B3.3. 4 of 14 flagged the helper-thread bypass (MODERATE): A3.5, B3.4, C1, C2.F1. Total: 9 of 14 reviewers cited at least one T1.1 hole.

**The bugs:**

- *(a) UI-handler path is not covered by `_in_dispatch`.* v2's design sketch wraps only `_dispatch_event`; the UI-handler invocation in `_handle_extension_ui_request` is a separate code path on the same reader thread. A sync UI handler calling `client.send()` has the **same** deadlock the guard was supposed to prevent.
- *(b) `threading.local()` is per-thread.* A handler that spawns a worker thread (e.g. `ThreadPoolExecutor.submit`) whose body calls `client.send()` and then joins on the result: the worker has no `_in_dispatch.value`, `send()` proceeds, blocks on its response — but the response can only be drained by the reader thread which is still blocked inside the handler waiting for the worker. Same deadlock, no detection.
- *(c) T3.1 UI response priority is not actually pinned by `_send_lock`.* The plan claimed UI-event-dispatch + UI-response-write are "atomically serialized on the reader thread under `_send_lock`," but the dispatch happens *before* `_send_lock` is acquired. Another caller thread can grab `_send_lock` and write a `prompt` request between the reader's UI-event dispatch and the reader's UI-response write. The fake-pi contract (`tests/pi/fake_pi_rpc.py:18`) requires the very next stdin line after `extension_ui_request` to be `extension_ui_response`. v2's design does not guarantee that.

**Options for (a) + (c):**

- *Single reader-thread "UI critical section"* — `_in_dispatch` is set across both `_dispatch_event` and `_handle_extension_ui_request`; `_send_lock` is held by the reader thread for the entire UI request → event dispatch → UI response sequence, blocking any caller-thread writer from interleaving. **Simple but holds `_send_lock` for arbitrary user-code duration — slow UI handlers stall every other writer.**
- *Stdin write-priority lane* — separate writer thread with a priority queue; UI responses preempt normal requests. **More machinery; clean wire-ordering guarantee; doesn't stall normal writers when UI handler is slow but the response is short.**
- *Reservation pattern* — a `Condition` variable: when `extension_ui_request` arrives, the reader sets a "UI response pending" flag; ordinary `send()` waits on the condition until the flag is cleared. **Middle complexity; reader still owns the UI response write.**

**Options for (b):**

- *Document the narrower contract* — `ReentrantRPCError` catches the same-thread case; the docstring of `ReentrantRPCError` and `docs/DESIGN.md` say "handlers must not synchronously wait on any work that touches the client, regardless of which thread the work runs on." Cheap; relies on user discipline.
- *Process-wide dispatch-depth counter under a lock* — `_dispatch_depth: int` under `_handlers_lock`, incremented on dispatch entry, decremented on exit. `send()` checks if `_dispatch_depth > 0` and the call comes from any thread spawned during dispatch. **Hard to implement correctly (which threads count as "spawned during dispatch"?); footgun risk.**
- *Move dispatch off reader thread entirely (option (c) from the v1 synthesis)* — naturally falls out of the event-bridge worker-pool work. **Defers to event-bridge co-design.**

**Recommendation:**

- For (a) + (c): **reservation pattern with reader-owned UI response write**. Conceptually simplest, doesn't require a separate writer thread, doesn't stall normal writers needlessly. Add a `_ui_response_pending: threading.Event` + condition. Reader sets it before dispatching the UI event; clears it after writing the UI response. `send()` waits on the condition (with timeout) if set.
- For (b): **document the narrower contract**. Phase 1 has no decision events yet; helper-thread reentrancy is a forward concern that the event-bridge proposal (with proper worker-pool dispatch) handles natively.

Test additions: UI handler that calls `client.get_state()` → `ReentrantRPCError`. Stalled UI handler + racing `prompt()` from another thread → fake-pi never sees `prompt` before `extension_ui_response`. Helper-thread bypass → documented as "won't detect; will deadlock; expected behavior" with explicit comment.

### T1.2 — Future correlation: shape correct, race details wrong

**Corroboration:** 5 of 14 reviewers flagged Future settlement races: A2.3, A3.3, B1.4, B2.4, B3.2. 2 of 14 flagged `_fatal_error` / `_pending` insertion race separately: A3.1, B3.1. Total: 7 of 14.

**The bugs:**

- *Future settlement is not idempotent.* The v2 prose says "`Future.set_result/set_exception` has single-assignment semantics so `_fail_pending` racing a late real response does not wedge." That is wrong. `concurrent.futures.Future.set_result()` after `set_exception()` raises `InvalidStateError`. The current asyncio code explicitly guards `if not future.done()`. v2 needs the same.
- *`_fatal_error` check and `_pending` insertion are not atomic.* `send()` does (1) check `_fatal_error`, (2) acquire `_pending_lock`, (3) insert future, (4) write request. Between (1) and (3), the reader thread can die and `_fail_pending` can snapshot the pending set — the new future is inserted after the snapshot, so it never gets failed, and the caller blocks until request timeout.
- *Response path doesn't pop ownership before settling.* If response handler does `if future and not future.done(): future.set_result(...)` without `pop()`-under-lock, a slow reader can still racey-settle a Future that `_fail_pending` already failed.

**Recommendation:**

- Centralize completion in a `_settle_future(future, result_or_exception)` helper: `pop` from `_pending` under `_pending_lock`, then `try: future.set_X(...) except InvalidStateError: pass`. Use everywhere — `_handle_message`, `_fail_pending`, `close()`, future cancellation.
- Make `_fatal_error` check + `_pending` insertion one critical section: `send()` acquires `_pending_lock`, checks `_fatal_error`, raises immediately if set, otherwise inserts future, releases lock, writes request. Symmetric: `_set_fatal()` acquires `_pending_lock`, sets `_fatal_error`, snapshots pending under lock, clears `_pending`, releases lock, then iterates settling each Future.

Test additions: barrier-driven test where `_set_fatal` fires between `send`'s fatal-check and pending-insert; caller raises immediately. Close-during-pending + late-response stress; reader thread stays healthy, no `InvalidStateError`.

### T1.3 — `request.settimeout` is over-scoped

**Corroboration:** 7 of 14 reviewers flagged this: A1.2, A2.1, B1.2, B2.3, B3.6, C1 T1.3, C2.F2.

**The bug:**

`self.request.settimeout(timeout_ms/1000)` at handler entry sets a **socket-wide** timeout. Python `socket.settimeout()` applies to **all subsequent `recv` and `sendall`** on the socket, not just the initial-frame read. This causes three problems:

1. Sidecar disconnect-watcher's `recv(1)` (supposed to block indefinitely until EOF) raises `socket.timeout` every `timeout_ms` and falls through to the `finally: cancelled.set()` path, **spuriously cancelling every tool** every `timeout_ms`.
1. `ctx.update`'s `sendall(...)` can now raise `socket.timeout`, contradicting the plan's stated "synchronous; blocks until bridge write returns; no timeout" contract for `ctx.update` (T3.5).
1. Drops the current asyncio code's `max(1.0, timeout_ms/1000.0)` floor, allowing micro-timeouts to cause spurious CI failures.

**Recommendation:**

Apply the deadline only to the initial-frame read. Two ways:

- *Pre-frame `select.select()`* on the request socket with `timeout_ms` deadline; once the frame is read, no socket-level timeout is set; sidecar `recv(1)` blocks indefinitely; `sendall` blocks indefinitely.
- *`request.settimeout(timeout) → initial read → request.settimeout(None)`* — restores blocking mode for everything after.

Restore the `max(1.0, ...)` floor: `request.settimeout(max(1.0, timeout_ms/1000.0))`.

Test additions: tool runs longer than `timeout_ms` without disconnecting → no spurious cancellation. Slow-pi-consumer test → `ctx.update` blocks past `timeout_ms` rather than raising `socket.timeout`. Tiny `timeout_ms` (10ms) input → still gets the 1-second floor.

### T1.4 — Half-duplex contract: pinned in docs, not enforced in code

**Corroboration:** 2 explicit (C1 T1.4, B1.6), plus the broader "sidecar passively swallows whatever comes" concern echoed in A1.1, A3.2, B1.1, B2.1, B2.2 (under the UI-ordering cluster).

**The bug:**

v2 documents the half-duplex contract in `docs/DESIGN.md` (good), and says the regression test sends "an extra byte before close" and the handler "returns with a `protocol_violation` error." But the v2 design doesn't specify how the sidecar code distinguishes a non-empty byte (protocol violation, log + cancel + flag in response) from `b""` (clean EOF). Without that distinction, the sidecar's bare `except` swallows the extra byte and the test described is unverifiable.

Plus: even with the distinction, B1.6 notes the sidecar can't preempt an uncooperative tool body. The "handler returns with `protocol_violation` error" is only reliable for cooperative tools.

**Recommendation:**

Specify the sidecar code path:

```python
def _watch_disconnect(self, ctx: ToolContext) -> None:
    try:
        data = self.request.recv(1)
        if data == b"":
            ctx._cancelled.set()
        else:
            ctx._cancelled.set()
            ctx._protocol_violation = True
            logger.warning("bridge protocol violation: received %r after request frame", data)
    except OSError:
        ctx._cancelled.set()
```

Document that protocol-violation handling is best-effort for uncooperative tools (same caveat as T2.6 cancellation honesty). Test the cooperative case; document the uncooperative case as a known limitation.

## Tier 2 — Significant v2-introduced or under-fixed problems

### T2.X — `close()` shutdown can deadlock behind `_send_lock`

**Corroboration:** 4 of 14, all rated HIGH/CRITICAL: A5.1, A5.2, B5.3, B5.4.

The plan's `close()` sequence: acquire `_send_lock` → set `_stdin_closed = True` → `proc.stdin.close()` → wait for reader EOF → join reader → `proc.wait()` → SIGTERM/SIGKILL. **But `_send_lock` is also held by `_send()` while writing/flushing.** If the child has stopped reading stdin (e.g. wedged child, full pipe), a writer blocks indefinitely holding `_send_lock`, and `close()` can never even mark stdin closed.

Compounding: v2 relies on "reader observes EOF on its own" to trigger `_fail_pending`. **But the reader is not in `read()` if it's executing a slow user hook.** EOF observation is deferred until user code returns. Pending RPC callers sit in `future.result(timeout=...)` until request timeout, not until `close()` returns.

**Recommendation:**

Separate "closing state" from the write lock:

- Add `_closing: threading.Event`. `close()` sets it *without* acquiring `_send_lock`. Writers check `_closing.is_set()` before acquiring the lock and return early. (The lock is still needed for the write-ordering invariant T2.9 while we *are* writing; it just doesn't gate the close path.)
- Make `close()` itself transition to terminal error state and call `_fail_pending(PiRpcProcessError("client closed"))` directly, before joining reader threads or waiting on the subprocess.
- `close()` bounded path: bounded `_send_lock` acquisition (1 sec) → if not acquired, skip clean stdin close and go straight to SIGTERM.

Test additions: subprocess that stops reading stdin + concurrent writer + `close()` → returns within bounded time. Event handler blocks forever + pending request + `close()` → pending caller fails immediately rather than after `request_timeout`.

### T2.X — Lifecycle state machine not specified

**Corroboration:** 4 of 14: A5.4, B3.7, B5.1, B5.2.

v2 repeatedly says `close()` is "strictly idempotent" and restart is "unsupported" but never specifies the synchronization that enforces these properties under threads. Two concurrent `close()` calls can race across subprocess teardown, server shutdown, tempdir cleanup. A concurrent `start()` can race with a `close()` already in progress.

Plus: B5.2 — the partial-start rollback test only covers `PiRpcClient.start()` failing. `harness.start()` has at least 5 failure points (server bind, tempdir, shim write, faux provider, `PiRpcClient.start`). Single test doesn't prove rollback works at any other cut.

**Recommendation:**

Add an explicit `_lifecycle_state: Literal["new", "starting", "started", "closing", "closed", "failed"]` guarded by `_lifecycle_lock`. `start()` transitions new → starting → started; `close()` transitions started → closing → closed (or failed → closed). Second `close()` from another thread waits on a `_close_complete: threading.Event`. Restart attempt from a `closed` state raises `RuntimeError("harness cannot be restarted; create a new instance")`.

Parametrize the partial-start test over each failure injection point.

### T2.X — `ToolRegistry.freeze()` leaks into caller's registry

**Corroboration:** 2 of 14: A5.3, B5.8.

`PythonToolServer.start()` calls `registry.freeze()` on the caller-supplied registry. If `harness.start()` later fails, `close()` can clean up everything else (socket, tempdir, process), but it cannot **unfreeze the caller's registry**. This is a real partial-state leak outside the harness object — the registry is user-owned and may be reused.

**Recommendation:**

Freeze a **private snapshot** inside `PythonToolServer` instead of mutating the caller's registry:

```python
class PythonToolServer:
    def start(self, registry: ToolRegistry) -> None:
        self._frozen_registry = registry.snapshot()  # immutable copy
        # ... rest of start ...
```

Caller's `ToolRegistry` is untouched. Document this contract in `docs/DESIGN.md`.

### T2.X — Handler registries (`_event_handlers`, UI handler dict) not thread-safe under FT

**Corroboration:** 3 of 14: A3.4, B2.5, B3.5.

The v2 architecture diagram says `_event_handlers` is "snapshotted under lock" but the prose never names the lock and the dispatch pseudocode does `list(self._event_handlers)` lock-free. `_ui_handlers` dict has no synchronization at all. Public methods `on_event()`, `set_extension_ui_handler()` mutate these from caller threads while the reader thread reads them. On 3.14t this is the same shared-mutable-state class the rewrite was supposed to fix.

**Recommendation:**

Add `_handlers_lock = threading.Lock()`. Guard `_event_handlers` list mutations and snapshots; `_ui_handlers` dict mutations and lookups. Snapshot under lock, release, then iterate handlers (so handlers themselves can register/unregister without deadlock). Stress test on 3.14t: concurrent register/unsubscribe while fake-pi emits events.

### T2.X — Sync-generator wrapper bypass

**Corroboration:** 4 of 14: A1.4, A4.2, B1.5, B4.4.

v2 rejects `inspect.isgeneratorfunction` at registration (T2.4). The runtime belt-and-braces check in `collect_tool_result` (T2.3) only rejects awaitables and async generators. A sync wrapper or lambda returning a generator object — `def f(): return (x for x in xs)` — bypasses both checks and falls through to `normalize_tool_value()`, stringifying as `"<generator object ...>"`. Same bogus-result class T2.3 fixed for coroutines.

**Recommendation:**

Extend the runtime guard:

```python
def collect_tool_result(value: Any, ctx: ToolContext) -> ToolResult:
    if inspect.isawaitable(value) or inspect.isasyncgen(value) or inspect.isgenerator(value):
        raise ToolError("sync tool returned an awaitable/async-gen/generator; ...")
    return normalize_tool_value(value)
```

Test: a sync lambda returning a generator object raises at execute time.

### T2.X — Event/UI handler callback rejection misses descriptor-bypass class

**Corroboration:** 2 of 14: A4.1, B4.3.

The v2 T2.2 fix only rejects coroutine event/UI handlers at registration via `iscoroutinefunction`. Same descriptor-bypass class as T2.3 for tools: callable instances with async `__call__`, sync wrappers returning `asyncio.Future`, raw `staticmethod` descriptors. The dispatch sketch calls `h(event)` and ignores the return.

**Recommendation:**

Give event/UI handlers the same belt-and-braces shape as tools. Decoration-time `iscoroutinefunction` check, plus dispatch-time `inspect.isawaitable(result)` rejection:

```python
def _dispatch_event(self, event: dict) -> None:
    with self._handlers_lock:
        handlers = list(self._event_handlers)
    self._in_dispatch.value = True
    try:
        for h in handlers:
            try:
                result = h(event)
                if inspect.isawaitable(result) or inspect.isasyncgen(result):
                    raise TypeError(f"event handler {h!r} returned awaitable; ...")
            except Exception:
                logger.exception("event handler raised")
    finally:
        self._in_dispatch.value = False
```

Test: callable instance with async `__call__` raises; sync wrapper returning `asyncio.Future` raises; raw `staticmethod` of `async def` raises.

### T2.X — Public API missing `ReentrantRPCError` and `PiRpcProcessError` exports

**Corroboration:** 2 of 14: A4.3, B4.2.

v2 introduces `ReentrantRPCError` as a new user-observable exception but says `__init__.py` exports stay unchanged. Same pre-existing gap with `PiRpcProcessError` — referenced in `docs/DESIGN.md` failure modes but not exported from `libharness.pi`. External callers can't catch these from the top-level namespace.

**Recommendation:**

Add to `src/libharness/pi/__init__.py`:

```python
from .rpc import (
    PiRpcClient,
    PiRpcError,
    PiRpcProcessError,
    ReentrantRPCError,
)
```

Document the exception hierarchy in `docs/DESIGN.md` § Failure modes. Pick a base class (`PiRpcError`?) and have `PiRpcProcessError` and `ReentrantRPCError` both subclass it for top-level `except PiRpcError:` to catch all.

### T2.X — Migration accounting incomplete

**Corroboration:** 3 of 14: A4.4, B4.7, B4.8.

v2's "What needs author sign-off" enumerates one breaking change (async event handlers). The actual list of breaking changes is much larger:

1. All `async def` → sync `def` on the public client API (`prompt`, `get_state`, `send`, etc.)
1. `async with PiPythonHarness` → `with PiPythonHarness`
1. `async with PiRpcClient` → `with PiRpcClient`
1. `await ctx.update(...)` → `ctx.update(...)` (now blocking)
1. Async event handlers rejected at registration
1. Async UI handlers rejected at registration
1. Sync-generator tools rejected at registration
1. Restart explicitly raises `RuntimeError`
1. Late `registry.register()` after server start raises
1. Reentrant `client.send()` from inside dispatch raises `ReentrantRPCError`
1. `_events` queue is now bounded (drops oldest on overflow)
1. `ctx.update` is now synchronous and blocking with no timeout

Also: `CLAUDE.md` still says "Async-first: prefer asyncio over threads" — direct contradiction of the locked concurrency decision. v2's `docs/DESIGN.md` migration scope misses this and `dev-notes/predecessors/v3/docs/runbook.md`'s generator pattern teaching.

**Recommendation:**

Add a dedicated **§ Breaking changes and migration** to `docs/DESIGN.md` with a table: old behavior, new behavior, exception raised, migration action. Update `CLAUDE.md` § Conventions: drop "Async-first." Mark predecessor docs as historical/stale-contract.

### T2.X — `ctx.update` timeout contract internally inconsistent

**Corroboration:** 1 explicit (B4.5), plus the broader T1.3 cluster (8 reviewers) which all note the contradiction.

v2 says `ctx.update` is "synchronous; blocks until the bridge write returns; no timeout" (T3.5). But the handler entry calls `request.settimeout(timeout_ms/1000)`, which applies to `sendall(...)` too. So `ctx.update` *can* time out via `socket.timeout`. The two statements contradict.

This intersects with T1.3 (above). Fixing T1.3 (limit timeout to initial-frame read) also fixes this. Alternative: explicitly redefine `ctx.update` as "blocks until the frame is accepted or bridge write timeout elapses; timeout maps to cancellation/tool error." Either way the plan must commit to one.

### T2.X — `Semaphore(16)` doesn't bound thread creation

**Corroboration:** 2 of 14: A1.5, C1.N4.

`ThreadingTCPServer` creates a handler thread per accepted connection *before* `handle()` can fail fast on the semaphore. The semaphore limits work inside `handle()`, not socket accept or thread churn under flood. The planned slowloris regression test would still pass even if the server briefly spawns 100 threads — it proves rejection semantics, not the claimed resource bound.

**Recommendation:**

Two options. Either narrow the wording in the plan to "limits concurrent execution after accept, not thread spawn" (and accept that 100 threads briefly spawn under flood), OR override `process_request` / `verify_request` on the `ThreadingTCPServer` subclass to reject before thread spawn (cleaner, more code).

Default `max_handlers=16` is also too tight per C1.N4 — current asyncio has no cap, and 16 means a 32-tool parallel batch errors silently. Either justify against an empirical workload or default to 256 with `max_handlers=` overridable.

## Tier 3 — Test infrastructure gaps

Both S4 reviewers (A6 and B6) and several others flagged the test plan as inadequate. Combined finding list:

### `fake_pi_rpc.py` cannot support several planned tests

**Corroboration:** A6.1, B6.2.

Current fake-pi handles `get_state`, `prompt`, `set_model` only with immediate well-formed behavior. Doesn't drive: parallel tools (test #4), bridge socket clients (#14, #15, #25), 5000-event flood (#7), UI ordering races (#23). The plan marks `fake_pi_rpc.py` "unchanged" — wrong.

**Recommendation:** mode-driven fake RPC + dedicated bridge-client helpers. Plan should name the helper per test in § 10.

### Phase-4 commit-time guards use wrong commands

**Corroboration:** A6.4, B6.4.

`pytest --collect-only -q | wc -l` counts presentation lines (tree branches + summary), not test nodeids. Drift-prone.

`grep -rq 'async def test_' tests/` misses async fixtures, helpers, split-line formatting, stale `@pytest.mark.asyncio`.

**Recommendation:**

- Use `pytest --collect-only -qq | grep -c '::'` for test count, OR `pytest --collect-only --json-report` parsing.
- Add AST-based check for async test functions/fixtures and stray `pytest.mark.asyncio`.
- Run the actual non-live suite after dropping `pytest-asyncio` (not just collect-only).

### Bisect-baseline mechanism manual, uninitialized, unenforced

**Corroboration:** A6.5, B6.5.

The plan says each phase commit appends count + signature hash to `dev-notes/rewrite-bisect-baseline.md` but never defines: file format, initial baseline row, how each commit proves it updated the file, what CI/pre-commit enforces it.

**Recommendation:**

- Define machine-readable format: one line per commit, `<commit-sha>\t<test-count>\t<sha256-of-sorted-nodeids>`.
- Initial baseline = current main HEAD count and signature, committed as the first row.
- Add a `make threads-rewrite-phase4-guard` Makefile target that asserts HEAD's count/signature matches the last row.
- Pre-commit hook or CI step runs the guard.

### Runtime-meta test is not 3.11-safe

**Corroboration:** A6.6, B6.6.

`sys._is_gil_enabled()` doesn't exist on 3.11. Plan's test would `AttributeError` on the 3.11 lane.

**Recommendation:**

```python
def test_runtime_is_what_we_think() -> None:
    ft = getattr(sys, "_is_gil_enabled", lambda: True)()
    # `_is_gil_enabled` returns False on 3.14t freethreaded; True (or absent) elsewhere
    ...
```

Write artifact to a known path (`.pytest-runtime-meta/<runtime>.json`); CI step compares the two runtime outputs.

### Hang-canary tests need bounded execution

**Corroboration:** A6.7, B6.3.

Reentrancy + cancellation tests can hang the whole suite if the implementation has a bug. Repo has no `pytest-timeout` dependency.

**Recommendation:**

- Add `pytest-timeout` to dev deps.
- Use `subprocess.run([sys.executable, "-c", script], timeout=...)` for daemon-shutdown tests (out-of-process).
- Add `@pytest.mark.timeout(N)` to all hang-canary tests.

### Tier 1 coverage incomplete

**Corroboration:** A6.2, B6.1.

T1.2 — the synthesis matrix called for "N caller threads, arbitrary response order, concurrent close, no hangs." The concrete test list (#3) only covers "pending request abandoned at close" — simpler. T3.1 — the matrix called for "fake-pi never sees `prompt` line before `extension_ui_response`." The concrete test (#23) only checks local event-before-response ordering.

T2.2 (reject async event/UI handlers) has no concrete test entry at all.

F.9 (cancellation observability log), F.11 (baseline-append enforcement), F.12 (dual-delivery contract verification) also have no concrete tests.

**Recommendation:**

Expand the test list to cover every Tier-1 finding completely. Specifically add:

- N-caller multi-thread stress on `_pending` with concurrent close.
- Stalled UI handler + racing `prompt()` → wire-order assertion at fake-pi boundary.
- `on_event(async def …)` and `set_extension_ui_handler(…, async def …)` both raise immediately.
- Dual-delivery contract: `wait_for_event` + `on_event` both observe the same event.

### Slowloris test over-sized for CI

**Corroboration:** A6.7, B6.9.

Opening 100 sockets tests OS limits as much as harness behavior. Brittle.

**Recommendation:** make `max_handlers` configurable in the test, set it to 4, open `max_handlers + 2` connections, assert rejection plus recovery after closing one. Tests the semaphore contract without exhausting ports.

### Wire-shape test level unclear

**Corroboration:** B6.10.

Test #18 (`ToolSpec.to_manifest()` optional fields) is a unit assertion. Test #19 (`ToolResult` non-default fields "assert shim observes them") requires bridge integration. Plan is ambiguous.

**Recommendation:** specify two levels: unit test for serialization (#18) plus a single bridge/shim round-trip for `ToolResult` (#19).

## Tier 4 — Polish (single-reviewer, MINOR)

- *C2.F6 — pi-buffer-fill cascade.* v2 keeps option (a) and acknowledges the buffer-fill risk but ships no watchdog. Add a `threading.Timer(threshold, log_warning)` around hook invocation in `_dispatch_event`. One log line on stall.
- *B5.9 — `_events` overflow logging unbounded.* Drop-oldest logging on every drop lets chatty pi turn bounded memory into unbounded log volume. Rate-limit warnings (first drop + every 1024 drops + once per second).
- *A6.10, B6.10 — duplicate registry-freeze coverage.* Plan tests `ToolRegistry.freeze()` in both `test_tools.py` and `test_server.py`. Pick one layer.
- *B3.8 — process-exit cleanliness test must be out-of-process.* `subprocess.run([sys.executable, "-c", script], timeout=...)`, not in-process or forked-multiprocessing.
- *C1.N5 — `threading.local` is a footgun for future worker pools.* Pool-reused threads keep stale `_in_dispatch.value`. Forward-looking; flag for event-bridge work.
- *C2.F8 — dual-delivery + drop-oldest produces divergent histories.* A `wait_for_event` consumer (queue, drops on overflow) and an `on_event` handler (always fires) see divergent event histories. Document in `docs/DESIGN.md`.
- *B5.7 — disconnect watcher needs explicit exception-safe finalization.* Current asyncio watcher catches exceptions and sets `cancelled` in `finally`; v2 thread plan doesn't. Add `try/except OSError: finally: ctx._cancelled.set()`.

## gpt-5.4 vs gpt-5.5 quality comparison

User's claim "5.5 is significantly smarter" is borne out by the data. Comparison:

- **Volume.** gpt-5.4 round produced ~30 distinct findings; gpt-5.5 round produced ~47. About 30–55% more depth per reviewer for the same prompt and same project.
- **Cross-cutting analysis.** gpt-5.5 was sharper at weaving findings together. B3 connected fatal/pending atomicity, Future settlement race, and handler registry locking into one coherent "RPC correlation lifecycle" critique. B5 connected `close()` deadlock to reader-blocked state and to bridge shutdown semantics into one "shutdown safety" critique. The gpt-5.4 round caught the same individual bugs but treated them as independent items.
- **Severity calibration.** gpt-5.5 was more willing to mark findings CRITICAL where gpt-5.4 hedged at MODERATE. Specifically: B3 marked 5 findings CRITICAL where A3 marked 3 — and the Tier 1 cluster analysis above confirms the 5.5 calls were correct.
- **Verdict consistency.** Both rounds reached the same overall verdict (12 of 12 "not ready"). gpt-5.5 reached it through more specific evidence; gpt-5.4 through narrower individual findings.
- **Opus complement.** Both Opus reviews surfaced the helper-thread bypass that no codex reviewer found. C2 added the watchdog + floor-drop findings that no codex reviewer found. Opus E and F were not redundant with each other — different cross-cuts.

**Practical implication:** gpt-5.5 is the new default for adversarial review work in libharness. gpt-5.4 still produces useful findings but at lower density. Opus subagents continue to complement codex with their own finding angles.

## Recommendations matrix

Decisive recommendations per consensus finding. Format mirrors the v1 synthesis matrix: bold ID + decisive action + (Lands).

### Tier 1 — Must be in v3 plan text before coding (GATE)

- **T1.1-a (UI handler hole + ordering)** *(Lands: v3 plan + P3)* — Implement reservation pattern: reader sets `_ui_response_pending: threading.Event` before dispatching UI event; clears after writing UI response. `send()` waits on the condition (with timeout) if set. Wraps `_in_dispatch` around both `_dispatch_event` *and* `_handle_extension_ui_request`. Sync UI handler calling `client.send()` raises `ReentrantRPCError`; stalled UI handler + racing `prompt()` → fake-pi sees UI response before prompt.
- **T1.1-b (helper-thread bypass)** *(Lands: v3 plan + Docs)* — Document narrower contract in `ReentrantRPCError` message and `docs/DESIGN.md`: "handlers must not synchronously wait on any work that touches the client, regardless of thread." Defer process-wide depth counter to event-bridge work.
- **T1.2-a (Future settlement race)** *(Lands: v3 plan + P3)* — Centralize via `_settle_future(future, result_or_exc)` helper that `pop`s from `_pending` under `_pending_lock`, then `try: future.set_X(...) except InvalidStateError: pass`. Use everywhere.
- **T1.2-b (`_fatal_error`/`_pending` atomicity)** *(Lands: v3 plan + P3)* — `send()` acquires `_pending_lock`, checks `_fatal_error`, inserts future, releases lock, writes. `_set_fatal()` acquires lock, sets fatal, snapshots+clears pending, releases lock, settles all Futures.
- **T1.3 (settimeout over-scope + floor drop)** *(Lands: v3 plan + P2)* — Scope timeout to initial-frame read only: `request.settimeout(max(1.0, timeout_ms/1000)) → read → request.settimeout(None)`. Sidecar `recv(1)` and `sendall` are blocking again.
- **T1.4 (half-duplex enforcement)** *(Lands: v3 plan + P2 + Docs)* — Specify sidecar code path that distinguishes non-empty byte from `b""`. Document as best-effort for uncooperative tools.

### Tier 2 — Should land in v3 plan (significant)

- **`close()` shutdown** — Separate `_closing: threading.Event` from `_send_lock`. `close()` calls `_fail_pending` directly, doesn't depend on reader. *(P0 + P3)*
- **Lifecycle state machine** — Add `_lifecycle_state` + `_lifecycle_lock` to harness/client. Parametrize partial-start test over every cut point. *(P0 + P4)*
- **Registry freeze leak** — Freeze a private snapshot inside `PythonToolServer`, not caller's registry. *(P0 + P2)*
- **Handler registry sync** — Add `_handlers_lock`. Guard event-handler list and UI-handler dict mutations + snapshots. *(P0 + P3)*
- **Sync-gen wrapper bypass** — Extend runtime guard: also reject `inspect.isgenerator(result)`. *(P0 + P1)*
- **Event/UI handler descriptor-bypass** — Belt-and-braces: decoration-time + runtime `isawaitable(result)` for event/UI handlers too. *(P0 + P3)*
- **Public API exports** — Export `ReentrantRPCError` + `PiRpcProcessError` from `__init__.py`. Document exception hierarchy. *(P0 + Docs)*
- **Migration accounting** — Add § "Breaking changes and migration" table to `docs/DESIGN.md`. Drop "Async-first" from `CLAUDE.md`. Mark predecessor docs as historical. *(Docs)*
- **`ctx.update` timeout contract** — Resolves via T1.3 fix; pin "no timeout" wording. *(P0 + Docs)*
- **`Semaphore(16)` bound** — Narrow wording ("limits execution, not thread spawn") OR override `process_request`; default `max_handlers` to 256, not 16. *(P0 + P2)*

### Tier 3 — Test infrastructure

- **fake-pi extension** — Mode-driven fake RPC + dedicated bridge-client helpers; name per test in plan § 10. *(P0 + tests)*
- **Phase-4 commit guards** — `pytest --collect-only -qq | grep -c '::'` for count; AST check for async tests/fixtures. *(P0 + P4)*
- **Bisect-baseline** — Machine-readable format; initial row; `make threads-rewrite-phase4-guard` target; CI/pre-commit enforces. *(P0 + Makefile)*
- **Runtime-meta 3.11 safety** — `getattr(sys, "_is_gil_enabled", lambda: True)()`; named artifact path. *(P0 + P1)*
- **Hang-canary bounds** — Add `pytest-timeout` dev dep; `@pytest.mark.timeout(N)` on hang-canary tests; subprocess for daemon-shutdown. *(P0 + pyproject)*
- **Tier 1 test coverage** — Add explicit tests for T1.2 multi-thread, T3.1 wire-order, T2.2 async-handler-rejection, F.12 dual-delivery. *(P0 + tests)*
- **Slowloris CI-safety** — `max_handlers=4` for test, `max_handlers + 2` connections. *(P0 + tests)*
- **Wire-shape test levels** — Specify unit vs integration per test. *(P0 + tests)*

### Tier 4 — Polish

- **Watchdog on hook duration** — `threading.Timer(threshold, log_warning)` around `_dispatch_event`. *(P3)*
- **`_events` overflow log rate limit** — First + every 1024 + once-per-second. *(P3)*
- **Out-of-process daemon-shutdown test** — `subprocess.run(...)`, not in-process. *(P4)*
- **Disconnect watcher exception handling** — `try/except OSError: finally: cancelled.set()`. *(P2)*
- **Dual-delivery + drop-oldest divergence doc** — One sentence in `docs/DESIGN.md`. *(Docs)*

## Verdict and path forward

**v3 is required.** Twelve of fourteen reviewers say v2 is not ready as written. The two reviewers who said "ready with surgical fixes" (C1 Opus E, B3 codex-gpt-5.5 S1) actually flagged multiple findings that the other twelve corroborated and rated higher; their "ready" verdict is the optimistic minority view.

**Scope of v3:** approximately 30 distinct items across all four tiers, of which ~10 are Tier 1 GATE items requiring plan-text revision before coding. v3 is a substantial revision but not a rethink — the asyncio→threads concurrency decision and the 4-phase feature-branch structure remain correct. v3 fixes the specific design holes v2 introduced or under-fixed.

**Recommended next action:** single v3 plan-revision pass folding in all items from the recommendations matrix above. Author sign-off on the Tier 1 GATE items (T1.1-a UI reservation pattern, T1.2 settlement race fix, T1.3 timeout scope, T1.4 enforcement) before coding. Then phase 1.

**Time estimate for v3 plan revision:** half to one day of focused editing, given the matrix above is decisive. Implementation phases unchanged.
