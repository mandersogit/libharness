---
status: In co-design
created: '2026-05-15'
---

# Threads-rewrite plan review — synthesis

Consolidated, deduplicated findings from six independent adversarial reviews of `dev-notes/2026-05-15-threads-rewrite-plan.md`. Ordered by *severity × corroboration count*, with options and a recommendation per finding so this doc can drive a single-pass plan revision.

## Reviewer roster

| ID  | Route                | Model                        | Reasoning | Scope                        | Verdict                |
| --- | -------------------- | ---------------------------- | --------- | ---------------------------- | ---------------------- |
| A   | codex direct         | gpt-5.4                      | xhigh     | concurrency correctness      | rework needed          |
| B   | codex direct         | gpt-5.4                      | xhigh     | API contract / test coverage | rework needed          |
| C   | libharness → pi      | openai-codex / gpt-5.5       | xhigh     | generalist                   | rework needed          |
| D   | libharness → pi → OR | mistralai/mistral-large-2512 | n/a       | generalist                   | "ready with changes"   |
| E   | Opus subagent        | claude-opus-4-7              | n/a       | generalist                   | rework (small/focused) |
| F   | Opus subagent        | claude-opus-4-7              | n/a       | generalist                   | "ready with changes"   |

**Detail:**

- *D's findings:* of 10, 6 were misreadings (the plan already addressed what D criticized). 4 were valid (one MINOR-grade only). D is included here for completeness on the few valid points but its "CRITICAL" labels were unreliable.
- *Verdict spread:* 4 of 6 reviewers recommended substantive rework; 2 (D, F) marked "ready with changes." E and F are both Opus on the same prompt and disagree on severity — E sees architectural risk where F sees contract-tightening work. F's calibration is the most charitable in the set; A and E's are the strictest. The truth is probably between E and F.

## Tier 1 — Gating findings (resolve before coding starts)

Findings flagged CRITICAL by ≥2 independent reviewers, or flagged CRITICAL by one reviewer with no rebuttal from the others.

### T1.1 Reader-thread sync hook dispatch deadlocks on reentrant RPC

**Corroboration:** A.1 (CRITICAL), C.2 (CRITICAL), E.C2 (CRITICAL). Three independent reviewers; the single highest-confidence finding in the set.

**The bug.** Plan § 6 commits to: (a) `_handle_message` runs on the stdout reader thread, and (b) hooks (legacy `_event_handlers`, future `_hook_dispatcher`) are dispatched *synchronously* from `_dispatch_event` because "decision events need a synchronous result for pi to advance." Result write-back goes through `_send_lock`-guarded `proc.stdin.write`. If any hook calls `client.send(...)` — `get_state()`, `prompt()`, `abort()`, anything — the hook writes the request and blocks on the pending queue. **Only the reader thread can drain that response from pi's stdout, and the reader thread is busy executing the hook.** Hang until `request_timeout`. Asyncio gets away with the same shape because `await client.send(...)` yields the loop thread; threads do not yield.

The plan's "Long-running hook caveat" (§ 6, line 238) acknowledges that slow hooks block event delivery and that pi's pipe buffer could fill — but frames it as a performance issue, not a correctness one. Reentrant `send()` from a hook is a textbook deadlock, not slow.

**Citations:** `plan.md:121,129,229-231,238` vs `src/libharness/pi/rpc.py:215-239,345-409`.

**Options:**

- *(a) Forbid reentrant RPC; detect at runtime.* Set a thread-local marker "in hook dispatch" around the hook call; `client.send()` checks the marker and raises a dedicated error. Cheap, no design change. Hooks have to bounce out (queue a follow-up, return a value that the caller acts on).
- *(b) Move all hook dispatch to a worker pool.* `concurrent.futures.ThreadPoolExecutor(max_workers=N)` runs hooks. Decision events still need to return a value to pi — correlate decision responses back through the reader by id. Allows arbitrary reentrancy but doubles the design complexity and adds a configurable `max_workers`.
- *(c) Hybrid — notification vs decision split.* Notification-only events (`on_event` style) dispatch to a worker queue. Decision events (future `Agent.on_*` hooks that pi awaits) stay synchronous on the reader thread but with the (a) guard against reentrant `client.send()`. Matches the notification-vs-decision distinction already drafted in `dev-notes/2026-05-14-event-bridge-proposal.md`.

**Recommendation: (c) is the long-term answer; (a) is the minimum viable position for phase 1.** Phase 1 doesn't ship the decision-event protocol — that's the Agent-class co-design gate. So phase 1 only needs to make the notification-only path (the existing `_event_handlers`) safe. Implement (a) now: thread-local "in dispatch" marker + loud error on reentrant `client.send()`. Document in `docs/DESIGN.md` that callback authors must not call back into the client. Test: a registered `on_event` handler that calls `client.get_state()` raises `ReentrantRPCError` immediately, not hangs.

Defer (c) to when the Agent-class hooks land — that's when worker-pool dispatch is actually needed.

### T1.2 Shared mutable RPC correlation state — two adjacent hazards

**Corroboration:** A.3 (CRITICAL), C.4 (MODERATE), E.C3 (CRITICAL), F.3 (MODERATE). Four reviewers, mixed severity but unanimous on "the plan understates this."

**The bugs (two related, often conflated):**

- *(a) `_pending` dict mutation unprotected.* Plan's architecture sketch annotates the field as "`(lock-protected)`" but § 3 prose never names a lock or acquisition order. Three concurrent mutators: caller threads (`__setitem__` + `pop`), reader thread (`__getitem__` + `q.put`), close path (`_fail_pending` iterates + clears). On 3.11 the individual ops are atomic under the GIL, but `_fail_pending`'s `list(values())` + `clear()` racing with caller `__setitem__` raises `RuntimeError: dictionary changed size during iteration` in practice. On 3.14t the table itself can corrupt.
- *(b) `Queue(maxsize=1)` is not a safe `Future` replacement under failure.* `Future.set_result/set_exception` has single-assignment semantics. `Queue(maxsize=1)` does not. If `_fail_pending` puts a sentinel exception during `close()`, then a late real response arrives, `_handle_message`'s `q.put()` blocks forever on a full queue with no consumer. Symmetric: if a caller times out and pops, then a late response arrives, no one is waiting.

**Citations:** `plan.md:31,158-174,303-305` vs `rpc.py:103,225,236,361,411-415`.

**Options for (a):**

- *(a1) Single `_pending_lock = threading.Lock()`.* Guard every dict op. Snapshot `list(values())` under the lock in `_fail_pending`; release before iterating. Document the lock-order constraint: *do not hold `_pending_lock` across `q.get(timeout=t)`* (F.3's concrete framing) and *acquire `_pending_lock` before `_send_lock`* (or vice versa — pick one).

**Options for (b):**

- *(b1) Replace `Queue(maxsize=1)` with `concurrent.futures.Future`.* `Future` has proper single-assignment semantics via `set_result` / `set_exception` / `cancel`. Use `future.result(timeout=t)` for the caller wait. Stdlib, well-tested, exactly the semantics the asyncio version had.
- *(b2) Custom `_ResponseSlot` with lock + event + value + done flag.* More code, equivalent behavior to (b1). Worth it only if you need behavior `Future` doesn't have (e.g. multi-result; we don't — RPC is one response per id).
- *(b3) Keep `Queue(maxsize=1)`, use `put_nowait` + swallow `queue.Full`.* Cheapest but doesn't fix the symmetric failure where the caller has already popped.

**Recommendation: (a1) + (b1) together.** `concurrent.futures.Future` is the asyncio `Future` analogue, single-assignment safe, cancellable, and supports timeout via `result(timeout=)`. The whole `Queue(maxsize=1)` choice in the plan was framed as "natural for multiple frames per id" — but the plan itself notes (§ 3) that streaming exists only on the bridge channel, not RPC. So the Queue's only feature over Future is unused. Use Future, drop the failure-mode complexity entirely.

Regression test (one test, both hazards): N caller threads each call `send()` against fake-pi; reader thread resolves them in arbitrary order; a parallel thread calls `close()` halfway. Assert: no `RuntimeError`, all callers receive either a response or a `PiRpcProcessError`, no caller hangs past timeout.

### T1.3 Bridge handler read timeout regresses to nothing

**Corroboration:** E.C4 (CRITICAL). Singleton, but unambiguous and cited.

**The bug.** Current `server.py:99` wraps the initial request frame read in `asyncio.wait_for(reader.readuntil(b"\n"), timeout=max(1.0, self.timeout_ms/1000.0))`. `BaseRequestHandler` under `ThreadingTCPServer` exposes a raw `self.request: socket.socket` with no built-in read deadline. Plan § 2 mentions `daemon_threads = True` and `allow_reuse_address = True` but **never mentions `self.request.settimeout(...)`**. A misbehaving (or malicious) connector that opens the bridge port and never sends `\n` leaks the handler thread indefinitely. `daemon_threads` lets the process exit, but `harness.close()` will block on its explicit `thread.join(timeout=...)`, and a tight test loop will pile threads.

**Citations:** `plan.md:132-156` vs `src/libharness/pi/server.py:99`.

**Options:**

- *(a) `self.request.settimeout(self.timeout_ms/1000)` at the top of the handler.* Single line, exact behavioral equivalence to current asyncio. All subsequent `request.recv` / `request.sendall` raise `socket.timeout` on stall.
- *(b) `socket.setdefaulttimeout()` set once at server startup.* Globally affects every socket the process opens, including ones we don't own. Too broad.
- *(c) Custom `selectors.select()` loop inside the handler.* Verbose; reinvents what `settimeout` already does.

**Recommendation: (a).** Trivial; matches current contract. Add a regression test: client opens TCP to the bridge port, writes partial bytes (no `\n`), waits; assert the handler returns within `timeout_ms` and no thread leaks.

### T1.4 Sidecar `recv(1)` will silently steal any future bidirectional byte

**Corroboration:** E.C1 (CRITICAL), F.4 (MODERATE).

**The bug.** Plan preserves the asyncio-era "sidecar reads 1 byte for EOF detection" pattern but moves it to a thread calling `sock.recv(1)`. Today this works *only* because the shim sends no bytes after the request frame — the bridge is effectively half-duplex shim→server post-handshake. **The plan's own § 6 prepares for round-trip decision events on the same socket.** Any future bidirectional message (cancel-ack, hook decision-response, keep-alive) would be silently consumed by the sidecar's `recv(1)` and dropped. Even today, if a misbehaving shim sends a stray byte before close, the sidecar swallows it and the handler never sees it. No test covers "more than just EOF arrives on the bridge socket."

**Citations:** `plan.md:148-156` vs `src/libharness/pi/server.py:138-149`, `src/libharness/pi/shim.py:99-112`.

**Options:**

- *(a) Commit to the half-duplex bridge contract.* Document in `docs/DESIGN.md` § Bridge protocol: "after the initial request frame, the client sends no further bytes until the connection is closed." Then `recv(1) → b""` is sound by contract. Add a regression test: server receiving an extra byte mid-tool surfaces loudly (logged + handler returns with `protocol_violation` error).
- *(b) Drop the sidecar; use `selectors.select([sock, cancel_pipe])` inside the handler.* Same thread owns the socket; can multiplex read/cancel/write. Restructures the handler but no longer fragile against future protocol evolution.
- *(c) `MSG_PEEK` instead of consuming.* `recv(1, socket.MSG_PEEK)` leaves the byte in the buffer. Brittle (POSIX-specific edge cases, partial-read semantics) and doesn't actually solve the design problem.

**Recommendation: (a) for phase 1; revisit when round-trip events land.** The event-bridge proposal (`dev-notes/2026-05-14-event-bridge-proposal.md`) hasn't been resolved yet, so we don't know whether round-trip messages will use the same socket or a separate channel. Pin the half-duplex contract now; if and when the event-bridge proposal lands with same-socket round-trip, refactor the watcher to (b) then. Document the contract loudly so the future decision is informed.

## Tier 2 — Should land before phase 1

Findings flagged MODERATE by ≥2 reviewers, or a single MODERATE that's particularly cheap to fix.

### T2.1 `docs/DESIGN.md` migration is missing from rewrite scope

**Corroboration:** B.4, C.6, E.M5, F.8. **Four reviewers — the highest cross-corroboration finding in the set.**

`docs/DESIGN.md` advertises `async def echo` tool examples (`:144`), `await ctx.update(...)` (`:163`), `async with PiPythonHarness(...)` (`:191`), and "asyncio JSONL server" architecture sketch (`:26`). Plan's file scope (`plan.md:43`) lists only Python source + `pyproject.toml`. After phase 4 the docs would still teach the old contract. Sidecar finding: vendored `dev-notes/predecessors/v5-synthesis/examples/basic.py:15` is also async, but the predecessors directory is a historical reference — that one stays as-is.

**Recommendation:** add `docs/DESIGN.md` to the phase 4 file list. The specific sections needing rewrite: § ToolRegistry, § ToolContext, § PiRpcClient, § PiPythonHarness, § Failure modes, plus the architecture sketch at the top. Spot-check `CLAUDE.md`'s architecture sketch too. Treat as merge-blocker, not follow-up.

### T2.2 `PiRpcClient` callback contract break is understated

**Corroboration:** B.1 (CRITICAL), C.7 (MODERATE).

`on_event` and `set_extension_ui_handler` currently accept *awaitable* handlers and dispatch via `await`. Plan says public methods "become plain `def`" — but async handlers will stop working, *and* sync handlers now need thread-safety they didn't need before. `tests/pi/test_rpc_fake.py` doesn't exercise either surface.

**Recommendation:** explicit contract change in `docs/DESIGN.md` *and* the rewrite plan. Reject coroutine event/UI handlers at registration with a loud error pointing at the migration note. Add a regression test that installs an `async def` event handler and asserts immediate rejection.

### T2.3 `async def` rejection misses descriptor-bypass + needs runtime `isawaitable(result)` check

**Corroboration:** B.2 (CRITICAL — comprehensive bypass list with verified empirical examples), E.M6 (MODERATE — runtime extension).

`inspect.iscoroutinefunction` / `inspect.isasyncgenfunction` at registration catches plain `async def`, `partial`, `classmethod`. Misses: `functools.wraps` sync wrappers that return coroutines, named lambdas returning coroutines, raw `staticmethod` descriptors, "sync callable that returns a coroutine" generally. Without the await-point check in `collect_tool_result`, these normalize to bogus text like `"<coroutine object ...>"` instead of failing.

**Recommendation:** belt-and-braces — keep the decoration-time check *and* add `if inspect.isawaitable(result): raise ToolError(...)` inside `collect_tool_result` after the call returns. Two tests: (1) lambda returning a coroutine raises at execute time; (2) `functools.wraps` of an async function raises at execute time.

### T2.4 Sync-generator tool support — plan is internally inconsistent

**Corroboration:** B.3, C.6, F.7 (adjacent). Plan § 1 says "Keeps `inspect.isgenerator`"; § 5 says "drop sync-gen." `docs/DESIGN.md:153` and `dev-notes/predecessors/v3/docs/runbook.md:120` teach generator-based tools as a supported pattern.

**Recommendation:** pick one and commit. **Drop sync-gen** is the more defensible call — `ctx.update` is the single streaming mechanism per v4 precedent, and no current test exercises sync-gen end-to-end through the bridge anyway. Update both the plan (resolve § 1 vs § 5 contradiction) *and* `docs/DESIGN.md` to drop generator examples. Add a regression test: a sync-generator tool function is rejected at registration with a remediation message.

### T2.5 `prompt_and_wait` is unscoped and untested standalone

**Corroboration:** E.M3, F.6.

`prompt_and_wait` is the most-used public method (every live test calls it transitively) and is a perfect deadlock canary for T1.1 — the caller thread blocks on `done.wait()` *while* the reader thread is delivering the events that set `done`. Plan never names it in the file-by-file change list or the regression-test list.

**Recommendation:** call out `prompt_and_wait` in the `rpc.py` change list. Add to `test_rpc_fake.py`: a standalone test that drives `prompt_and_wait` against fake-pi and asserts all events are delivered and the call returns. Bonus: a second test where an `on_event` handler raises — assert `prompt_and_wait` still completes (the handler is logged but doesn't stop the wait).

### T2.6 Cancellation semantics overpromise handler return

**Corroboration:** A.4, C.3.

Plan test #8 expects "handler returns to pool" while the tool body keeps running. Both cannot be true: the tool body executes on the handler thread, so the thread stays occupied until the tool returns. Plan also accepts that uncooperative tools (no `ctx.cancelled` check) leak handler threads as daemons — but doesn't surface this loudly enough vs the current asyncio code which can actually cancel tool tasks.

**Recommendation:** rewrite plan § 4 and test #8 description to say explicitly: "uncooperative tool body remains running on its handler thread until completion; daemon-thread shutdown semantics still apply at process exit; no in-process cancellation is possible." Document in `docs/DESIGN.md` as a known limitation of the threading model, with the recommendation that long-running tools should poll `ctx.cancelled`. Test #8 should assert process-exit cleanliness, not handler return.

### T2.7 Partial-start cleanup: `__exit__` doesn't run if `__enter__` raises

**Corroboration:** A.5 (MODERATE), C.1 (CRITICAL).

Python language gotcha. If `start()` raises after `PythonToolServer.start()` succeeded but before assigning `self.pi`, `__exit__` is never called. Plan claims `__exit__` cleanup is unconditional but the standard `__enter__`/`__exit__` protocol doesn't guarantee that.

**Recommendation:** `PiPythonHarness.start()` wraps its sequence in `try: ... except: self.close(); raise`. The `close()` must be idempotent (already needs to be for re-close safety). Add a regression test: monkey-patch `PiRpcClient.start` to raise after `PythonToolServer.start()` succeeds; enter the harness with `with`; assert the listener socket is closed, no server thread remains, and the tempdir is gone.

### T2.8 Reader-thread fatal-error observable state

**Corroboration:** C.9 (MODERATE), F.10 (MINOR).

Plan's test #10 (invalid JSON from pi) doesn't specify the post-reader-death contract for `client.send()`. Today asyncio bubbles the exception out of the loop; daemon reader thread under threading would silently log to stderr. Future `send()` calls might hang waiting for a response from a dead reader.

**Recommendation:** add `_fatal_error: Exception | None` to `PiRpcClient`. `_stdout_reader` wraps the per-record dispatch in `try/except Exception` → log + set `_fatal_error` + call `_fail_pending(exc)` + exit loop. Future `send()` checks `_fatal_error` and raises it immediately. Stderr context is preserved via `self.stderr`. Test: feed invalid JSON via fake-pi; assert subsequent `send()` raises a `PiRpcProcessError` with the original JSON error in its `__cause__`.

### T2.9 Stdin lock + close ordering: `ValueError` race

**Corroboration:** F.2 (MODERATE). Singleton, but specific and well-evidenced — closely related to T1.2.

Under asyncio, `is_closing()` + `BrokenPipeError` covers writes-after-close cleanly. Under threading, after `proc.stdin.close()` returns, subsequent `proc.stdin.write(...)` raises `ValueError("write to closed file")`, not `BrokenPipeError`. Plan doesn't specify (a) whether `close()` is held under the write lock, (b) whether writers check a `_stdin_closed` flag, or (c) which exceptions writers swallow.

**Recommendation:** invariant — every `proc.stdin.write` is `with _send_lock: if not _stdin_closed: ...`; `_stdin_closed` is set under the same lock by `close()` *before* `proc.stdin.close()`; writers swallow `BrokenPipeError | ValueError | OSError`. Test: concurrent `close()` and a hook-decision write; assert no exception escapes.

## Tier 3 — Singleton substantive findings worth landing

Single-reviewer findings with specific evidence and a clear fix. Most can be added to phase 1 or phase 2 work without affecting design decisions.

### T3.1 Control-message ordering: `extension_ui_response` must follow `extension_ui_request` immediately

**Reviewer:** A.2 (CRITICAL).

Pi sends `extension_ui_request` and expects the *very next* stdin line to be the corresponding `extension_ui_response`. Plan's `_send_lock` provides mutual exclusion but no priority — another thread can race in with a normal request between them.

**Options:** (a) priority queue for stdin writes with UI responses preferred; (b) keep `_send_lock`, but `_send_extension_ui_response` is the only path that writes outside hook dispatch and is called synchronously from the reader thread before returning. With T1.1 (hooks on reader thread or guarded by reentrancy detection), (b) is correct by construction.

**Recommendation:** (b). With T1.1's fix in place, the reader thread serializes hook + UI dispatch + UI response write atomically. Add a regression test using fake-pi: trigger a `get_state()` that causes fake-pi to emit `extension_ui_request`; deliberately stall the UI handler; race a second caller thread calling `prompt()`; assert fake-pi never sees the `prompt` line before `extension_ui_response`.

### T3.2 Wire-shape "unchanged" claim names the wrong object + tests are too narrow

**Reviewer:** B.5 (MODERATE).

Plan says "Wire shape of `ToolResult`, `ToolSpec`, `RegisteredTool` unchanged." `RegisteredTool` is internal; the wire-visible surfaces are `ToolSpec.to_manifest()` and `ToolResult.to_wire()`. The shim consumes optional fields (`label`, `promptSnippet`, `promptGuidelines`, `executionMode`, `terminate`) that current tests don't cover.

**Recommendation:** fix the wording in the plan. Add round-trip tests in `test_tools.py` and `test_server.py` for each optional `ToolSpec` field and non-default `ToolResult` field (`terminate`, image blocks, `details`).

### T3.3 `ToolRegistry` mutability under parallel execution / FT

**Reviewer:** C.5 (MODERATE).

`ToolRegistry._tools` is an unlocked mutable dict. With the threaded bridge calling `manifest()` / `get()` concurrently against possible late `register()` calls, 3.14t hits the same dict-corruption window as `_pending`.

**Options:** (a) freeze the registry at `PythonToolServer.start()`; reject `register()` after start; (b) add a `threading.RLock`; allow concurrent registration; (c) document that registration after `start()` is unsupported and rely on convention.

**Recommendation:** (a). The pi-tools-are-registered-upfront pattern is what every existing test does; locking down the contract is cleaner than supporting late registration we don't actually need. Add a test that asserts `register()` after `start()` raises.

### T3.4 Bridge server has no bounded-connection / slowloris controls

**Reviewer:** C.8 (MODERATE).

Current async server has an 8 MiB decoder cap and a request timeout. Plan doesn't explicitly preserve size limits, socket timeouts, or max-active-handler bounds. `ThreadingTCPServer` thread-per-connection without these is a DoS surface.

**Recommendation:** plan § 2 must specify: `request.settimeout(timeout)` (overlaps T1.3), max request bytes via a counting wrapper on `recv`, max active handlers via `threading.Semaphore(max_handlers)` with `acquire(blocking=False)` in the handler entry (loud reject on exhaustion). Default max_handlers to a small number (16) — tools should not be that parallel. Add a regression test: 100 client connections; assert connections beyond max_handlers are rejected, server stays responsive.

### T3.5 `ctx.update` blocking semantics not documented

**Reviewer:** F.7 (MODERATE).

Sync `ctx.update(...)` looks like fire-and-forget to anyone migrating from `await ctx.update(...)`. It isn't — `sendall(...)` blocks until the kernel buffer accepts the bytes. A slow pi-side consumer means a stuck handler thread holding the tool function.

**Recommendation:** one-line clarification in plan § 5 *and* `docs/DESIGN.md` § ToolContext: "`ctx.update` is synchronous; blocks until the bridge write returns; no timeout." Add a test that exercises a slow pi consumer (fake-pi delays its read) and confirms `ctx.update` blocks rather than dropping the frame.

### T3.6 `_events` queue is unbounded

**Reviewer:** D.6 (the one valid Mistral finding worth keeping).

Today asyncio's `_events = asyncio.Queue()` is unbounded. The plan keeps the same shape. A chatty pi with no consumer could grow it unboundedly.

**Recommendation:** set `_events = queue.Queue(maxsize=N)` with a reasonable bound (e.g. 4096). On full, log + drop oldest. Document that `_events` consumers should drain promptly. Optional — could also be marked "fine, leak detected via monitoring" if we trust the consumer pattern.

### T3.7 `asyncio_mode = "auto"` removal can silently no-op tests

**Reviewer:** E.M4 (MODERATE).

Phase 4 rewrites all `async def test_*` to sync and drops the `pytest-asyncio` ini option. If the phase 4 commit drops the option *before* converting every async test, pytest will collect-and-skip them with `PytestUnhandledCoroutineWarning` rather than fail — silent green-by-skip.

**Recommendation:** add a commit-time guard in the same phase 4 commit: a shell-level assertion that `grep -r 'async def test_' tests/` returns no match, *and* a `pytest --collect-only` count assertion against the prior baseline. Either fails the commit if a test was missed.

### T3.8 `bufsize=0` + raw `os.read` justification is oversold + has a close-path race

**Reviewer:** E.M2 (MODERATE).

Plan claims "default block-buffering would deadlock our read loop." That argument applies to *writers*, not readers; `Popen` stdout pipe is unbuffered by default for streams. Meanwhile `os.read` on the raw fd creates a close-path race with `Popen.__exit__` closing the same fd, producing "I/O operation on closed file" or double-close.

**Recommendation:** drop the raw-fd `os.read` and use `proc.stdout.read(4096)` (current asyncio code's choice). If the original concern about line-flushing was real, document the specific failure mode and add the targeted test; otherwise just use the buffered path. Either way, document the close sequence: (1) `proc.stdin.close()` → wait for reader thread to observe EOF (`b""`) → join reader → `proc.wait()`.

### T3.9 Concurrent-tool test (#4) under-specified

**Reviewer:** E.M7 (MODERATE).

Plan test #4 says "Two parallel tools; results don't cross-talk." How is cross-talk asserted? Both tools currently get a fresh `ToolContext` per bridge connection (the shim opens a new socket per `execute`), so cross-talk would only happen if there's a bug in the shim or the server's request-id correlation.

**Recommendation:** make the test explicit. Each tool emits N distinct `update` frames carrying its own sentinel value (e.g. `tool_a` emits values `a1..a10`, `tool_b` emits `b1..b10`); the test asserts each shim connection observes only its own sentinels in order.

### T3.10 UI-handler dispatch ordering invisible in plan § 6

**Reviewer:** E.M1 (MODERATE).

Current `_handle_message` does `_dispatch_event(event)` *and then* `_handle_extension_ui_request(message)` — events fire before the UI response is computed and sent. Plan's § 6 only diagrams `_dispatch_event → hook_dispatcher`; the UI-request branch is never mentioned.

**Recommendation:** plan § 6 sequence diagram must show the UI-request branch explicitly, with an ordering note: an `on_event` handler for `extension_ui_request` observes the event *before* `_send_extension_ui_response` writes back. Add a unit test asserting this ordering.

## Tier 4 — Polish (cheap, do alongside)

These are all single-reviewer, low severity, easy fixes. Roll into the relevant phase commit without ceremony. (Decisive actions for each are in the § Recommendation matrix below.)

- Silent-leak tool: no observability signal (F.9).
- "Green only at the end" forfeits `git bisect` (F.11).
- Event dual-delivery contract unstated (F.12).
- `_send_lock` vs `_stdin_lock` name inconsistency (F.1).
- Sidecar `join(timeout=0.1)` fires too early (A.6).
- Restart contract test contradicts current code (C.10).
- LOC estimates rosy (E.m1).
- `daemon_threads` + `keep_temp` edge case (E.m2).
- FT runtime-meta test missing (E.m3).

## Consolidated punch list — what to do before phase 1

Strict-priority order. Lines marked **GATE** must be in the plan *text* (revised) before coding starts; the rest can be tracked as commits during the implementation.

1. **GATE** Rewrite § 6 to specify the hook dispatch contract for phase 1 (T1.1 — pick (a) "forbid + detect reentrant RPC"; defer (c) to Agent-class co-design).
1. **GATE** Rewrite § 3 to name `_pending_lock`, specify lock-order and `q.get`-outside-lock rule, and switch `Queue(maxsize=1)` → `concurrent.futures.Future` (T1.2).
1. **GATE** Add `self.request.settimeout(self.timeout_ms/1000)` to § 2 handler design + regression test (T1.3).
1. **GATE** Pin half-duplex bridge contract in `docs/DESIGN.md` and plan § 2 + regression test for protocol-violation byte (T1.4).
1. Add `docs/DESIGN.md` to phase 4 file scope with named sections to rewrite (T2.1).
1. Decide: drop sync-gen tools or keep them. Either way resolve the § 1 vs § 5 internal contradiction (T2.4).
1. Add the descriptor-bypass tests + runtime `isawaitable(result)` check (T2.3).
1. Add async event/UI handler rejection contract + test (T2.2).
1. Add `prompt_and_wait` standalone test to test_rpc_fake (T2.5).
1. Rewrite cancellation section for honesty about handler-thread leaks (T2.6).
1. Add `try/except + self.close()` rollback in `PiPythonHarness.start()` + test (T2.7).
1. Add `_fatal_error` state + reader exception policy (T2.8).
1. Specify stdin-lock / `_stdin_closed` flag invariant (T2.9).
1. Pick a singleton Tier-3 finding per phase to land alongside that phase's work (T3.1 through T3.10).
1. Roll all Tier-4 polish into whichever phase touches the relevant code; no ceremony.

After steps 1-4 are in the *plan text*, the Agent-class co-design is the only remaining gate. The plan author flagged that gate; this synthesis confirms it but adds four more.

## Recommendation matrix (all tiers, one entry per finding)

Decisive verdict per finding for the plan-revision pass. "Lands" gives the natural commit/phase home: P0 = plan-text revision (no code), P1–P4 = the four rewrite phases per the plan's phase decomposition (P1 `tools.py`, P2 `server.py`, P3 `rpc.py`, P4 `harness.py`+cleanup+docs), "Docs" = `docs/DESIGN.md` (all Docs items land in P4 per T2.1).

Compact list form is intentional — the cells are too long for a table that respects the 150-col rule, and some contain `|` characters (Python union types, shell pipes) that confuse the table parser.

### Tier 1 — Gating (P0 — plan-text revision before coding)

- **T1.1** *(Lands: P0 + P3)* — Implement option (a): thread-local "in dispatch" marker around the hook call; `client.send()` raises `ReentrantRPCError` if set. Defer option (c) hybrid worker-pool to the event-bridge work, where it lands naturally with the notification-vs-decision split.
- **T1.2a** *(Lands: P0 + P3)* — Add `_pending_lock = threading.Lock()`. Snapshot via `list(values())` under lock in `_fail_pending`; release before iterating. Lock-order rule: do not hold `_pending_lock` across the response wait; acquire `_pending_lock` before `_send_lock` (pick one direction and document it).
- **T1.2b** *(Lands: P0 + P3)* — Replace `Queue(maxsize=1)` for `_pending` slots with `concurrent.futures.Future`. Drop the sentinel-wedge complexity entirely; `future.result(timeout=t)` gives the caller wait, `set_result` / `set_exception` give safe single-assignment.
- **T1.3** *(Lands: P0 + P2)* — Add `self.request.settimeout(self.timeout_ms/1000)` at the top of the bridge handler. Regression test: client opens TCP, sends partial bytes (no `\n`), assert handler returns within `timeout_ms` and no thread leak.
- **T1.4** *(Lands: P0 + P2 + Docs)* — Pin the half-duplex bridge contract in `docs/DESIGN.md` § Bridge protocol: "after the initial request frame, the client sends no further bytes until the connection is closed." Regression test: a protocol-violation byte is logged and the handler returns with a `protocol_violation` error. Refactor sidecar to `selectors.select([sock, cancel_pipe])` only if round-trip events ever need the same socket.

### Tier 2 — Should land in P3/P4 (the implementation phases that touch the relevant code)

- **T2.1** *(Lands: P4 + Docs)* — Add `docs/DESIGN.md` to phase 4 file scope. Rewrite §§ ToolRegistry, ToolContext, PiRpcClient, PiPythonHarness, Failure modes; update the architecture sketch at the top. Spot-check `CLAUDE.md` § Architecture for stale async references. Treat as merge-blocker, not follow-up.
- **T2.2** *(Lands: P3)* — Reject coroutine event/UI handlers at registration with a loud error pointing at the migration note. Regression test installs an `async def` event handler and asserts immediate `TypeError`.
- **T2.3** *(Lands: P1)* — Belt-and-braces async-tool detection: keep the decoration-time `iscoroutinefunction` / `isasyncgenfunction` check, and add a runtime `if inspect.isawaitable(result): raise ToolError(...)` inside `collect_tool_result` after the handler returns. Two tests: lambda-returning-coroutine and `functools.wraps`-of-async, both raise at execute time.
- **T2.4** *(Lands: P1 + Docs)* — Drop sync-generator tool support. Resolve the § 1 vs § 5 plan contradiction. Reject sync-gen handlers at registration with a remediation message. Update `docs/DESIGN.md` to remove generator examples.
- **T2.5** *(Lands: P3)* — Call out `prompt_and_wait` in the `rpc.py` change list. Add a standalone test in `test_rpc_fake.py`: deliver all events and return; bonus test where an event handler raises and `prompt_and_wait` still completes.
- **T2.6** *(Lands: P0 + P2 + Docs)* — Rewrite plan § 4 and test #8 to be honest about handler-thread leak semantics: uncooperative tool bodies remain on their handler threads until completion; `daemon_threads` is process-exit safety only, not in-process cancellation. Document as a known limitation in `docs/DESIGN.md` with the recommendation that long-running tools poll `ctx.cancelled`.
- **T2.7** *(Lands: P4)* — `PiPythonHarness.start()` wraps its sequence in `try: ... except: self.close(); raise`. Ensure `close()` is idempotent. Regression test: monkey-patch `PiRpcClient.start` to raise after `PythonToolServer.start()` succeeds; assert listener socket, server thread, and tempdir are all cleaned up.
- **T2.8** *(Lands: P3)* — Add a fatal-error slot of type `Exception or None` to `PiRpcClient`. Reader thread wraps the per-record dispatch in `try/except Exception` that logs, sets the slot, calls `_fail_pending(exc)`, and exits the loop. Future `send()` checks the slot at entry and raises immediately. Test: invalid JSON propagates to subsequent `send()` via `__cause__`.
- **T2.9** *(Lands: P0 + P3)* — Stdin invariant: every `proc.stdin.write` is `with _send_lock: if not _stdin_closed: ...`; `_stdin_closed` is set under the same lock by `close()` *before* `proc.stdin.close()`. Writers swallow the union of `BrokenPipeError`, `ValueError`, `OSError`. Regression test: concurrent close + hook-decision write asserts no exception escapes.

### Tier 3 — Singleton substantive (land alongside the relevant phase)

- **T3.1** *(Lands: P3)* — With T1.1 in place, the reader thread serializes hook dispatch, UI-request dispatch, and UI-response write atomically. No separate ordering fix needed. Add a regression test asserting the order: trigger a `get_state()` that causes fake-pi to emit `extension_ui_request`; assert fake-pi never sees a competing thread's `prompt` line before the `extension_ui_response`.
- **T3.2** *(Lands: P0 + P2)* — Fix plan wording: the wire surfaces are `ToolSpec.to_manifest()` and `ToolResult.to_wire()`, not `RegisteredTool` (internal-only). Add round-trip tests for each optional `ToolSpec` field (`label`, `promptSnippet`, `promptGuidelines`, `executionMode`) and non-default `ToolResult` field (`terminate`, image blocks, `details`).
- **T3.3** *(Lands: P2)* — Freeze `ToolRegistry` at `PythonToolServer.start()`. Reject `register()` after start with a clear error message. Regression test asserts late registration raises.
- **T3.4** *(Lands: P2)* — In `server.py`: `request.settimeout` (overlaps T1.3); counting wrapper on `recv` with max-bytes cap mirroring the current 8 MiB; `threading.Semaphore(16)` for max active handlers with `acquire(blocking=False)` and loud rejection on exhaustion. Regression test: 100-connection flood; assert connections beyond the cap are rejected and the server stays responsive.
- **T3.5** *(Lands: P0 + P1 + Docs)* — One-line clarification in plan § 5 *and* `docs/DESIGN.md` § ToolContext: "`ctx.update` is synchronous; blocks until the bridge write returns; no timeout." Slow-pi-consumer test (fake-pi delays its read) asserts `ctx.update` blocks rather than dropping the frame.
- **T3.6** *(Lands: P3 + Docs)* — Set `_events = queue.Queue(maxsize=4096)`. On full: log a warning and drop oldest. Document the consumer drain expectation in `docs/DESIGN.md`.
- **T3.7** *(Lands: P4)* — Add two commit-time assertions in the phase-4 commit that drops `pytest-asyncio`. First, `grep -r 'async def test_' tests/` returns no match. Second, `pytest --collect-only` test count is at least the prior baseline. Either failing fails the commit. Prevents silent green-by-skip.
- **T3.8** *(Lands: P0 + P3)* — Drop the raw-fd `os.read` choice. Use `proc.stdout.read(4096)` (the asyncio code's current pattern). Document the close sequence: `proc.stdin.close()` → reader thread observes EOF on its own (`read` returns empty bytes) → join reader → `proc.wait()`.
- **T3.9** *(Lands: P0 + P2)* — Make plan test #4 explicit about cross-talk assertion. Each tool emits N distinct sentinel updates (`tool_a` emits `a1..a10`, `tool_b` emits `b1..b10`); assert each shim connection observes only its own sentinels, in order.
- **T3.10** *(Lands: P0 + P3)* — Plan § 6 sequence diagram must include the UI-request branch with explicit ordering: an `on_event` handler for `extension_ui_request` observes the event *before* `_send_extension_ui_response` writes back. Unit test asserts this ordering.

### Tier 4 — Polish (roll into the relevant phase, no ceremony)

- **F.9** *(Lands: P2)* — Add `logger.info("tool %s ran to completion after cancellation, result discarded", tool_name)` in the handler's `finally` when `cancelled` is set and the final-response write failed with `BrokenPipeError`. Distinguishes silent leak from unrelated exception.
- **F.11** *(Lands: P1–P4)* — Each phase commit appends `pytest --collect-only` test count + a signature hash to `dev-notes/rewrite-bisect-baseline.md`. Bisect-by-test-count remains possible across the four-commit "green only at the end" branch.
- **F.12** *(Lands: P0)* — Add one sentence to plan § 3 or § 6: "events go both to `_events` and to `_event_handlers` callbacks on the reader thread; consumers observe events independently — preserved from asyncio."
- **F.1** *(Lands: P0)* — Pick `_send_lock` (already used in three of the four plan occurrences). Substitute globally; remove `_stdin_lock`.
- **A.6** *(Lands: P0)* — Acknowledge in plan § 2: sidecar `join(timeout=0.1)` is best-effort cleanup; the sidecar unblocks when `socketserver` closes the socket *after* `handle()` returns, so the join in `finally` is mostly racing the close. Not a leak under `daemon_threads`, just a doc honesty fix.
- **C.10** *(Lands: P4)* — Declare restart explicitly unsupported. `harness.close()` then `harness.start()` raises `RuntimeError("harness cannot be restarted; create a new instance")`. Update plan's test #7 accordingly.
- **E.m1** *(Lands: P0)* — Add caption to plan's LOC table: "estimates ±25%, not load-bearing for scope decisions."
- **E.m2** *(Lands: P0)* — Plan § 2 cleanup section adds: "explicit handler-thread join happens on clean shutdown; `daemon_threads` is the safety net for the misbehaving-tool case only. With `keep_temp=True`, half-written frames at interpreter shutdown may be observable in retained artifacts."
- **E.m3** *(Lands: P1)* — Add `tests/pi/test_runtime_meta.py::test_runtime_is_what_we_think` recording `sys.version_info` plus `sys._is_gil_enabled()` to a CI artifact, so `make test-ft` and `make test-311` provably run on different runtimes.

### What this matrix implies for the plan-revision commit

Counting by landing target:

- **P0 (plan-text only, no code):** 17 items. This is the revised plan doc. About a half-day of editing once the Tier 1 decisions are reaffirmed.
- **P1 (`tools.py`):** 4 items.
- **P2 (`server.py`):** 7 items.
- **P3 (`rpc.py`):** 9 items.
- **P4 (`harness.py` + cleanup):** 6 items.
- **Docs (`docs/DESIGN.md` + `CLAUDE.md` spot-check):** 6 items, all land in the P4 commit per T2.1.

The P0 work — the plan revision itself — is the gate. Once that lands, the implementation phases are mechanical against the matrix.

## Notes on the review-route comparison

Six reviews across three orchestration routes (codex direct, libharness→pi→openai-codex, libharness→pi→openrouter) and three model families (gpt-5.4/5.5/mistral-large/opus-4-7) is a useful data set in its own right.

**Quality ranking (substantive new-finding density × accuracy):**

1. **E (Opus subagent, generalist) — highest density and highest novelty.** Identified C1 (sidecar bytes-stealing) and C4 (handler timeout regression), neither of which any other reviewer caught. Cited evidence specifically. Severity calibration was strict.
1. **A (codex/gpt-5.4 xhigh, concurrency) — tied for most CRITICAL findings, all confirmed by at least one other reviewer.** Most narrowly focused; angle specialization paid off.
1. **B (codex/gpt-5.4 xhigh, API/contract) — sharp on the descriptor-bypass attack on async-def rejection.** The empirical "I verified `registry.register(lambda ...: af(...))` and `registry.register(C.__dict__["sm"])` both currently register cleanly" sentence is the kind of verification work other reviewers didn't do.
1. **C (libharness→pi→gpt-5.5 xhigh, generalist) — solid corroboration of A's CRITICALs + three new substantive findings (T3.3, T3.4, restart contract).** The libharness→pi route worked exactly as a codex-direct route would.
1. **F (Opus subagent, generalist) — 12 findings, all real, but severity-discounted vs E.** F's "ready with changes" verdict on the same evidence E rated CRITICAL is the most consequential model-disagreement in the set. F added 4 new substantive moderates (T2.9, T3.5, plus several polish items) the other reviewers missed.
1. **D (libharness→pi→OR/mistral-large, generalist) — 6 of 10 findings were misreadings.** D criticized the plan for omitting content the plan explicitly addresses (StrictJsonlDecoder thread-safety, `daemon_threads`, `allow_reuse_address`, decoration-time async rejection). The 4 valid findings (T3.6 unbounded `_events` is the best) were all MINOR-grade. **Mistral Large is not a useful reviewer at this scope** — it didn't read the plan carefully before flagging.

**Practical takeaway for the libharness review workflow:** Opus subagents + codex xhigh are roughly equivalent quality. Mixing both is worth the cost — they find non-overlapping things. Mistral Large is not worth the run for plan-review work; it's a content model, not a critical-reader model. xAI Grok 4.3 wasn't tested for this workload but the prior empirical data (offensive-joke compliance) suggests it would be similarly content-focused.

**The libharness→pi→openai-codex route is production-grade for review workloads.** C and the equivalent codex-direct route produced comparable results, which validates that the threading rewrite's eventual users can drive real review work through libharness without losing fidelity.
