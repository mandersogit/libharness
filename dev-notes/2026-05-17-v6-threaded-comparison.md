---
status: Draft
created: '2026-05-17'
---

# v6-threaded vs current libharness/planned rewrite — comparison

A discussion document comparing `dev-notes/predecessors/v6-threaded/` to the *current* `src/libharness/pi/` source (v5 port + `set_model` fix + manifest `protocolVersion`) and to the *planned* `2026-05-15-threads-rewrite-plan.md` (asyncio → threads). The point of the comparison is to surface (a) what v6 actually proposes — which is *not* what the rewrite plan proposes — and (b) which v6 ideas might be worth borrowing into our threaded rewrite, even though we're explicitly not taking its core architecture.

## TL;DR

- **v6 is asyncio-inside, threads-outside.** It keeps the v5 asyncio core wholesale (`asyncio.subprocess`, `asyncio.start_server`, `async def` everywhere) and wraps it in a thread-owned facade (`PiAgentHarness` proxy → owner thread → submit coros to a dedicated `AsyncioLoopThread`). Tool execution gets pushed onto a shared `ThreadPoolExecutor`. v6 calls this "thread-owned reshaping."
- **Current libharness is asyncio-inside, asyncio-outside.** v5 port verbatim. Single `set_model` bug fix and a manifest `protocolVersion` handshake added.
- **Planned rewrite is sync-inside, sync-outside.** Rip asyncio out: `subprocess.Popen` + reader threads, `ThreadingTCPServer`, `threading.Event`, sync `def` for everything, `async def` rejected at decoration. FT-first (3.14t) with 3.11 compatibility.
- **The two "threaded" approaches are fundamentally different.** v6 wraps asyncio; the rewrite removes asyncio. The author already decided (2026-05-15) for the latter — v6's hybrid is exactly the option the concurrency-model discussion considered as option **B** ("sync facade over async core") and rejected.
- **Some v6 details are worth porting forward into the rewrite anyway.** Registry freezing, `BridgeEndpoint.env()` method, multi-harness scoping concepts, `HarnessSnapshot` introspection. See § What to borrow.

## Where v6 came from

v6 lives at `dev-notes/predecessors/v6-threaded/` (untracked at time of writing — `git status` shows it as `??`). It is not part of the v1–v5 series reviewed in `dev-notes/2026-05-14-pi-python-harness-version-review.md`. It appears to be a *follow-on synthesis* generated externally after v5: same `pi_python_harness` package layout, same `PiPythonHarness`, same shim, plus an added thread-ownership layer (`runtime.py`, `agent.py`) and a new `THREADING_MODEL.md`.

`docs/IMPLEMENTATION_JOURNAL.md` § "Thread-owned reshaping pass" describes it as a second pass over the v5 synthesis. So v6 ≈ v5 + threading shell, not a from-scratch design.

## High-level posture comparison

| Dimension              | v6-threaded                         | Current `src/libharness/pi/`        | Planned rewrite                              |
| ---------------------- | ----------------------------------- | ----------------------------------- | -------------------------------------------- |
| I/O programming model  | asyncio (in dedicated thread)       | asyncio (caller owns loop)          | threads + blocking syscalls                  |
| Subprocess client      | `asyncio.create_subprocess_exec`    | `asyncio.create_subprocess_exec`    | `subprocess.Popen` + reader threads          |
| Bridge server          | `asyncio.start_server`              | `asyncio.start_server`              | `socketserver.ThreadingTCPServer`            |
| Tool handler shape     | sync OR async                       | sync OR async                       | sync only; async rejected at decoration      |
| Streaming primitives   | async-gen + sync-gen + `ctx.update` | async-gen + sync-gen + `ctx.update` | `ctx.update` only                            |
| Cancellation primitive | `threading.Event`                   | `asyncio.Event`                     | `threading.Event`                            |
| Tool execution thread  | shared `ThreadPoolExecutor`         | asyncio loop                        | per-connection handler thread                |
| Caller surface         | sync (proxy marshals to loop)       | async (`async with`, `await`)       | sync (`with`, plain `def`)                   |
| Multi-harness story    | first-class                         | single harness implied              | TBD (rewrite preserves single-harness shape) |
| FT (3.14t) posture     | not addressed                       | not addressed                       | FT-first                                     |
| Python version floor   | 3.10                                | 3.11                                | 3.11; 3.14t for FT bonus                     |
| Manifest protocolVer.  | absent                              | `MANIFEST_PROTOCOL_VERSION = 1`     | preserved                                    |
| `set_model` wire shape | `model` (BUG)                       | `modelId` (FIXED)                   | preserved                                    |

**Detail:**

- *I/O programming model:* v6 keeps every async coroutine the v5 synthesis had and just moves the event loop into a dedicated thread. The rewrite removes the event loop entirely. The current code is the v5 baseline: asyncio coroutines executed by whatever loop the caller provides (typically `asyncio.run`).
- *Subprocess client:* v6 inherits v5's `asyncio.create_subprocess_exec` with the same reader-task structure. Rewrite plan replaces with `subprocess.Popen(bufsize=0)` and dedicated stdout/stderr reader threads; explicit reasoning in `2026-05-15-threads-rewrite-plan.md` § Design answers #1.
- *Bridge server:* v6 keeps `asyncio.start_server` and adds an optional `tool_executor` that pushes tool execution off the loop. Rewrite uses `socketserver.ThreadingTCPServer` end-to-end; tool execution happens on the handler's own thread (no separate executor).
- *Tool handler shape:* v6 keeps every shape v5 accepted (sync def / async def / sync-gen / async-gen). Rewrite plan accepts only sync `def` and rejects `async def` at decoration time per not-pi-2 precedent.
- *Streaming primitives:* v6 keeps all three v5 mechanisms simultaneously (yields + `ctx.update`). Rewrite collapses to `ctx.update` only — see plan § Design answers #5.
- *Cancellation primitive:* v6 already uses `threading.Event` for cancellation (because tool may run on pool thread, not loop thread); current code uses `asyncio.Event`; the rewrite goes with `threading.Event` for the same reason but without the asyncio fan-in. The current code's `ToolContext._cancelled: asyncio.Event` is the field the rewrite changes.
- *Tool execution thread:* v6's `PythonToolServer` accepts a shared `ThreadPoolExecutor` and runs every tool call through `loop.run_in_executor(...)`. Rewrite runs the tool on the bridge-handler thread directly — `ThreadingTCPServer` already gives one thread per connection, and pi only issues one bridge connection per execute.
- *Caller surface:* v6's caller surface is sync because the proxy marshals coroutine calls to the loop thread. The rewrite reaches the same sync surface but by being sync end-to-end (no loop to marshal to).
- *Multi-harness story:* v6 designs explicitly for multiple `PiAgentHarness` instances sharing one `HarnessRuntime` (one loop thread, one tool pool). Current libharness has no concept of multi-harness sharing; the rewrite plan doesn't pick it up either.
- *FT (3.14t) posture:* v6 never mentions freethreading. The rewrite is FT-first; the current code is 3.11 only.
- *Manifest protocolVer.:* v6 omits the handshake field, current adds it (commit `b4d2ccd`), rewrite preserves it.
- *`set_model` wire shape:* v6 has the same bug v5 had (`model` instead of `modelId`); current fixed it (commit `0058390`); the rewrite carries the fix forward.

## What v6 added on top of v5

Two new modules are the substantive delta:

| File         | LOC | Purpose                                                   |
| ------------ | --- | --------------------------------------------------------- |
| `runtime.py` | 204 | `AsyncioLoopThread` + `HarnessRuntime` + default helpers  |
| `agent.py`   | 408 | `PiAgentHarness` proxy + `_PiAgentHarnessCore` + snapshot |

Plus targeted edits to three v5 files:

- `tools.py` — `ToolRegistry._lock` + `freeze()` + `frozen` property (so registries become thread-safe and become read-only before worker threads start touching them).
- `server.py` — optional `tool_executor` constructor arg; `_dispatch_execute` runs the tool through `loop.run_in_executor(executor, ...)` when an executor is supplied; `send_update_threadsafe` writes update frames back via `asyncio.run_coroutine_threadsafe(...)`.
- `__init__.py` — exports `PiAgentHarness`, `HarnessRuntime`, `AsyncioLoopThread`, `HarnessSnapshot`, default-runtime helpers.

Everything else (`harness.py`, `rpc.py`, `shim.py`, `jsonl.py`, the existing tests, `pyproject.toml`) is the v5 source verbatim.

**Detail:**

- *`runtime.py`:* defines `AsyncioLoopThread` (an asyncio loop running on a named background thread; provides `submit(coro) → concurrent.futures.Future` and `run(coro) → result`). `HarnessRuntime` couples one loop thread to one shared `ThreadPoolExecutor`. `get_default_runtime()` / `close_default_runtime()` provide a process-singleton.
- *`agent.py`:* `PiAgentHarness` is a thread-owned proxy that creates its own owner thread on construction, builds a `_PiAgentHarnessCore` on that thread, and marshals each public-method call through a `queue.Queue` to the owner thread. The owner thread in turn submits coroutines (Pi RPC, bridge I/O) to the runtime's loop thread. A `threaded=False` test mode keeps the core on the calling thread. `HarnessSnapshot` is a frozen dataclass with thread/lifecycle facts — `harness_id`, `owner_thread_id`, `async_loop_thread_id`, `registry_frozen`, `bridge_endpoint`, `extension_paths`.
- *`ToolRegistry` changes:* the registry now has a `threading.RLock`, a `frozen` flag set by `freeze()`, and rejects registration after freezing. Intent: harness owner calls `freeze()` before starting, so the tool-pool worker threads can read the registry without lock contention.
- *`server.py` changes:* `__init__` takes `tool_executor: concurrent.futures.Executor | None`. When set, `_dispatch_execute` calls `loop.run_in_executor(self.tool_executor, self._execute_tool_blocking, ...)`, which runs the tool synchronously on the pool thread. Update frames generated by the tool body call back via `send_update_threadsafe`, which routes through `asyncio.run_coroutine_threadsafe(send_update_async(value), loop)`. Without an executor, the original async path is preserved.

## Module-by-module read

### `runtime.py` (new)

Two classes:

1. `AsyncioLoopThread` — owns one `asyncio.AbstractEventLoop` running on a named daemon thread. `submit(coro) → concurrent.futures.Future` and `run(coro, timeout=…) → result`. `stop()` cancels pending tasks, awaits `shutdown_asyncgens` and `shutdown_default_executor`, then closes the loop.
1. `HarnessRuntime` — pairs one loop thread with one `ThreadPoolExecutor` (named `pi-tool-*`). Owns the executor by default; closes both on `close()`.

Module-level singleton + lock provides `get_default_runtime()` so test code and casual application code don't have to plumb the runtime through.

This is well-built and self-contained. If we ever want a "long-running, multi-harness, in-app embedding" mode later, this module is a reasonable starting point — but it's irrelevant under the rewrite plan because there's no asyncio loop to dedicate a thread to.

### `agent.py` (new)

`PiAgentHarness` is the proxy/owner-thread pattern:

1. Constructor spawns an owner thread named `PiAgentHarness-<id>` (or `MainThread` under `threaded=False`).
1. The owner thread runs a command loop: `while True: cmd = queue.get(); cmd(self._core)`.
1. Public methods are implemented as `self._call(lambda core: core.foo(...))`, which `queue.put`s the lambda and blocks on its `concurrent.futures.Future`.
1. `_PiAgentHarnessCore` does the actual work: it holds the `PythonToolServer`, the `PiRpcClient`, the registry, and the temp dir. Each stateful method asserts thread affinity via `threading.get_ident() == self.owner_thread_id`.
1. `core.call_rpc("prompt", ...)` is the bridge: marshals the async call to the runtime loop thread via `self.runtime.run_async(self.pi.prompt(...))`.

The pattern is straightforward and (importantly) correct under the v6 design: it gives the application a sync surface while keeping all the asyncio code structurally intact and uncontended.

### `server.py` (delta vs current)

```python
def __init__(self, registry, *, ..., tool_executor: concurrent.futures.Executor | None = None):
    self.tool_executor = tool_executor
    ...

async def _dispatch_execute(self, request, reader, writer):
    ...
    if self.tool_executor is None:
        # original v5 path: tool runs on the asyncio loop
        result = await self._execute_tool_async(tool_name, params, ctx)
    else:
        # v6 path: tool runs on a pool thread
        result = await loop.run_in_executor(
            self.tool_executor,
            self._execute_tool_blocking,
            tool_name, params, ctx,
        )
```

The cancellation event becomes `threading.Event` (not `asyncio.Event`) because the tool may be running on a pool thread. The `send_update` callback becomes thread-mode-aware: when on the loop, it `await`s; when off the loop, it bounces through `asyncio.run_coroutine_threadsafe`.

This is a thoughtful change inside the asyncio model. The rewrite plan invalidates it (no loop, no `run_in_executor`, just direct sync call on the handler thread), but the *concept* — "registry must be freezable before workers read it" — carries over.

### `tools.py` (delta vs current)

- `ToolRegistry._lock = threading.RLock()`.
- `ToolRegistry._frozen: bool`.
- `freeze()` sets `_frozen = True`; subsequent `register()` calls raise `ToolError`.
- All registry mutators/readers acquire the lock.
- `frozen` property is lock-protected.

Schema inference, `ToolResult`, `ToolContext` (still `async def update`), `collect_tool_result` (still handles async-gen + sync-gen + awaitable) are otherwise unchanged from v5.

The lock + freeze pattern is sound. The rewrite plan doesn't formalize a "frozen" lifecycle for the registry because the threaded version reads the registry from a single owner (the harness) plus the handler threads, and the handler threads read after `harness.start()` returns — there's a natural happens-before. But the *explicit* freeze is a nice ergonomic: it asserts that registration is closed, instead of leaving "what if a tool is registered after start?" undefined.

### `harness.py` (unchanged vs current)

Verbatim copy of v5's `PiPythonHarness`. Async `__aenter__`/`__aexit__`, asyncio internals.

In v6, `PiPythonHarness` is kept "for compatibility / async-first experiments" (per README and `BEST_PRACTICES.md`) and `PiAgentHarness` is the recommended embedding surface. So the v6 package ships *both*. Our rewrite ships only the (sync) `PiPythonHarness`.

### `rpc.py` (unchanged vs current, modulo the `set_model` bug)

v6's rpc.py is v5's rpc.py — same `set_model` wire-shape bug we fixed at `0058390`. The asyncio internals are otherwise identical.

### `shim.py` (unchanged vs current, modulo protocolVersion)

v6's TS shim doesn't have the protocolVersion handshake. Otherwise identical to current.

### `__init__.py` exports

v6 exports `PiAgentHarness`, `HarnessSnapshot`, `HarnessRuntime`, `AsyncioLoopThread`, `close_default_runtime`, `get_default_runtime` in addition to the v5 surface. Current libharness exports only the v5 surface.

## Where the rewrite plan disagrees with v6

The 2026-05-15 concurrency decision rejects v6's posture explicitly. Quoting `2026-05-15-concurrency-model-discussion.md` § "B. Sync facade over async core":

> Keep all current asyncio internals. Add a `SyncPiPythonHarness` that runs the asyncio loop in a background thread and exposes a sync `with` + sync method calls (v1 used this pattern; it worked).
>
> - **Pro:** preserves the current code; adds an opt-in sync surface.
> - **Con:** two paradigms to maintain. Subtle seam bugs around loop lifecycle and exception propagation.
> - **Con:** doesn't fix the `on_*` hooks problem unless we also rewrite the Agent class as sync (at which point the async core isn't earning its keep).

v6 is exactly option B. The decision picked option D (threads on freethreaded CPython 3.14t, backward-compatible with C / threads on 3.11 GIL). The rationale: the `on_*`-hooks design intent forces sync hooks; once hooks are sync, an async core isn't earning its keep; and family precedent (hildy, simple-harness, not-pi-2) is unanimously sync.

Importantly, v6 was generated *before* the libharness concurrency discussion happened and reflects a different design instinct ("keep the existing code; add a thread surface on top"). It's not engaging with the design choices made on 2026-05-15.

## What v6 gets right that we should consider keeping

Despite the architectural mismatch, there are local-good ideas in v6 that the rewrite plan could borrow without compromising the threads-end-to-end posture.

| Idea                                          | Cost to adopt | Recommendation                        |
| --------------------------------------------- | ------------- | ------------------------------------- |
| `ToolRegistry.freeze()` + `frozen` property   | ~10 LOC       | Adopt; ergonomically clean            |
| `HarnessSnapshot`-style introspection         | ~30 LOC       | Adopt selectively (thread IDs, paths) |
| `BridgeEndpoint.env(prefix=…)` already exists | 0 LOC         | Already in current; keep              |
| Explicit per-harness owner thread name        | ~5 LOC        | Defer (no users yet)                  |
| Shared `ThreadPoolExecutor` for tools         | ~50 LOC       | Defer; needs multi-harness story      |
| `threaded=False` test mode                    | n/a           | Not needed in pure-sync rewrite       |
| `HarnessRuntime` singleton                    | n/a           | Not needed in pure-sync rewrite       |

**Detail:**

- *`ToolRegistry.freeze()` + `frozen` property:* asserts "registration is closed" lifecycle. Lets the harness fail loudly if a user tries to register a tool after `harness.start()`. ~10 LOC + 1 test. Worth adopting into the rewrite — the locking can drop (one-thread registration), but the `_frozen` flag carries useful intent. Add it in phase 1 of the rewrite.
- *`HarnessSnapshot`-style introspection:* a frozen dataclass returned by `harness.snapshot()` with current thread IDs, bridge endpoint, extension paths, started state. Useful for debugging tool-pool issues, especially under FT where "which thread is this?" matters. Adopt a slimmed-down version (drop the `owner_thread_id` / `async_loop_thread_id` fields, since we don't have a dedicated I/O thread; add the bridge listener thread ID and active connection count).
- *`BridgeEndpoint.env(prefix=…)`:* both v6 and current already have this; flagged here so we don't drop it during the rewrite.
- *Explicit per-harness owner thread name:* v6's `owner_thread_name` constructor arg lets multiple harnesses get distinguishable thread names in tracebacks. The rewrite is single-harness for now; defer.
- *Shared `ThreadPoolExecutor` for tools:* v6's central reason for this is "centralize tool concurrency limits across N harnesses." The rewrite has no N-harness story; deferring this avoids paying complexity now. If multi-harness becomes a goal, revisit — the rewrite's `ThreadingTCPServer` handler-per-connection model can be retrofitted to submit to a shared pool.
- *`threaded=False` test mode:* exists in v6 because the threaded facade hides too much for tests; our rewrite is sync end-to-end, so tests just call `harness.client.get_state()` from the test thread. No facade to flip off.
- *`HarnessRuntime` singleton:* the rewrite has no loop to share, and the tool pool isn't worth singletonizing for a single-harness world.

## What v6 *doesn't* address that the rewrite plan does

- **Freethreading.** v6 has no FT awareness. The rewrite is FT-first by design (3.14t primary; 3.11 GIL compatible).
- **`async def` rejection.** v6 still accepts async tool handlers. The rewrite rejects them at decoration with a remediation message (not-pi-2 precedent).
- **Hook dispatch architecture.** v6 has no `Agent` class and no `on_*` hooks; the threaded facade doesn't address how user code would react to pi events synchronously. The rewrite plan dedicates § 6 to the hook dispatch mechanism (single dispatch site, reader thread, write-back through `_send_lock`).
- **Subprocess buffering gotcha.** v6 uses `asyncio.create_subprocess_exec`, which handles the buffering for us. The rewrite plan calls out `subprocess.Popen(bufsize=0)` + raw `os.read(fd, n)` on stdout, with explicit reasoning about the deadlock case (pi line-flushes, Python block-buffers).
- **Disconnect-watcher sidecar.** v6's `watch_disconnect` is an asyncio task. The rewrite plan keeps the sidecar pattern but as a daemon thread doing `sock.recv(1)`.
- **Concrete regression test catalog.** The rewrite plan enumerates 10 specific tests (handler leak on mid-stream disconnect, SIGKILL external, pending request abandoned at close, concurrent execute, concurrent prompt-and-event, async-def rejection, stale-state restart, long-running tool ignoring cancellation, bridge-token mismatch, invalid JSON from pi). v6 has none of these.

## What to do with v6

Three options:

1. **Leave v6 vendored as a curiosity, do nothing else.** It's already at `dev-notes/predecessors/v6-threaded/` and excluded from lint/test toolchains by `dev-notes/predecessors/v*/` rules. Cost: ~7MB on disk (includes `.pyc` caches and `.DS_Store`).
1. **Treat v6 as a v5-class predecessor and link from the predecessor README.** Update `dev-notes/predecessors/README.md` to mention v6 in the Layout section. Pro: discoverability. Con: it isn't part of the v1–v5 review chain; the predecessor README's editorial framing ("AI experiments whose review became this project's port baseline") doesn't quite fit.
1. **Promote the worthwhile ideas now: `freeze()` and `HarnessSnapshot`.** Add a small task to the threaded-rewrite plan: "phase 1 also adds `ToolRegistry.freeze()`; phase 4 adds `HarnessSnapshot`." Drop v6 once those ideas are absorbed, OR keep it as historical reference along with v1–v5.

**Recommendation: do both 2 and 3.** Link v6 from the predecessor README (one sentence: "v6-threaded is a follow-on synthesis applied on top of v5; it explores option B from the concurrency-model discussion (sync facade over async core), which we ultimately rejected in favor of option D"). And add the two small ideas (`freeze()`, `snapshot()`) to the rewrite plan as scoped follow-ons — they're 30-60 LOC total and improve the rewrite without compromising it.

## Open questions for the author

1. Was v6 generated by you or handed to you by someone else? (The implementation journal credits "the synthesis" but doesn't sign it.) If you generated it, was it pre- or post- the concurrency-model decision? That affects whether v6 should be read as an unreviewed exploration or an alternative that lost.
1. Do you want `freeze()` and `snapshot()` folded into the threaded rewrite, or kept separate as a later "ergonomics" pass?
1. Should we keep `PiAgentHarness` (the threaded facade) as a name for *anything* in the rewrite? v6 uses it for "thread-owned proxy"; the rewrite could conceivably use it for "the agent-shaped class with `on_*` hooks." Reusing the name in a different sense is potentially confusing.

## Reference: file map

| Path                                | Status in v6 | Status in current  |
| ----------------------------------- | ------------ | ------------------ |
| `src/pi_python_harness/__init__.py` | extended     | v5 verbatim        |
| `src/pi_python_harness/agent.py`    | new          | absent             |
| `src/pi_python_harness/cli.py`      | as v5        | absent             |
| `src/pi_python_harness/harness.py`  | as v5        | v5 verbatim        |
| `src/pi_python_harness/jsonl.py`    | as v5        | v5 verbatim        |
| `src/pi_python_harness/rpc.py`      | as v5        | v5 + set_model fix |
| `src/pi_python_harness/runtime.py`  | new          | absent             |
| `src/pi_python_harness/server.py`   | extended     | v5 verbatim        |
| `src/pi_python_harness/shim.py`     | as v5        | v5 + protocolVer.  |
| `src/pi_python_harness/tools.py`    | extended     | v5 + protocolVer.  |
| `docs/THREADING_MODEL.md`           | new          | n/a (sib. DESIGN)  |

**Detail:**

- *`agent.py` / `runtime.py`:* the v6 thread-ownership delta — proxy harness and dedicated asyncio loop thread + shared tool pool.
- *`harness.py`:* identical to v5 in both v6 and current; kept "for compatibility" in v6, kept as the primary surface in current.
- *`rpc.py`:* v6 carries v5's `set_model` bug (`model` field); current fixed it (commit `0058390`) to use `modelId` matching pi-mono's `rpc-types.ts:31`.
- *`server.py`:* v6 adds optional `tool_executor`; current keeps v5's loop-only execution.
- *`shim.py`:* v6 has no protocolVersion handshake; current asserts `protocolVersion == 1` in the shim (commit `b4d2ccd`).
- *`tools.py`:* v6 adds `_lock` + `freeze()` + `frozen`; current keeps v5's lock-free single-owner registry and adds the protocolVersion constant.
- *`docs/THREADING_MODEL.md`:* v6's own contract for the thread-owned posture; libharness has a counterpart in `docs/DESIGN.md` (architecture sketch + Resolved decisions table).
