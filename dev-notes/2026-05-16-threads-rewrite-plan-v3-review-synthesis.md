---
status: In co-design
created: '2026-05-16'
---

# Threads-rewrite plan v3 review — synthesis

Consolidated findings from six independent adversarial reviews of `dev-notes/2026-05-15-threads-rewrite-plan.md` (v3). Third in the series after the v1 synthesis (`dev-notes/2026-05-15-threads-rewrite-plan-review-synthesis.md`) and the v2 synthesis (`dev-notes/2026-05-16-threads-rewrite-plan-v2-review-synthesis.md`). Ordered by severity × corroboration count, with recommendations.

## Reviewer roster

Six independent passes, all on codex direct / gpt-5.5 / xhigh.

| ID  | Scope                 | Verdict                                          |
| --- | --------------------- | ------------------------------------------------ |
| G1  | generalist            | not ready; targeted v4 amendments                |
| G2  | generalist            | not ready; focused v4 pass before implementation |
| S1  | concurrency / locking | ready with changes, not as written               |
| S2  | API / migration       | not complete on public API accounting            |
| S3  | lifecycle / cleanup   | not lifecycle-ready as written                   |
| S4  | tests / CI            | not adequate yet; much stronger than v2          |

**Consensus verdict.** 6 of 6 say "not ready as written, v4 needed." But — and this matters — every reviewer used "targeted amendments" or "focused v4 pass," not "rewrite." The findings are tighter and more surgical than v2's. v3 closed v2's load-bearing issues but introduced second-order problems where the fixes themselves have subtle races.

**Comparison to v2 review:** v2 had 14 reviewers and 12 said "not ready"; v3 has 6 reviewers and 6 say "not ready." Same percentage (100%) but the v2 findings were "this whole fix is wrong" while v3 findings are "this fix has a smaller window race." This is the expected pattern of successive review rounds — diminishing severity per round, ever-more-specific findings.

## Tier 1 — Gating findings (CRITICAL, 3+ reviewers)

Five issues. All in code-level concurrency design where v3's fixes have residual races.

### T1.1-v3 UI reservation has a post-gate race

**Corroboration:** G1, G2, S1 — three independent CRITICAL.

**The bug.** v3's reservation pattern (`_ui_response_pending: threading.Event` + condition, set by reader before dispatching UI event) blocks ordinary `send()` only **while the flag is set**. A caller that passed the gate while it was clear, then stalled before acquiring `_send_lock`, can still win `_send_lock` after the reader sets the flag — putting `prompt` on stdin before `extension_ui_response`. The race window is narrower than v2's, but the same wire-ordering violation.

**Citations:** `plan.md:614,627,642,233`, `tests/pi/fake_pi_rpc.py:18`, `src/libharness/pi/rpc.py:216,366,366`.

**Options:**

- *(a) Writer-priority thread.* Single writer thread + priority queue; UI responses preempt normal writes structurally. Closes the race fully. Adds a thread + queue + failure modes.
- *(b) Write-admission protocol that revokes admitted writers.* On UI request, reader sets flag *and* signals any admitted-but-not-yet-written caller to retry the gate. Requires `send()` to recheck after lock acquisition, before `_write`. ~10 lines.
- *(c) Hold `_send_lock` continuously through UI cycle.* Reader acquires `_send_lock` when it sees `extension_ui_request`, doesn't release until UI response written. Stalls all writers for arbitrary UI-handler duration but trivially correct.

**Recommendation: (b) write-admission with recheck.** Cheapest fix, closes the race, doesn't introduce new threads or stall the whole writer surface on slow UI handlers. Concretely: `send()` after acquiring `_send_lock` rechecks `_ui_response_pending`; if set, releases the lock, waits on the condition again, then retries. Add the barrier test G1/G2/S1 all asked for: pause a normal `send()` post-gate, inject UI request, assert UI response wins on the wire.

### T1.2-v3 `_settle_future` ownership semantics inconsistent

**Corroboration:** G1, G2, S1 — three independent CRITICAL.

**The bug.** v3 defines `_settle_future(req_id, ...)` as "pop under `_pending_lock` then settle." But the cleanup section says `_fail_pending` **snapshot-clears `_pending` and then settles via `_settle_future`**. If implemented literally, every `_settle_future` call after the clear finds no `req_id` in `_pending` and returns without completing the captured futures. The `_set_fatal` pseudocode separately settles the snapshot directly, so v3 has two incompatible recipes.

**Citations:** `plan.md:16,225,441,747`, `synthesis v2:84`.

**Options:**

- *(a) Split into two helpers.* `_pop_pending(req_id) → Future | None` for response path; `_complete_future(fut, value=None, exc=None)` for already-owned futures (used by `_fail_pending` after snapshot-clear).
- *(b) Make `_settle_future` accept an already-owned Future.* `_settle_future(fut_or_id, ...)` with isinstance check.
- *(c) Inline the snapshot-clear settlement.* Remove `_settle_future` from `_fail_pending`; do the loop directly there.

**Recommendation: (a) split into two helpers.** Cleanest naming, no overload polymorphism, matches actual usage: response path needs pop-by-id, fail/close path has already-owned future. Eliminates the ambiguity.

### T1.X-v3 Lifecycle close-during-start race

**Corroboration:** G2, S1, S3 — three independent CRITICAL.

**The bug.** v3 releases `_lifecycle_lock` while `start()` performs multi-step I/O (server bind → tempdir → shim write → faux provider → `PiRpcClient.start`). `close()` is allowed to transition `"starting" → "closing"` and run cleanup while the original `start()` thread is still creating resources. The cleanup can remove the tempdir/server while start continues, then `start()` may transition to `"started"` after `close()` "completed." Tests cover concurrent close/close and start/start but not start/close.

**Citations:** `plan.md:247-248,758,767,820`, `harness.py:65-105`.

**Options:**

- *(a) Hold `_lifecycle_lock` for the whole start sequence.* Trivially correct; serializes all close attempts behind a multi-second start window. Bad for "I want to abort a hanging start."
- *(b) Set an abort flag in `_lifecycle_state` ("aborting"); checked after each start cut point.* `start()` checks after every step; if aborted, transitions to `"failed"` and lets `close()` finish. `close()` during `"starting"` sets the abort flag, waits on a `_start_complete` event, then proceeds.
- *(c) Cancel via subprocess/socket close.* `close()` during `"starting"` terminates the partial pi process and closes the server socket to unblock any pending start operation, then waits for `start()` to fail naturally.

**Recommendation: (b) abort flag + `_start_complete` event.** Lets close during a slow start be responsive (don't wait minutes) while still serializing the actual teardown sequence. Concrete: add `_start_complete: threading.Event`, set at end of `start()` whether success or failure. `close()` during `"starting"`: set `_lifecycle_state = "aborting"`, release lock, `_start_complete.wait(timeout)`, then proceed with cleanup. Add barrier tests at each cut point with concurrent `close()` per G2/S1/S3.

### T1.X-v3 `close()` doesn't check `_closing` in `_pending` insertion critical section

**Corroboration:** G2 CRITICAL; reinforced by S3's "close() returns with callbacks still running" (HIGH).

**The bug.** v3's atomic critical section in `send()` checks `_fatal_error` but not `_closing`. If `close()` sets `_closing` (lock-free), `_fail_pending` runs, then a `send()` already past the gate inserts a new future — `_send()` returns silently because `_closing.is_set()` was checked outside the lock, the future is never written, and the caller blocks until `request_timeout`.

**Citations:** `plan.md:283,292,295,619`, `rpc.py:229`.

**Options:**

- *(a) Add `_closing` check to the atomic critical section.* `with self._pending_lock: if self._fatal_error or self._closing.is_set(): raise; self._pending[id] = fut`. Closes the race; requires `close()` to acquire `_pending_lock` to set `_closing` (or accept the race that a `send()` racing exactly with `close()` sees stale `_closing`).
- *(b) Set `_fatal_error` instead of just `_closing` when close starts.* Reuses the existing fatal-check path. `close()` sets `_fatal_error = PiRpcProcessError("client closed")` under `_pending_lock`, then proceeds.

**Recommendation: (b) set fatal on close.** Symmetric with reader-thread fatal handling, no second flag to coordinate. Already requires `_pending_lock` (which close needed to take anyway for `_fail_pending`). Document that "closed" is a kind of fatal state from the caller's POV.

### T1.X-v3 `_stdin_closed` set outside `_send_lock` in fallback path

**Corroboration:** S1 CRITICAL, S3 HIGH.

**The bug.** v3's `close()` pseudocode acquires `_send_lock` with `timeout=1.0`; if the acquire times out (because a writer is blocked in `stdin.write/flush`), it sets `_stdin_closed = True` and calls `proc.stdin.close()` **anyway**. This violates the documented stdin invariant ("`_stdin_closed` is set under `_send_lock`"). The blocked writer is still inside `write/flush` on a file object the close just torched.

**Citations:** `plan.md:280-317,361`, `rpc.py:183`.

**Options:**

- *(a) On lock-acquire timeout, terminate the subprocess first.* SIGTERM/SIGKILL unblocks the writer's `write/flush` with `BrokenPipeError`; writer releases `_send_lock`; close acquires the lock and proceeds.
- *(b) Skip stdin close entirely on timeout.* If close can't acquire the lock, go straight to subprocess termination; never mutate `_stdin_closed` outside the lock.

**Recommendation: (a) terminate first, then acquire.** Preserves the invariant. Documented order: `_closing.set()` → `_fail_pending` → try `_send_lock.acquire(timeout=1)`; on timeout → `proc.terminate()` → `proc.wait(timeout=1)` → acquire `_send_lock` (now succeeds because writer got `BrokenPipeError`) → set `_stdin_closed` → close stdin → release.

## Tier 2 — Significant (HIGH/MODERATE, 2+ reviewers or single HIGH)

### `_write` swallows OSError on normal request path

**Corroboration:** G1, G2 MODERATE.

v3's `_send()` swallows `BrokenPipeError | ValueError | OSError`, returns silently. `send()` then waits on `fut.result(timeout=request_timeout)` and times out instead of failing immediately. Current asyncio code raises `PiRpcProcessError` directly on stdin write failure.

**Recommendation:** Normal request write path returns success/failure or raises `PiRpcProcessError` immediately; only best-effort close/UI cleanup paths swallow. `send()` rollback path (the current `except Exception` clause) becomes specific to write failures, leaving wait-on-future cleanly separate.

### `close()` lacks guaranteed `finally` completion

**Corroboration:** S3 HIGH.

v3 says cleanup transitions to `"closed"` and sets `_close_complete`, but doesn't require this in `finally`. If `pi.close()`, `server.close()`, or tempdir cleanup raises, the harness can remain `"closing"` forever and a second `close()` waits indefinitely.

**Recommendation:** `close()` body wrapped in `try: ... finally: self._lifecycle_state = "closed"; self._close_complete.set()`. Cleanup errors logged but not raised; specific cleanup-error policy documented. Test: monkeypatch each cleanup step to raise; double-close still returns; context-body exceptions not lost.

### `close()` returns with reader-thread user code still running

**Corroboration:** S3 HIGH.

Reader thread hosts event/UI handlers. v3 joins reader threads with a timeout but doesn't ensure no callbacks fire after close returns. A final pi message racing close can enter a slow handler; close returns and cleans resources while user callback is still executing.

**Recommendation:** Set `_closing` in the reader's dispatch path too — `_dispatch_event` and `_handle_extension_ui_request` early-return if `_closing.is_set()`. Document the close postcondition: "after `close()` returns, no callbacks fire." Test: blocked event handler + concurrent close + assert no handler invocations after close returns.

### Public API exports still incomplete

**Corroboration:** S2 HIGH.

v3's `__init__.py` bullet names the new exception hierarchy but doesn't address existing exports: `PiLaunchConfig`, `BridgeEndpoint`, `ToolError`, `ToolSpec`. Dropping any of these is an undocumented break; preserving them without naming them leaves the migration contract ambiguous.

**Recommendation:** Make the v3 plan explicit about the final `__all__`. Either preserve every existing export (and name them) or explicitly remove with migration guidance.

### Async tool body breaking change missing from migration table

**Corroboration:** S2 HIGH.

The migration table has 12 rows; row 7 covers sync-generator tools, but `async def` tools and sync wrappers returning awaitables are not their own row. They're implicit in the runtime guard description but a user grepping the table for "async tool" won't find them.

**Recommendation:** Add a distinct migration row: "async tool body / async-gen tool body / sync tool returning awaitable → all rejected; migrate to sync `def` returning `ToolResult` or using `ctx.update`."

### `PythonToolServer` direct-use breaking changes missing from migration table

**Corroboration:** S2 HIGH.

`PythonToolServer` is publicly exported. It currently has `async start()`, `async close()`, async context manager. Breaking-change table covers `PiRpcClient` and `PiPythonHarness` but not direct `PythonToolServer` users.

**Recommendation:** Add migration rows for `await server.start()/close()` → `server.start()/close()` and `async with PythonToolServer` → `with PythonToolServer`. Or explicitly de-publicize direct server use with a deprecation note.

### `ToolRegistry` live registry remains unlocked under FT

**Corroboration:** G1, G2, S1 MODERATE × 3.

v3's snapshot-not-freeze fixes server-side races but the caller-owned `ToolRegistry._tools` is still an unlocked dict. Late `register()` is now legal locally — and a caller can mutate while `PythonToolServer.start()` is snapshotting on another thread. On 3.14t this is the same FT race the rewrite was supposed to remove, just moved one layer out.

**Recommendation:** Add `_registry_lock` inside `ToolRegistry`; `register()`, `get()`, `manifest()`, `snapshot()` all acquire. The lock is fine-grained (one Python object) and doesn't block server operations (snapshot copies under lock then releases). Test: concurrent `register()` + `snapshot()` on 3.14t.

### `max_handlers` design contradicts itself

**Corroboration:** G2, S1, S4 MODERATE × 3.

Phase 2 description says semaphore acquired at handler entry (after thread spawn). Rejected-alternatives table says v3 "moves gate to `process_request` so threads aren't spawned past cap." Different DoS behavior, different test claim.

**Recommendation:** Pick one and update the plan consistently. Codex's v3 (which we cross-read during the v3 revision) chose `process_request` override for pre-thread gating; my v3 should adopt that — it actually delivers on the "bounds thread creation" claim that the rejected-alternatives section asserts. Update phase 2 description + tradeoffs + test (assert handler-thread count, not just rejection count).

### Helper-thread bypass test design is fragile

**Corroboration:** G1, S1, S4 MODERATE × 3.

`pytest.mark.timeout(5)` is a **failure** mechanism, not a passing assertion. A test that "asserts hangs within the timeout window" either fails the suite (when timeout fires) or leaves stuck subprocess/reader state. The synthesis recommended subprocess-based testing; v3 said `pytest.mark.timeout`. Wrong primitive.

**Recommendation:** Move to subprocess + `subprocess.run([sys.executable, "-c", script], timeout=N)`; assert `subprocess.TimeoutExpired` is raised. Outside-process, kill on timeout, no suite contamination. Or replace with a docs-only contract test (a test that just imports `ReentrantRPCError` and asserts its docstring mentions helper-thread case).

### `send()` rollback scoped too broadly

**Corroboration:** S3 MEDIUM.

v3's `except Exception` wraps both `_write()` AND `fut.result(timeout=...)`. Timeouts, fatal-during-wait, and command failures during wait are all routed through `_settle_future`, obscuring cause and racing with legitimate future settlement.

**Recommendation:** Split write failure rollback from wait handling. Pseudo-code:

```text
insert future
try: write
except WriteError: pop+fail; raise
try: return fut.result(timeout)
except TimeoutError: pop+cancel; raise
```

Add tests for timeout, fatal during wait, late response after caller timeout.

## Tier 3 — Single MODERATE / pattern issues

### Event handler runtime guard ignores generator + non-None returns

**Corroboration:** G1 MINOR, S2 MEDIUM.

Sync generator event handler returns a generator object → silently ignored. Event handlers are effectively `None`-returning callbacks; safest contract is to reject any non-`None` return.

**Recommendation:** After handler invocation: reject `inspect.isgenerator(result)` (consistent with tool path); optionally reject any non-`None` result with a warning. Tests for generator-returning event/UI handlers.

### Dual-delivery ordering unspecified

**Corroboration:** S1 MODERATE.

v3 says events go to both `_events` queue and handlers, but doesn't say which fires first. Under threads, `_put_event` first → queue consumer can observe event before handlers finish; handlers first → slow handler delays queue consumers.

**Recommendation:** Pin the order in the plan and `docs/DESIGN.md`. Pick "queue first, then handlers" (a queue consumer should see the event no later than a handler does). Test with blocking handler + concurrent `next_event()`.

### Slowloris test mismatched to resource-bound claim

**Corroboration:** S1, S4 MEDIUM.

Same as the `max_handlers` design contradiction above — if v3 claims pre-thread gating, the test should assert handler-thread count, not just rejection count.

**Recommendation:** Update test #15 (in v3 § 10) to track peak handler-thread count via the server-owned counter and assert it never exceeds `max_handlers`.

### Async iterables bypass guard

**Corroboration:** S2 MEDIUM.

`inspect.isasyncgen` catches async-generator objects (`async def f(): yield`). It does NOT catch arbitrary async iterators with `__aiter__` / `__anext__`. A class implementing those returned from a sync wrapper bypasses both the awaitable and the async-gen checks.

**Recommendation:** Extend the runtime guard to also reject `collections.abc.AsyncIterable` / `AsyncIterator` instances. One additional check, no per-type complexity.

### `ctx.update` "no timeout" is a DoS contract

**Corroboration:** S2 MEDIUM.

Slow or malicious bridge consumer can pin every handler until `Semaphore(max_handlers)` is exhausted. v3 documents "no timeout" but doesn't flag this as a security/availability concern.

**Recommendation:** Add a configurable `bridge_write_timeout` (default `None` = current behavior, but configurable). Document in `docs/DESIGN.md` § Security/limits: "default `ctx.update` is unbounded; under untrusted shim, set `bridge_write_timeout`."

### Async-test migration guard inconsistent

**Corroboration:** S2 MEDIUM.

Plan adds both a Makefile grep guard AND an AST guard test. The grep catches only `async def test_*`. The AST guard catches async fixtures, `pytest.mark.asyncio`, and `pytest_asyncio` imports too.

**Recommendation:** Drop the Makefile grep guard; rely on the AST test as the single machine-enforced gate. Run it as part of `make all` so it gates every commit, not just phase-4.

### Event queue payload + timeout contract not pinned

**Corroboration:** S2 LOW.

`asyncio.Queue.get(timeout=...)` raises `asyncio.TimeoutError`; `queue.Queue.get(timeout=...)` raises `queue.Empty`. Different exception class. Plan doesn't say whether to wrap or leak.

**Recommendation:** Wrap in `PiRpcError` subclass (e.g. `EventQueueEmpty(PiRpcError)`) so the public API doesn't leak stdlib internals. Or document explicitly that `queue.Empty` is the new exception (cheap, mostly equivalent).

### Bisect-baseline signature not enforced

**Corroboration:** G2 MINOR, S4 MEDIUM.

Synthesis required `<commit>\t<count>\t<sha256-of-sorted-nodeids>`. v3's Makefile guard checks count but not signature. Same-count test deletion+addition passes silently.

**Recommendation:** Makefile guard computes current count + signature; asserts both match last row.

### Bisect-baseline append unenforced

**Corroboration:** S4 MEDIUM.

Plan says each phase commit appends a row, but no CI/precommit checks "was the file updated this commit?" A commit that forgets to append silently passes.

**Recommendation:** Pre-commit hook on phase branches: `git diff --cached --name-only | grep -q rewrite-bisect-baseline.md` OR CI check `git log --pretty=format: --name-only HEAD~1..HEAD | grep -q rewrite-bisect-baseline.md`.

### Partial-start tests miss realistic failure modes

**Corroboration:** S3 MEDIUM.

Monkeypatching `PiRpcClient.start` to raise proves rollback called but not that a client with `Popen` already assigned and reader threads already started is cleaned up. Realistic modes: missing pi binary, fake-pi exits during startup after pipes/threads exist, startup handshake timeout, server bind failure.

**Recommendation:** Expand parametrized partial-start test with real failure injections, not just monkey-patches. Assert process death, reader-thread exit, server close, and tempdir cleanup for each.

### Runtime-meta not enforced by CI

**Corroboration:** S4 MEDIUM.

Artifact is written but no Makefile target compares 3.11 vs 3.14t outputs.

**Recommendation:** `make threads-runtime-meta-check` target that runs both lanes, compares `.pytest-runtime-meta/*.json`, asserts 3.14t has GIL disabled and 3.11 has GIL enabled. Add to `make all-both-venvs` (or wherever the cross-venv targets live).

### Bridge shutdown promises without mechanism

**Corroboration:** S3 MEDIUM.

Plan says "explicit handler join with timeout for orderly shutdown" but `ThreadingTCPServer` doesn't track handler threads unless the subclass does so.

**Recommendation:** Server subclass maintains `_active_handler_threads: set[threading.Thread]`, populated in `process_request_thread` start, removed in `finally`. `close()` snapshots the set and joins each with timeout.

## Tier 4 — Polish (single MINOR)

- *Thread-leak assertion flaky* (S4): `threading.enumerate()` filtered by names races daemon handlers/sidecars/watchdog. Prefer server-owned counters/events: active handler count returns to baseline, watcher count returns to baseline, close completes within timeout.
- *Phase-4 guard weaker than AST guard* (S4): Phase-4 Makefile guard's `grep -rq 'async def test_'` is narrower than the AST test. Drop the grep guard (covered by AST).
- *T1.3 timeout floor regression test* (S4): v3 restores `max(1.0, timeout_ms/1000)` but doesn't test it. Add `test_server_initial_frame_timeout_floor` with `timeout_ms=10`.
- *send() rollback split* — see Tier 2 above; explicitly separate write-failure rollback from wait-handling.

## Recommendations matrix

Decisive per-finding actions. ✅ = pull into v4 plan-text revision; ⚠️ = defer to phase-1 with documented assumption; ❌ = explicitly out-of-scope.

### Tier 1 GATEs (v4 plan-text required)

- ✅ **T1.1-v3** — implement (b) write-admission with recheck. `send()` rechecks `_ui_response_pending` after acquiring `_send_lock`; if set, releases and waits again. Add the barrier test.
- ✅ **T1.2-v3** — split `_settle_future` into `_pop_pending(req_id) → Future | None` + `_complete_future(fut, value=None, exc=None)`. Update all call sites.
- ✅ **Lifecycle close-during-start** — implement (b) abort flag + `_start_complete` event. Add barrier tests at each cut point with concurrent `close()`.
- ✅ **`close()` doesn't check `_closing` in atomic insertion** — implement (b) set `_fatal_error = PiRpcProcessError("client closed")` on close start. Reuse existing fatal-check path.
- ✅ **`_stdin_closed` outside `_send_lock` in fallback** — terminate subprocess before final lock acquisition; never mutate `_stdin_closed` outside the lock.

### Tier 2 (v4 plan-text recommended)

- ✅ `_write` swallows OSError — normal request path raises immediately; swallow only on close/UI cleanup.
- ✅ `close()` guaranteed finally completion — wrap in `try/finally`.
- ✅ `close()` returns with callbacks running — check `_closing` in dispatch; document "no callbacks after close returns."
- ✅ Public API exports — explicit `__all__` in plan.
- ✅ Async tool body migration row — add to migration table.
- ✅ `PythonToolServer` migration rows — add to migration table.
- ✅ `ToolRegistry` live lock — `_registry_lock` for FT correctness.
- ✅ `max_handlers` design — pick `process_request` override; update phase 2 + tradeoffs + tests consistently.
- ✅ Helper-thread bypass test — subprocess-based.
- ✅ `send()` rollback split — separate write-failure from wait-handling.

### Tier 3 (v4 plan-text or phase 1 — small scope)

- ✅ Event handler generator/non-None return — add to runtime guard.
- ✅ Dual-delivery ordering — pin "queue first, then handlers."
- ✅ Async iterables in guard — add `AsyncIterable` rejection.
- ⚠️ `ctx.update` DoS contract — add `bridge_write_timeout` config + security note in `docs/DESIGN.md`. Default unbounded (preserves current behavior); users opt in.
- ✅ Async-test migration guard unify — drop grep, keep AST.
- ✅ Event queue timeout exception — wrap in `EventQueueEmpty(PiRpcError)`.
- ✅ Bisect-baseline signature enforced — Makefile guard computes + compares both count and signature.
- ✅ Bisect-baseline append enforced — pre-commit/CI check.
- ⚠️ Partial-start tests realistic failure modes — expand during phase 4 implementation; tests evolve with concrete code.
- ✅ Runtime-meta CI enforcement — `make threads-runtime-meta-check` target.
- ✅ Bridge shutdown mechanism — concrete server subclass with `_active_handler_threads` set.
- ⚠️ Slowloris test mismatched — fixed by the `max_handlers` design pick above.

### Tier 4 (polish, fold into phase work without ceremony)

- ⚠️ Thread-leak assertion — server-owned counters during phase 2 implementation.
- ⚠️ Phase-4 guard weaker than AST — drop grep, keep AST. One-line plan edit.
- ⚠️ T1.3 timeout floor test — add to test list.

## Path forward — recommendation

**Reach v4. Then stop adversarial-review-cycling and start phase 1.**

The reviewer findings have converged. v3's CRITICALs are all "this specific race in this specific helper" — they're surgical, code-level, no longer "rethink the design." v4 with the Tier 1 + Tier 2 amendments will resolve every named issue.

Further review rounds on v4 would likely surface Tier 3-equivalent findings (smaller windows, more specific edge cases). The marginal value drops; the cost (synthesis time, your attention) doesn't. The author flagged at v2 review time that "the threads rewrite plan is unblocked once Tier 1 GATE items are resolved"; v3's GATE items are mostly resolved with new smaller GATE items in their place.

**Concrete next step:** single v4 plan-text pass folding in all Tier 1 + Tier 2 + most of Tier 3. Quick author confirm on the five Tier-1 design choices. Then phase 1 implementation.

**Scope estimate for v4:** ~half day of focused editing, given the matrix above is decisive. Implementation phases unchanged from v3. Phase 1 unblocks once v4 lands and author signs off.

Alternative path if you want to ship faster: skip v4 and start phase 1 directly, treating the Tier 1 findings as **implementation notes** rather than plan-blockers. Risk: phase 1 code may need restructuring if a Tier 1 finding turns out to be a real bug (vs the "this might be wrong under a race I can describe in prose" pattern). My read of the findings: at least 3 of 5 Tier 1 issues are real bugs that **will** manifest in phase 1 testing (T1.1, T1.2, close-during-start). The other 2 (`_closing` in atomic insertion, `_stdin_closed` outside lock) might not manifest under normal usage but will under stress tests.

I recommend v4. The reviewer signal is consistent across two synthesis rounds (v2 and v3) that the dispatch / future / lifecycle territory is high-risk; pinning the contracts in plan text before code is cheaper than fixing the code after the fact.
