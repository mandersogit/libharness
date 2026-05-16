---
status: "Final"
created: "2026-05-16"
---

# Phase 4 design review synthesis (v1)

Three independent adversarial reviewers (2 codex gpt-5.5 xhigh, 1 Opus subagent) reviewed the **v1** of `dev-notes/2026-05-16-threads-rewrite-phase4-design.md`. All three said "not ready as an implementation contract." This doc consolidates findings; the design has been revised to v2 (same file) addressing all CRITICAL + HIGH items.

| ID      | Verdict                                          |
| ------- | ------------------------------------------------ |
| codex-A | not ready (5 HIGH+ findings, 7 MODERATE)         |
| codex-B | not ready (2 CRITICAL, multiple HIGH)            |
| Opus    | not ready, needs targeted edits (3 CRITICAL, 3 HIGH) |

Inputs: `/tmp/phase4-design-review-codex-A.md`, `/tmp/phase4-design-review-codex-B.md`, `/tmp/phase4-design-review-opus.md`.

## Tier-1 — must fix before approval-gated implementation begins

### F1. `asyncio.CancelledError` claim is factually wrong (Opus C1, implicit in codex-A H2)

**Bug.** § Risks #2 of v1 claimed: *"A tool body that raises `asyncio.CancelledError` (no loop active) is just a regular exception — the existing `except Exception` catches it."* This is **wrong**. `asyncio.CancelledError.__mro__` is `(CancelledError, BaseException, object)` — it does NOT inherit from `Exception`, regardless of loop state. Acting on this rationale and dropping the phase 2 F1 explicit `except asyncio.CancelledError:` arm would re-introduce a known wire-corruption bug.

**Fix in design v2.** Edit § Risks #2 to acknowledge the actual MRO and **keep the explicit `except asyncio.CancelledError:` arm** in the new sync `_handle_execute`. Add a regression test (tool body raises CancelledError → structured error response).

### F2. `_start_complete` producer contract unspecified → close-during-start deadlock (Opus C2, codex-B C1, codex-A CRITICAL)

**Bug.** v1's design says `close()` from `starting` waits unbounded on `_start_complete` after active unblock. But never specifies WHERE `_start_complete` is set in `start()`. A literal implementation that wraps cut points in `try/except: _cleanup(); raise` (without `finally: _start_complete.set()`) creates the v4-synthesis T2.1 deadlock: `close()` waits forever on an event the failing-start path never sets.

**Fix in design v2.** Show the `start()` body skeleton with **explicit `finally: _start_complete.set()`**. State that the event is set on every exit path — success, failure, abort.

### F3. `_cleanup()` runs in TWO paths with no ownership rule (Opus C3, codex-B C2, codex-A CRITICAL)

**Bug.** Both `start()`'s except handler AND `close()`'s post-`_start_complete` arm call `_cleanup()`. Design v1 claims "at most once" but provides no lock or ownership flag. `_cleanup()` mutates `self.pi`, `self.server`, `self._tempdir` without lifecycle-lock protection — racy on FT, racy against the `harness.client` property.

**Fix in design v2.** Pick one cleanup owner. Option (a): `start()`'s except handles all start-failure cleanup; `close()` waits for `_start_complete`, then runs cleanup ONLY if `start` did NOT clean up (set a `_cleanup_done` flag). Option (b): `close()` is always the cleanup owner; `start()` just sets `_start_complete` in finally and re-raises. **v2 picks (b)**: simpler, single owner. `start()` runs the user's resource-creating code; on failure it just re-raises after setting `_start_complete` (and `_lifecycle_state = "aborted"`); cleanup is exclusively `close()`'s job.

### F4. Shim-write cut point has no active unblock (Opus H1, codex-A HIGH)

**Bug.** v1 admits shim/faux-extension writes have no unblock; the next paragraph claims close is "bounded by cut-point timeouts." Contradiction. On slow filesystems, close hangs unbounded.

**Fix in design v2.** **Reorder cut points** so non-unblockable steps (shim write, faux write) run BEFORE I/O-launching steps (server bind, pi launch). Then the active-unblock list is non-empty exactly when partial start has launched threads. Close-during-pre-server-bind work is fast because writes are local and small.

### F5. Active unblock interleaves with `_start_sync` mid-mutation (codex-A CRITICAL, Opus H2)

**Bug.** `close()` calls `self.pi._close_sync()` while `self.pi._start_sync()` may still be mutating internal state. Phase 3's `PiRpcClient` has `_close_lock` (serializes closes) but NOT a start/close lock. The interleave is unsafe.

**Fix in design v2.** Either (a) add a start/close lock to `PiRpcClient` (deferred to phase 4 cleanup), or (b) the harness's `_cleanup` owner waits for `_start_complete` before touching `self.pi`. **v2 picks (b)**: `close` always waits for `_start_complete` first (per F3 fix), so by the time cleanup runs, `start()` is no longer mutating.

### F6. Phase-4 Makefile guard parsing is broken + can self-certify test deletion (codex-A HIGH)

**Bug.** `tail -n 1 dev-notes/rewrite-bisect-baseline.md` is the trailing prose, not a data row. `grep -oE '\b[0-9]+\b' | head -1` would read digits from the commit SHA before the test count. And the count comparison allows test-count decrease in the same commit that updates the baseline.

**Fix in design v2.** Replace shell parsing with a small Python helper script. Enforce: (1) previous row's SHA matches HEAD's parent, (2) test count non-decreasing (with optional allowlist for legitimate deletions), (3) phase-N commit must edit the baseline file.

### F7. Async-shim removal scope missed in-tree `_*_sync` callers (codex-A HIGH)

**Bug.** v1's test-rewrite section only names live tests + one async-shim test. But `tests/pi/test_rpc_fake.py`, `test_server.py`, `test_set_model.py` call `_start_sync`, `_close_sync`, `_get_state_sync`, `_prompt_and_wait_sync`, `_set_model_sync`, etc. directly. Renaming the methods without updating these tests breaks the suite.

**Fix in design v2.** Add all three test files to the phase-4 rewrite scope. Since phase 4 drops the private/public split, rewriting tests to public sync method names is the correct migration.

## Tier-2 — MODERATE findings worth addressing in v2

### F8. Five-state machine doesn't specify user-visible result of aborted start (codex-A MODERATE, Opus M1)

**Fix in v2.** Pin: abort-during-start raises `PiRpcProcessError("harness start aborted by concurrent close")` to the start caller. Diagnostic field `_start_error` for observability.

### F9. `close()` from `new` state not specified (codex-A MODERATE)

**Fix in v2.** Add transition `new → closed` (no-op cleanup, but `_lifecycle_state = "closed"` so a subsequent `start()` raises restart error). Add test.

### F10. Sync-generator decision contradicts itself across the doc (codex-A MODERATE, Opus M6)

**Fix in v2.** Commit to **drop now** (per recommendation in v1's risks section). Remove the "keep for one release" prose. Add registration + runtime rejection tests.

### F11. PythonToolServer restart semantics not specified (codex-A MODERATE)

**Fix in v2.** Apply the same single-use rule to `PythonToolServer` as `PiRpcClient`. Document in migration table.

### F12. Removing per-execute event loop is a hidden breaking change for async-app callers (codex-A MODERATE)

**Fix in v2.** Add migration recipe in breaking-changes prose: "async-app callers should wrap blocking client calls with `asyncio.to_thread()` rather than calling them from the event loop directly."

### F13. Test enumeration too thin (Opus H3)

**Fix in v2.** Enumerate the lifecycle tests explicitly: 6 partial-start-failure tests (one per cut point), 6 close-during-cut-point tests, double-close from threads, restart-after-close, close-from-new, cleanup-runs-exactly-once-under-concurrent-start-failure-and-close.

### F14. Bisect-baseline retroactive substitution is brittle (codex-A MODERATE, Opus M4)

**Fix in v2.** Add the Python parser script (per F6) to also enforce: if the previous row has a `PHASE*_PLACEHOLDER` SHA, the current commit MUST substitute it with a real SHA. Pre-commit hook on phase branches.

## Tier-3 — MINOR (defer or accept)

- v1's `_aborted` flag is defined but never read in pseudocode (Opus M3) — fixed naturally by v2's single-owner-cleanup design (F3); no `_aborted` flag needed.
- Migration prose distribution between commit message + docs/DESIGN.md (Opus M5) — defer to implementation.

## Out of scope (not phase-4 design concerns)

- Phase 3's `PiRpcError` hierarchy is locked-in (already shipped, not under design review).
- Event-bridge co-design (separate gate).
- Helper-thread reentrancy detection.

## Plan

1. Revise design v1 → v2 in `dev-notes/2026-05-16-threads-rewrite-phase4-design.md` addressing F1–F14.
1. Commit both the synthesis doc + the revised design doc.
1. Optional second design-review pass (3 reviewers on v2) — author's call. The findings here are surgical; v2 should converge on "approved" without another round, but the author chose 3-reviewer review for design before to catch exactly this class of bug.
1. After approval, phase 4 implementation begins. Same per-phase loop as phases 1-3.
