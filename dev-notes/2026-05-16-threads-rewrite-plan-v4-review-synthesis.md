---
status: In co-design
created: '2026-05-16'
---

# Threads-rewrite plan v4 review — synthesis

Consolidated findings from six independent adversarial reviews of `dev-notes/2026-05-15-threads-rewrite-plan.md` (v4). Fourth in the series. Ordered by severity × corroboration count, with recommendations.

## Reviewer roster

Six independent passes, all on codex direct / gpt-5.5 / xhigh.

| ID  | Scope                 | Verdict                                                     |
| --- | --------------------- | ----------------------------------------------------------- |
| G1  | generalist            | not ready; v5 needed                                        |
| G2  | generalist            | not ready; v5-level edit before implementation              |
| S1  | concurrency / locking | not ready as designed; another focused concurrency pass     |
| S2  | API / migration       | not ready for API/migration completeness                    |
| S3  | lifecycle / cleanup   | substantially stronger than v2, still not lifecycle-correct |
| S4  | tests / CI            | not adequate; executable-spec gaps remain                   |

**Honest assessment.** The user set a win condition: "you win if they find almost no fault in v4." **I lost.** 6 of 6 reviewers say not ready. v4 closed three of v3's five Tier-1 races (T1.2 future split, T1.4 fatal under lock, T1.5 stdin-closed invariant) but introduced four new bugs (T1.1 layered locking is actively worse than v3, `bridge_write_timeout` socket-shared race, "no callbacks after close" postcondition is unimplementable, active-handler tracking populated too late). Net progress over v3 is marginal — fewer Tier 1 bugs but the new ones are equally severe.

Pattern across rounds: each revision fixes the previously-flagged Tier 1 bugs and introduces new Tier 1 bugs in the fix mechanism. This suggests planning has diminishing returns; the remaining problems are at the granularity that implementation review (not plan review) catches.

## Tier 1 — Gating findings (CRITICAL, 3+ reviewers)

Four issues. All in code-level concurrency design where v4's *new* code has bugs.

### T1.1-v4 UI ordering: layered locking is worse than v3's reservation

**Corroboration:** G1, G2, S1 — three independent CRITICAL.

**The bug.** v4 has caller-thread `send()` acquire `_ui_response_condition` BEFORE `_send_lock`, with the recheck inside. This is *strictly worse* than v3 because:

1. A caller acquires `_ui_response_condition`.
1. Reader receives `extension_ui_request`, tries to acquire `_ui_response_condition` to set the flag, blocks.
1. Caller acquires `_send_lock`, writes a normal request to stdin.
1. Caller releases everything.
1. Reader finally acquires the condition, sets the flag, writes UI response.

The wire sees a normal request before the UI response. The reader is starved by the caller it was trying to gate.

The synthesis recommendation was specific and literal: *"`send()` acquires `_send_lock`, then rechecks `_ui_response_pending`. If set, releases `_send_lock`, waits on the condition, retries."* I deviated to "layered locking" thinking it was equivalent. It wasn't — and the deviation is what introduced the new race.

**Citations:** `plan.md:608,852,875` vs `synthesis v3:45`.

**Recommendation:** follow the synthesis literally for v5. `send()` acquires `_send_lock` first. Re-checks `_ui_response_pending` under it. If set, releases `_send_lock`, waits on `_ui_response_condition`, retries. Reader's UI-response write path bypasses the condition (already does in v4). Reader does NOT need to acquire `_ui_response_condition` to set the flag — `threading.Event.set()` is lock-free. (The condition is used only for caller-thread wait/notify.) The barrier test the reviewers all asked for: reader has decoded `extension_ui_request` but is paused before flag-set; assert no normal request reaches stdin before the UI response. This time, write the pseudocode and test concretely, don't paraphrase.

### T1.2-v4 close-during-start timeout reintroduces the v3 race

**Corroboration:** G1, G2, S1, S3 — four independent CRITICAL.

**The bug.** v4 makes `close()` wait up to 10 seconds for `_start_complete`, then proceeds with cleanup regardless. If `start()` is blocked longer than 10s (slow `PiRpcClient.start`, port-bind contention, slow shim write to network filesystem), cleanup runs *concurrently* with start — the exact v3 bug v4 was meant to fix. Worse: `start()` checks for `_lifecycle_state == "aborting"` after each cut point, but if `close()` has already transitioned to `"closed"`, the check misses the abort and `start()` proceeds to finalize against a torn-down harness.

**Citations:** `plan.md:986,996` vs `synthesis v3:67`.

**Recommendation:** `close()` must not run cleanup until `start()` has observed the abort and stopped touching shared resources. Options:

- *(a) Unbounded wait with active unblock.* `close()` actively cancels in-flight start operations (terminate partial pi process, close server socket to unblock bind, etc.), then waits unbounded on `_start_complete`. Always safe; can hang if a start step has no unblock mechanism.
- *(b) Make every start cut point bounded.* Each step has its own timeout (e.g., `PiRpcClient.start` already has `startup_timeout`); `start()` itself becomes bounded; close can safely wait the sum.
- *(c) Weaken contract: close-during-start is best-effort.* Document that close during `"starting"` may leak resources if start is blocked. Update docs.

**Recommendation: (a) with documented unblock mechanisms.** For v5: list every start cut point's unblock signal (subprocess.terminate, server socket close, tempdir.cleanup) and have `close()` apply them in order before waiting on `_start_complete`. Then the wait is *bounded by the cut-point timeouts*, not by an arbitrary 10s.

### T1.3-v4 "no callbacks after close returns" is unimplementable

**Corroboration:** G1, S1, S3 — three independent (G1 MODERATE, S1+S3 CRITICAL).

**The bug.** v4 pins the contract: "after `close()` returns, no event/UI callbacks fire." But callbacks run on the stdout reader thread, and `close()` only bounded-joins reader threads. If a callback is already running when close starts (especially a UI handler), the reader thread won't observe `_closing.is_set()` until it returns from the user code. `close()` returns when the join times out; the callback keeps running against torn-down resources.

**Citations:** `plan.md:26,386,764` vs `synthesis v3:127`.

**Recommendation:** weaken the contract. v5 should say: *"After `close()` begins, no new callbacks start dispatching. In-flight callbacks may continue running but observe `_cancelled` set; any access to client resources from a callback after close raises `PiRpcProcessError`."* This is achievable: check `_closing.is_set()` at dispatch entry (already in v4), and have `client.send` etc. raise `PiRpcProcessError` once `_closing` is set.

Drop the "no callbacks after close returns" wording. Add the blocked-handler test the synthesis asked for, but with the weaker assertion: the handler keeps running, but doesn't touch client state successfully.

### T1.4-v4 `bridge_write_timeout` races sidecar via shared socket timeout

**Corroboration:** G1, G2, S1, S3 — four independent (G1+G2+S1 MODERATE, S3 HIGH).

**The bug.** v4 implements `bridge_write_timeout` by `self.request.settimeout(timeout)` before `sendall`, `settimeout(None)` after. The sidecar disconnect-watcher is blocked in `recv(1)` on the **same socket**. Socket timeout is per-socket state, not per-operation or per-thread. When `ctx.update` sets a finite timeout, the sidecar's `recv(1)` can raise `socket.timeout`, the sidecar's `except OSError: finally: cancelled.set()` fires, and the tool is spuriously cancelled.

This is a **new bug introduced in v4**. v3 didn't have `bridge_write_timeout`.

**Citations:** `plan.md:467,491,501` vs `server.py:138`.

**Recommendation:** don't use shared `settimeout` for write deadlines. Options:

- *(a) `select.select()` before `sendall`* with the timeout; if ready, `sendall` is non-blocking; if not, raise. Doesn't mutate socket state.
- *(b) Drop the feature.* `bridge_write_timeout` is a security-mitigation knob; if implementing it is this hard, document the DoS surface and recommend operational mitigations (process isolation) instead.
- *(c) Per-write socket duplication.* `os.dup` the socket fd, set timeout on the dup, `sendall` on the dup, close. Linux-specific, awkward.

**Recommendation: (a) `select.select` write-readiness check.** Simple, portable, doesn't mutate shared state. Pseudocode for v5: `if not select.select([], [self._request], [], timeout)[1]: raise PiRpcProcessError("ctx.update timeout"); self._request.sendall(frame)`. Sidecar `recv(1)` continues to block normally.

## Tier 2 — Significant (HIGH/MODERATE, 2+ reviewers or single HIGH)

### T2.1-v4 `start()` rollback can wait on its own `_start_complete`

**Corroboration:** S3 HIGH.

`start()` failure transitions to `"failed"` and calls `self.close()`. But `close()` from `"starting"`/`"failed"` waits on `_start_complete` — which `start()` hasn't set yet. Either deadlock or burn the full 10s timeout for every failed start.

**Recommendation:** in `start()`'s except, set `_start_complete.set()` before calling `self.close()`. Or have `start()`'s except path call a private `_cleanup()` directly, bypassing the public `close()` wait.

### T2.2-v4 Update-frame wire shape regresses

**Corroboration:** S2 HIGH.

v4's `ctx.update` pseudocode sends `{"type": "update", "result": ...}` and omits the request `id`. The current wire format (preserved by `shim.py:160`) is `{"id": request_id, "type": "update", "data": ...}`. v4 violates its own "wire protocols unchanged" claim.

**Recommendation:** fix the pseudocode: `{"id": request_id, "type": "update", "data": result.to_wire()}`. Add the round-trip test that asserts `shim.onUpdate` sees the non-default `ToolResult` fields.

### T2.3-v4 Event/UI handler return contracts conflated

**Corroboration:** S2 HIGH.

v4 has a single `_check_handler_return` that warns on any non-`None` return. Current UI handlers return `Mapping | None` (the response dict); event handlers return `None`. v4 would warn on every valid UI response.

**Recommendation:** split the guards. Event handlers must return `None`; non-`None` warns. UI handlers may return `Mapping | None`; non-Mapping (other than `None`) raises. Both reject awaitable / async-gen / generator / `AsyncIterable`.

### T2.4-v4 UI handler runtime rejection skips required response

**Corroboration:** G2 MODERATE.

If a UI handler returns an awaitable (descriptor-bypass case), `_check_handler_return` raises before `_write_stdin_ui_response`. Pi expects an `extension_ui_response` after `extension_ui_request` — without one, pi waits indefinitely or wedges.

**Recommendation:** UI handler invocation wrapped in try/except; on runtime-shape error, log the programming error AND send a default-cancel UI response so the protocol invariant holds. Or explicitly fatal-close the client and document.

### T2.5-v4 Active handler tracking populated too late

**Corroboration:** S1 MODERATE.

v4's `process_request_thread` adds the thread to `_active_handler_threads`. But `process_request` (overridden to acquire the slot semaphore) spawns the thread before `process_request_thread` runs. Window where the thread exists but isn't tracked. `close()` snapshots once; can miss a just-spawned handler.

**Recommendation:** track the thread inside `process_request` (the override that owns thread creation), at the same point as `_handler_slots.acquire`. Use a barrier in tests to verify the tracking races are closed.

### T2.6-v4 `ToolRegistry.snapshot()` is shallow

**Corroboration:** S1 MODERATE.

v4's `snapshot()` is `ImmutableRegistry(dict(self._tools))` — copies the dict but `RegisteredTool` and `ToolSpec` instances are shared. A caller with a reference (from `get()`) can mutate metadata while handler threads read.

**Recommendation:** make `RegisteredTool` and `ToolSpec` frozen dataclasses (mostly already are). Or have `snapshot()` deep-copy. Or expose only read-only proxies from `ImmutableRegistry`.

### T2.7-v4 `_set_fatal` pseudocode is not valid Python

**Corroboration:** G1, G2 MINOR.

`PiRpcProcessError("rpc client fatal") from exc` is valid only in a `raise` statement, not as an expression passed to another function. v4's `_complete_future(fut, exc=PiRpcProcessError(...) from exc)` won't parse.

**Recommendation:** explicit helper `def _caused(exc_cls, msg, cause): e = exc_cls(msg); e.__cause__ = cause; return e`. Use everywhere.

### T2.8-v4 `PiRpcClient.close()` lacks always-finalizing finally

**Corroboration:** S3 MODERATE.

v4's harness `close()` has the try/finally for state finalization. The client `close()` sketch sets `_close_complete` only at the end, without try/finally. Any unexpected exception in `stdin.close()`, `proc.wait()`, or reader join leaves `_close_complete` unset; concurrent closers wait until timeout.

**Recommendation:** mirror the harness pattern — wrap client cleanup in `try/except/log/finally`, set terminal state and `_close_complete.set()` unconditionally in finally.

## Tier 3 — Specific MODERATE / pattern issues

- **Fake-pi adequacy still aspirational** (S4 HIGH). Plan names modes but doesn't specify the synchronization contracts (barriers, ack files, sidecar log files). Reviewers can't tell if the modes will actually be implementable without more spec.
- **`ctx.update` slow-consumer tests nondeterministic** (S4 HIGH). TCP peer "doesn't read" doesn't reliably block `sendall` for small frames; kernel buffers absorb them. Need explicit deterministic mechanism (out-of-process bridge client, shrink receive buffers, large-frame loop).
- **Bisect-baseline self-certifies** (S4 HIGH). Same commit can update the baseline file with a lower count after deleting tests; no parent-row check. Plan needs: last row commit SHA must equal HEAD; count >= previous row (with explicit allowlist for legitimate deletions); CI fails if baseline file not staged in phase commit.
- **Exception hierarchy breaking change underplayed** (S2 MEDIUM). `PiRpcError` becomes abstract base; existing `except PiRpcError:` catches now also catch process, reentrant, and queue-timeout failures. Migration table mentions the exception classes but doesn't call this out as a behavior change for catchers.
- **Restart-unsupported documented for harness only** (S2 MEDIUM). `PiRpcClient` and `PythonToolServer` are public; direct users can also try to restart them. v4 enforces single-use at harness level only.
- **UI event wrapper schema undefined** (S2 MEDIUM). `_wrap_as_event(message)` is called but the resulting event payload shape isn't specified. Breaking change if it differs from current raw-dict dispatch.
- **Predecessor examples teach stale contracts** (S2 MEDIUM). `dev-notes/predecessors/v5-synthesis/examples/basic.py` and `dev-notes/predecessors/v3/docs/runbook.md` still teach async tools, sync-generator streaming, etc. Plan touches `docs/DESIGN.md` and `CLAUDE.md` but not these. Either mark predecessors archived or update.
- **`bridge_write_timeout` not wired through `PiPythonHarness`** (G1 MODERATE). Plan says it lives on `PiLaunchConfig` but the server pseudocode takes it as a `PythonToolServer` kwarg. Harness needs to pass it through; no public path exists.
- **Event-handler descriptor-bypass tests have wrong observable** (S4 MEDIUM). v4 dispatch catches all handler exceptions and logs. `pytest.raises(TypeError)` on a dispatched handler won't see the exception. Tests need to assert log output or different observable.
- **Partial-start needs test hook points** (S4 MEDIUM). Cross-thread monkeypatching of `tempdir.cleanup` / shim writes / client start can race or miss the intended cut point. Implementation should expose narrow injectable cut-point callbacks.
- **Runtime-meta artifact comparison weak** (S4 MEDIUM). Comparing only GIL state doesn't catch a mislabeled artifact path (both jobs writing to the same file). Need expected version_info, executable path, and unique artifact name per venv.
- **`close()` self-join footgun** (S3 MEDIUM). If user code calls `client.close()` from inside an event/UI handler, the reader thread tries to join itself → `RuntimeError`. Either document close-from-callback as unsupported and raise early, or skip `current_thread()` in joins.
- **`ThreadingTCPServer.shutdown()` requires serve thread** (S3 HIGH). `shutdown()` must be called while `serve_forever()` is active; calling before serve-thread start or after it dies hangs. Plan calls `shutdown()` unconditionally in close. Needs lifecycle owner with `_serve_started` flag.

## Tier 4 — Polish

- Plan's `_set_fatal` returns `from exc` syntax issue (T2.7) — also affects readability.
- Event handler bad-return logging vs raising (S4 MEDIUM observability) — tests need to assert log lines.
- Predecessor archival banners (S2 MEDIUM).

## Why v4 missed the win condition

Honest retrospective. The user's challenge was specific: aim for "almost no fault." I missed by:

1. **Deviating from the synthesis on T1.1.** The v3 synthesis literally specified the algorithm: "send() acquires \_send_lock, rechecks \_ui_response_pending, if set releases lock and waits, retries." I substituted "layered locking" thinking it was equivalent. It wasn't; it was *worse*. The reviewers caught this within minutes.
1. **Adding new features without thinking through their interaction with existing state.** `bridge_write_timeout` was Tier-3 polish from v3; I implemented it via `socket.settimeout` without noticing the sidecar shares the socket. The shared-state hazard was visible if I'd thought one more step.
1. **Overpromising in contracts.** "No callbacks after close returns" sounds good but isn't implementable with bounded reader joins. Pinning contracts that the implementation can't actually deliver is a recurring failure mode across plan rounds.
1. **Pseudocode that doesn't parse.** `PiRpcProcessError(...) from exc` as an expression is a Python syntax error. Two reviewers caught it. Should have run the pseudocode through Python before committing.
1. **Tracking timing bugs.** `_active_handler_threads` populated after the slot is acquired but in a different method — a race that's obvious in hindsight but easy to miss when writing prose.

The recurring meta-pattern: each revision fixes the previously-flagged Tier 1 bugs and introduces new Tier 1 bugs in the new fix mechanism. v1→v2 fixed asyncio→threads strategy holes, introduced Future/dispatch races. v2→v3 fixed those, introduced UI ordering + close-during-start races. v3→v4 fixed *some* of those (T1.2, T1.4, T1.5) but introduced bridge_write_timeout race + close-during-start-timeout race + worse-UI-ordering race + several smaller issues.

The signal here is informative: **plan-level design is converging asymptotically toward correctness, never reaching it.** Implementation review (running code under actual races) is the bottleneck. Continued plan revision has diminishing returns vs. starting implementation and fixing what's wrong in code.

## Recommendation

**Two options. I recommend (b).**

### Option (a): Write v5

Scope: 4 Tier-1 fixes (T1.1 literal synthesis pattern, T1.2 unbounded close-during-start wait with active unblock, T1.3 weakened "no callbacks" contract, T1.4 `select`-based bridge write timeout) + 8 Tier-2 quick fixes (wire shape, return contracts, snapshot deep-copy, syntax fixes, etc).

Estimated time: half a day of editing.

Estimated outcome: v5 review will find 3-5 new Tier 1 bugs in the new fix mechanisms (per the pattern). Probably no closer to "no fault" than v4.

### Option (b): Stop plan revisions; start phase 1 implementation

Accept that:

1. v4 has known Tier 1 issues (4 of them) that will be caught by implementation testing within hours of writing code.
1. The fixes are surgical and well-understood (per § Recommendation above for each).
1. Plan revision rounds have diminishing returns: each round fixes some bugs and adds others.
1. Implementation under actual races catches bugs faster than prose review.

**Path:** start phase 1 (`tools.py`) with the v4 plan as the design contract. Treat the v4 review synthesis Tier-1 findings as **implementation-time TODOs** — fix them when writing the code, not before. Specifically:

- When writing `_settle_future` helpers: split into `_pop_pending` + `_complete_future` (per v3 synthesis, already in v4 plan text).
- When writing UI dispatch: literal synthesis pattern (acquire `_send_lock`, recheck flag, release+wait if set). Don't deviate to "layered locking."
- When writing `close()`-during-start: actively unblock partial start (terminate subprocess if launched, close server socket if bound) before waiting on `_start_complete`.
- When writing `ctx.update` with `bridge_write_timeout`: `select.select` write-readiness, don't mutate shared socket timeout.
- Postcondition on close: "no NEW callbacks after close begins"; in-flight callbacks observe `_closing` and fail any client access.
- Wire-shape: `{"id": req_id, "type": "update", "data": result.to_wire()}` (preserve current shim contract).

In code, these are 5-30 lines each. As prose, they're slippery — easy to write wrong.

**My recommendation: (b). Stop revising. Start phase 1.**

The v4 plan is good enough as a design contract. The remaining issues are well-named and well-understood. Implementation testing will catch the residual bugs faster than another round of plan review. Adversarial review of code (rather than prose) gives sharper signal — reviewers can run tests instead of imagining races.

The user's win condition was a teaching exercise: it surfaced the pattern that planning has diminishing returns. The lesson learned is the value. The plan is ready in the sense that "ready means good enough to start implementing"; it's not ready in the sense that "ready means perfect." The latter isn't achievable through prose review of code that doesn't exist yet.
