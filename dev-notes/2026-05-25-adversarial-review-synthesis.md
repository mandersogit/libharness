---
status: Triaged — C1, C2, C3 fixed 2026-05-25; MODERATE backlog pending
created: '2026-05-25'
---

# Adversarial-review synthesis — 9 reviews, asyncio-in-thread branch

## Update (2026-05-25): CRITICAL fixes landed

All three CRITICAL findings are resolved in source. New regression tests at `tests/pi/test_critical_review_fixes.py` (7 cases). 157 unit + 3 live tests pass on 3.11; 155 unit + 3 live on 3.14t (2 wheel-build tests skipped because the FT venv lacks `build`). Lint, mypy strict, pyright clean.

- **C1 (close-during-pending dispatch)** — `_dispatch_sync_hook` and `_register_continuation` now coordinate with `_close_lock`. When closed, the awaiting future/coroutine is completed with `RuntimeError` instead of stranding. Tests: `test_c1_dispatch_sync_hook_after_close_completes_with_exception`, `test_c1_register_continuation_after_close_completes_main_future`, `test_c1_main_future_unblocks_when_continuation_arrives_after_stop`.
- **C2 (owner-thread re-entry deadlock)** — `submit()` detects re-entry via `threading.get_ident() == self._owner_thread.ident` and raises `RuntimeError` with an actionable message ("use `async_*` hook or call from another thread"). Only fires in `threaded=True`; `threaded=False` re-entry is mechanically safe (nested pump) and is preserved. Tests: `test_c2_sync_hook_calling_snapshot_raises_clear_error`, `test_c2_threaded_false_allows_reentry`.
- **C3 (universal wheel ships host-specific binary)** — new `BdistWheelWithPiTag` cmdclass in `setup.py` overrides `bdist_wheel.finalize_options` and `get_tag` based on `_vendored_binary_present()` (checks `src/libharness/_vendor/pi/pi(.exe)` on disk, not the env var alone). Wheels with the binary get a platform tag (e.g. `py3-none-linux_aarch64`); wheels without it remain `py3-none-any`. Tests: `test_c3_wheel_with_bundled_binary_has_platform_tag`, `test_c3_wheel_without_binary_has_universal_tag`.

MODERATE backlog (M1-M17) remains; see triage order below.

## Methodology

Nine adversarial reviews of the current `main` (= `asyncio-in-thread` tip, commit `8cf7537`) on 2026-05-25:

- **2× Opus generalist** (via Agent tool, general-purpose subagent): design-alignment lens, contributor-onboarding lens
- **1× codex-cli generalist, medium effort, `--json`**: validation run that also produced findings
- **6× codex-cli specialized, xhigh effort, `--json`**: 2 generalists (design alignment + senior-reviewer PR lens) + 4 aspects (threading model, F14 reader, pi vendoring, hook surface). The "v2" suffix on filenames distinguishes them from a first v1 batch that wedged for 8 hours due to the stdin-hang hazard now documented in the codex-cli skill.

Raw outputs live in `/tmp/libharness-review/`. This document is the deduplicated synthesis.

Total finding counts: **3 CRITICAL**, **17 MODERATE**, ~**16 MINOR**, plus 5 design concerns, ~6 test gaps, and 4 documentation-drift sites. The F8 + F14 + pi-vendoring work is conceptually correct (the v9 intent table is matched at the architectural level) but the implementation has real correctness gaps in close-vs-queue races, sync-hook re-entrancy, lifecycle/restart state cleanup, and wheel-platform packaging.

## Triage summary (recommended action order)

1. **Fix C1, C2, C3 immediately.** Three independent reviewers agreed on C1; two independent reviewers agreed on C2. C3 is single-reviewer but is a load-bearing distribution bug.
1. **Fix M1, M2, M5, M7, M10, M11 next.** These are tight, localized correctness fixes (timeout / lifecycle / fail-fast) with concrete reproducers in the source reviews.
1. **Fix M3, M4, M6, M8, M9 in a vendoring-and-RPC-API cleanup batch.** Related; should be done together.
1. **Update docs to match post-F8 reality (M14).** 5+ stale `hook_executor` references across 3 files; fix in one editing pass.
1. **Decide on the design concerns (D1-D5) in a separate co-design session.** Not bugs; deliberate trade-offs to confirm.
1. **Backfill the test gaps (T1-T6).** Each one corresponds to a fix above; pair them with the fixes.

______________________________________________________________________

## CRITICAL findings

### C1 — `close()` strands pending `_HookCall` / `_Continuation` items

**Found by:** opus-gen-1, codex-gen-1-v2, codex-gen-2-v2 (3-reviewer consensus)

**Files:** `agent.py:251-255` (`_STOP` enqueue under `_close_lock`), `agent.py:540-559` (`_register_continuation`, unlocked), `agent_class.py:212` (`_dispatch_sync_hook`, unlocked)

**Description:** `close()` puts `_STOP` on the command queue under `_close_lock`. But two other producers — `_register_continuation` (the done-callback for loop futures) and `_dispatch_sync_hook` (loop-thread sync hook dispatch) — put items onto the same queue **without taking the lock**. If `close()` wins the race, those items land *behind* `_STOP`. The owner thread exits on `_STOP` without draining (`_owner_loop` returns immediately). The awaiting coroutine on the loop is wedged on `asyncio.wrap_future(completion)` — never resolved until `runtime.close()` cancels it (which can be much later; the default singleton runtime is closed at process exit). For the window in between, the loop holds references to the bridge socket etc., and any reader on the bridge sees no reply.

**Repro (per codex-gen-1-v2 and codex-gen-2-v2):** `threaded=True` harness, `submit()` an op that returns a manually-controlled `concurrent.futures.Future`, wait until the op is dispatched, call `close()`, then resolve the inner future and assert the outer future resolves or raises promptly. Today it hangs.

**Fix sketch:** Both producers must take `_close_lock` before enqueueing. If `_closed` is set, refuse to enqueue and instead `completion.set_exception(PiAgentHarnessClosed)` / `main_future.set_exception(...)` so awaiting callers unblock immediately. The same coordination should cover hook calls coming in on a still-being-closed harness.

**Test gap (T1):** No regression test exercises close-during-pending-`_HookCall` or close-during-pending-`_Continuation`. `test_ergonomic_pass_cluster4a` covers only the F9 sync-submit race.

### C2 — Sync hooks on the owner thread can't call public harness API (re-entrancy deadlock)

**Found by:** codex-gen-1-v2, codex-gen-2-v2 (2-reviewer consensus)

**Files:** `agent.py:391` (`_call`), `agent.py:575` (`submit`), `agent_class.py:199` (`_dispatch_sync_hook`)

**Description:** Post-F8, sync hooks run on the owner thread. A sync `on_*` / `decide_*` hook that calls any public harness method (`self.snapshot()`, `self.send(...)`, `self.abort()`, etc.) enqueues work onto the same single-consumer queue and then blocks waiting for the owner thread to consume it — but the owner thread *is* the hook, so the new work never runs. Specific to `threaded=True`; in `threaded=False`, MainThread pumps recursively and works mechanically (though a hook calling `agent.send(...)` still deadlocks pi for orthogonal reasons).

**Repro (per codex-gen-1-v2):** `threaded=True` Agent whose `on_agent_start()` calls `self.snapshot()`. Trigger `agent._async_on_event({"type": "agent_start"})`. Assert completion within a short timeout. Today it times out.

**Fix sketch:** Detect re-entry — in `_call` / `submit`, check `threading.get_ident() == self._owner_thread.ident`. If true, either: (a) execute the operation directly in-thread (bypass queue), (b) raise a clear `RuntimeError("sync hook called public harness API; use async hook or call before/after the operation")`. Pick based on whether re-entry is a feature or an anti-pattern (we recommend the latter — disallow it explicitly).

### C3 — Universal wheel ships host-specific pi binary

**Found by:** codex-vendoring-v2 (single reviewer; load-bearing distribution bug)

**Files:** `setup.py:27` (`BuildPyWithPi.run`), `_pi_vendor.py:129` (host-platform selection), `pyproject.toml:61` (`package-data` glob ships `_vendor/pi/**/*`)

**Description:** The current setup builds a wheel that contains the host-build-machine's pi binary, but the wheel's tags say `py3-none-any` (universal). If published to PyPI, pip happily installs the wheel on every platform with only one platform's binary inside. Windows installs look for `pi.exe` (not present); wrong-arch Unix installs hit `Exec format error`. The "install-time fetch via pure-Python wheel" intent (documented in `dev-notes/2026-05-24-pi-vendoring-design.md`) is not what the code actually does — at install time, the setup.py hook only runs for sdist installs, not wheel installs. Wheels skip the hook entirely.

**Fix sketch:** Three options, pick one:

- (a) **Don't run the vendor step during wheel build.** The wheel ships only source; sdist→wheel build on the user's machine fetches at that point. Matches the documented design.
- (b) **Build per-platform wheels via cibuildwheel.** Matches the documented future opt-in (`libharness[bundled-pi]`). Now becomes the current default rather than opt-in.
- (c) **Lazy-fetch at first import or first `start()`.** Smaller wheel, network at runtime. Mismatches the v9 intent.

Recommend (a) — fix the bug without rearchitecting. (b) is a separate scope decision.

______________________________________________________________________

## MODERATE findings

### M1 — `pump_until` silently ignores `timeout` for Future/Coroutine in `threaded=False`

**Found by:** opus-gen-1, codex-threading-v2

**File:** `agent.py:401-446`

The docstring promises "Honors `timeout`" but only honors it for `Event` targets and Future/Coroutine in `threaded=True`. The Future/Coroutine in `threaded=False` branch (`_pump_until(loop_future)` then `loop_future.result()` without timeout) is unbounded. **Repro:** `threaded=False` harness, unresolved `Future()`, `pump_until(f, timeout=0.1)` hangs until externally killed. **Fix:** Either honor the timeout (pass it down to `_pump_until` and respect it like `_pump_until_event` does) or raise `NotImplementedError` on the unsupported combo.

**Test gap (T2):** No test passes a Future with timeout in `threaded=False`.

### M2 — Failed harness startup leaves the harness in a false "started" state, blocks retry

**Found by:** codex-gen-1-v2, codex-gen-2-v2

**Files:** `agent.py:646` (already-started guard), `agent.py:708` (`self.pi` assigned pre-await), `agent.py:791` (snapshot.started checks `self.pi is not None`)

`_PiAgentHarnessCore.start()` assigns `self.pi = PiRpcClient(...)` *before* awaiting `pi.start(...)`. If the subprocess startup fails, `self.pi` is set, `snapshot().started` returns `True`, and the next `start()` raises `RuntimeError("...already started")` from the guard. **Repro:** pi_command that exits immediately; first `start()` raises `PiRpcProcessError`; second `start()` raises "already started"; `snapshot().started` is `True`. **Fix:** Wrap `_start_async` in try/except that nulls `self.pi` and `self.server` on failure; re-raise. Or assign `self.pi` only after `pi.start()` succeeds.

**Test gap (T3):** No failed-start rollback test.

### M3 — `wait_for_event(timeout=X)` treats X as per-iteration, not total deadline

**Found by:** codex-gen-json-lastmessage, codex-gen-1-v2

**File:** `rpc.py:516`, `rpc.py:547-550`

Non-matching events arriving repeatedly extend the wait indefinitely. **Repro (codex-gen-1-v2):** `timeout=0.1` observed taking ~0.3s under a short stream of non-matching events. **Fix:** Track a wall-clock deadline; reduce per-iteration timeout on each retry.

**Test gap (T4):** No total-deadline test for `wait_for_event`.

### M4 — `prompt_and_wait()` is not request-scoped

**Found by:** codex-gen-json-lastmessage

**File:** `rpc.py:392-409`

Subscribes to *all* client events; appends every event; stops on the first `agent_end` from any run. Two concurrent `prompt_and_wait()` calls on one client mix streams; either can return on the other's `agent_end`, truncating the slower run. **Fix:** Each call should filter by a request-scoped session/run id, or be documented as "do not call concurrently on the same client."

**Test gap (T5):** No concurrent-`prompt_and_wait` test.

### M5 — `fetch_and_extract(force=True)` destroys existing install before verifying new

**Found by:** codex-gen-json-lastmessage, codex-vendoring-v2

**File:** `_pi_vendor.py:140-157`

`shutil.rmtree(pi_dir)` at line 143 runs *before* the download + sha256 verify. A transient network failure or sha mismatch leaves the user with no working pi. **Repro (codex-gen-json-lastmessage):** forced refetch failure → `vendored_pi_binary(...).exists() == False`. **Fix:** Download + verify to a temp dir first, then atomic rename or move into place. Only rmtree the old location after the new content is staged.

### M6 — Vendoring is not version-aware (silent stale binary after `PI_VERSION` bump)

**Found by:** codex-gen-json-lastmessage, codex-gen-2-v2

**Files:** `_pi_vendor.py:132-135`, `setup.py:46-47`

If the binary exists, `fetch_and_extract()` returns immediately without checking that the on-disk version matches the current `PI_VERSION` or hash table. A future `PI_VERSION` bump silently keeps running the older bundled binary at the same path. Today this is latent; bites on the first version bump. **Fix:** Stamp a version file alongside the binary (e.g., `_vendor/pi/.libharness_version`); compare on the fast path; re-fetch if mismatched.

### M7 — `PiRpcClient._reader_failure` is sticky across `close()`/`start()` reuse

**Found by:** codex-gen-1-v2, codex-f14-v2

**Files:** `rpc.py:149`, `rpc.py:226` (start), `rpc.py:331` (send), `rpc.py:274` (close)

After a catastrophic reader death, reusing the same `PiRpcClient` (via `close()` then `start()`) leaves `_reader_failure` set; subsequent `send()` calls fail fast against the stale crash marker even though the new subprocess is healthy. Same applies to the decode error counters (`_jsonl_decode_error_count` etc.). **Fix:** Clear `_reader_failure`, `_first/_last_jsonl_decode_error`, and `_jsonl_decode_error_count` in `start()`.

### M8 — `setup.py` only catches `PiVendorError`; raw `OSError`/`PermissionError` escape

**Found by:** codex-gen-2-v2, codex-vendoring-v2

**Files:** `setup.py:48`, `_pi_vendor.py:141/143/165/172` (mkdir/rmtree/extract/chmod)

`urlopen()` network/TLS/HTTP errors, and `mkdir`/`rmtree`/`extractall`/`chmod` raw `OSError`/`PermissionError` bypass the advertised actionable error path. The promised "set `LIBHARNESS_SKIP_PI_VENDOR=1` or provide your own pi" guidance only fires for hash mismatch / unsupported platform. **Fix:** Wrap a broader exception set in `setup.py`'s `BuildPyWithPi.run`, or catch in `fetch_and_extract` and re-raise as `PiVendorError` with the actionable message. The runtime resolver's error message also tells pip users to run `make install-pi`, which is repo-specific — split the user-facing and contributor-facing error messages.

### M9 — Vendoring install path is destructive and non-atomic

**Found by:** codex-vendoring-v2

**Files:** `_pi_vendor.py:132/143/162`

"Binary exists" is the only completeness signal. `rmtree`-then-`extractall` in place is non-atomic. A killed process, partial extract, or parallel build/install leaves `_vendor/pi/pi` present but sibling assets missing; future runs skip repair because the existence check passes. **Fix:** Stage to temp dir → verify → atomic rename. Add a `.complete` sentinel file written last; use that as the completeness signal instead of binary existence.

### M10 — Clean reader EOF doesn't set `_reader_failure`; `send()` can still time out

**Found by:** codex-f14-v2

**Files:** `rpc.py:331`, `rpc.py:574`, `rpc.py:614`

`_read_stdout_loop` breaks on `not chunk` (clean EOF) without setting `_reader_failure`. If stdout closes cleanly while stdin is still writable, the next `send()` enqueues and times out instead of raising `PiRpcProcessError` fast. Also: `decoder.flush()` is never called, so unterminated trailing bytes are silently dropped. **Repro:** fake process with `stdout.read()` returning `b""` and `stdin.drain()` a no-op; `await send(timeout=0.01)` → `TimeoutError` with `_reader_failure is None`. **Fix:** Set `_reader_failure = PiRpcProcessError("Pi stdout closed cleanly")` on the EOF path; call `decoder.flush()` and log/print any partial-buffer warning.

### M11 — Reader-crash in-flight RPC loses original exception chain

**Found by:** codex-f14-v2

**Files:** `rpc.py:600`, `rpc.py:614`, `rpc.py:706`

The crash is stashed in `_reader_failure` (good), but the `finally` block calls `_fail_pending(PiRpcProcessError("Pi stdout closed..."))` — generic message, `__cause__` is `None`. Only *later* `send()` calls preserve the original cause via the explicit chain in the fail-fast guard. **Repro:** fake `stdout.read()` raising `RuntimeError("boom from reader")`, request registered first → `await send()` raises `PiRpcProcessError("Pi stdout closed...")` with no cause; the reader task itself carries the `RuntimeError`. **Fix:** `_fail_pending` should accept the `_reader_failure` and chain it via `.__cause__`.

### M12 — Strict mode doesn't cover bridge observation (`notify_event`) deliveries

**Found by:** codex-hooks-v2

**Files:** `agent_class.py:120`, `agent_class.py:126`, `server.py:201`

`_async_on_bridge_event(..., require_decision=False)` calls `_dispatch_notification_hook(..., strict=False)` and returns. A strict agent silently ignores a known decision event with no `on_X` hook when the gate is closed. Docs describe strict mode more broadly than the implementation behaves. **Fix:** Either pass `strict=self._raise_on_unhandled_event` through, or document the carve-out explicitly.

### M13 — `__init_subclass__` validation only covers class-body-time hooks

**Found by:** codex-hooks-v2

**Files:** `agent_class.py:51`, `hook_surface.py:239`, `hook_surface.py:48`

Validation walks `cls.__dict__` at class creation. A typoed hook inherited from a non-`Agent` mixin is never validated; post-class mutation (`MyAgent.on_not_a_pi_event = fn`) also bypasses both name and collision checks. The docs' blanket "`__init_subclass__` rejects unsupported hook suffixes" claim is broader than reality. **Fix:** Either walk the full MRO during validation (catches mixins) and warn on post-class mutation via `__setattr__` on a metaclass, or narrow the doc claim.

### M14 — Multiple stale `hook_executor` references in docs + code

**Found by:** opus-gen-1, opus-gen-2, codex-gen-1-v2, codex-gen-2-v2, codex-hooks-v2 (5-reviewer consensus)

**Files:**

- `docs/DESIGN.md:27`, `:50`, `:324`/`325` — architecture references
- `docs/AGENT_HOOKS.md:129` — contradicts the corrected text at line 47
- `src/libharness/pi/agent_class.py:36-49` / `:41` — `Agent` docstring
- `src/libharness/pi/runtime.py:128` — class docstring claims "loop thread + tool executor + hook executor"

All sites still describe the pre-F8 "dedicated single-worker hook executor" model. `hook_executor` was removed from `HarnessRuntime` on 2026-05-24. **Fix:** Single editing pass: search-replace all `hook_executor` mentions and the matching prose; verify `docs/AGENT_HOOKS.md` is internally consistent.

Also flagged: `docs/DESIGN.md:199-208` leads with `PiPythonHarness` as the canonical Python API, but the rest of the codebase treats `PiAgentHarness` / `Agent` as primary. A reader copying the example lands on the wrong type.

### M15 — `PiPythonHarness` silently doesn't use a tool executor

**Found by:** opus-gen-2

**File:** `harness.py:53`

`PythonToolServer(registry)` constructed without `tool_executor=`. `PiAgentHarness` always passes `runtime.tool_executor`. So sync/blocking tools under `PiPythonHarness` stall the asyncio loop. No docstring warning. **Fix:** Either pass `tool_executor=` in `PiPythonHarness` too, or document `PiPythonHarness` as "async-tools only" and add an assertion at server construction time.

### M16 — `PiAgentHarness(threaded=True, start_owner_thread=False)` half-init deadlock state

**Found by:** opus-gen-2

**File:** `agent.py:193-197`

With `threaded=True` and `start_owner_thread=False`, the harness ends `__init__` with no core and no owner thread. Subsequent operations deadlock on `_owner_ready` or raise from `_call`. One test (`test_ergonomic_pass_cluster2.py:251-263`) uses this pattern with a comment acknowledging it's a trap. **Fix:** Raise `ValueError` in `__init__` when this combination is detected unless an explicit `_partial_init=True` sentinel is passed. Or deprecate `start_owner_thread` entirely (the only test using it can switch to `threaded=False`).

### M17 — Consolidated test gaps (cross-cutting)

**Found by:** opus-gen-1, codex-gen-2-v2, codex-gen-1-v2

- **T1** (CRITICAL gap, paired with C1): close-during-pending-`_HookCall`/`_Continuation`. No coverage.
- **T2** (M1): `pump_until(future, timeout=)` in `threaded=False`. Untested.
- **T3** (M2): failed-start rollback. No test asserts `snapshot().started == False` after a failed `start()`.
- **T4** (M3): `wait_for_event` total deadline.
- **T5** (M4): concurrent `prompt_and_wait` on one client.
- **T6**: vendoring success E2E for tarball/zip extract (only failure paths + idempotent fast-path are tested today; per codex-gen-2-v2).
- **T7** (M7): restart-after-reader-failure. No test verifies counters/failure state clear.
- **T8** (opus-gen-1, lower priority): hook-ordering when `_HookCall` and `_Continuation` interleave on the queue.

______________________________________________________________________

## MINOR findings

Numbered for citation; not by priority. Many are documentation or hardening nits.

1. **`_handler_ctx_mode` misses `**kwargs` case** (opus-gen-1, opus-gen-2, codex-hooks-v2 — 3-reviewer consensus). `def decide_X(self, event, **kw)` silently loses ctx. `agent_class.py:281-309`. The 3-way agreement suggests this should perhaps be MODERATE.
1. **`_pump_until_event` polls every 50ms** (opus-gen-1, opus-gen-2). Hardcoded, no override, not in docstring. `agent.py:455-471`.
1. **`_STOP` re-enqueue branch in `_pump_until*` is dead code in `threaded=False`** (opus-gen-1). `close()` only puts `_STOP` in threaded mode. `agent.py:467-469`, `:491-495`.
1. **`_register_continuation` strand on `commands.put` failure** (opus-gen-1). Currently `# pragma: no cover` because queue is unbounded; the silent-strand path would be undetectable. `agent.py:555-559`.
1. **`close()` double-check `if self._closed:` outside `_close_lock`** (opus-gen-1). Two threads racing close can both pass; wasteful, not catastrophic. `agent.py:243-261`.
1. **`setattr(exc, '_libharness_logged', True)` not slot-safe** (opus-gen-1). A user exception with `__slots__ = ()` raises `AttributeError`. `agent_class.py:177`, `:192`.
1. **`PiLaunchConfig.startup_timeout` misnamed** (opus-gen-2; already known as F29 / "remain deferred"). Silently capped to 0.2s. `rpc.py:73-82`.
1. **`PiLaunchConfig.pi_command = None` magic resolution surprise** (opus-gen-2). Reader sees `None` and assumes PATH; system refuses PATH explicitly. `rpc.py:45-58`.
1. **`send(timeout=X)` / `prompt_and_wait(timeout=X)` silently translate to `wait_timeout = X + 1.0`** (opus-gen-2). Undocumented 1-second pad. `agent.py:266-270`, `:284`.
1. **`HookContext._cancelled: threading.Event | None`** (opus-gen-2). If `None`, `cancelled` always `False`. Dispatcher always passes one, but the dataclass leaves room for `None` to silently disable cancellation. `events.py:67-72`.
1. **`tarfile.extractall` has no path/symlink hardening** (codex-vendoring-v2). No `filter="data"` (Python 3.12+) or member validation. Defense-in-depth gap, not exploitable today. `_pi_vendor.py:163`.
1. **Bridge doesn't enforce per-event decision return shapes** (codex-hooks-v2). TS shim forwards any JSON; per-event table in docs is advisory. Malformed returns cross the bridge unchecked. `shim.py:270/299`.
1. **F-number citations in source code with no in-tree decoder** (opus-gen-2). 49 hits; decoder is `dev-notes/2026-05-17-v8-port-deferred-items.md`. No in-tree link from `agent.py:164` to that doc.
1. **SESSION-STATE stale** (codex-gen-1-v2). Says "No code changes to src/libharness/pi/ yet" (line 18) and lists session wrappers / generator-through-bridge coverage as pending, both already done.
1. **`_OBSERVABLE_EVENT_NAMES = _EVENT_NAMES | _DECISION_EVENT_NAMES` is observer-set + decider-set, not "observable-only"** (opus-gen-2). Naming surprise.
1. **`PythonToolServer.timeout_ms` controls two unrelated timeouts** (opus-gen-2). Bridge read timeout + threadsafe-update-relay timeout share a parameter.

______________________________________________________________________

## Design concerns (deliberate trade-offs to confirm)

### D1 — Hook starvation under sustained operation traffic

**Source:** opus-gen-1

Post-F8, FIFO ordering on the owner queue means if a caller submits N RPCs ahead of a `_HookCall`, the hook queues behind N commands. The dispatcher doesn't prioritize hook calls. In HITL scenarios, a `prompt()` enqueued after a decision event arrives could starve the human's decision response. **Discussion needed:** is starvation acceptable here, or should hooks have priority?

### D2 — "Single code path" goal only partially realized

**Source:** opus-gen-1

`_dispatch_sync_hook` is branch-free (good — that was the v9 invariant). But `agent.py:430-446` (`pump_until`) and `agent.py:391-398` (`_call`) still branch on `self.threaded`. "Consumer differs by mode" is enforced by explicit branching at the dispatcher entry points, not by polymorphism. **Discussion needed:** confirm this matches intent, or push further (e.g., a `_Consumer` strategy object that abstracts owner-thread-vs-MainThread).

### D3 — Sync-hooks-must-not-block invariant is unenforced and unwarned

**Source:** opus-gen-2

Under `threaded=False`, blocking sync hooks block MainThread (documented as a limitation). No enforcement, no warning at registration time, no warning at runtime. A user's `time.sleep(60)` in `on_tool_call` wedges the test driver. **Discussion needed:** should we detect long-running hooks at runtime and warn? Or accept that this is documented and move on?

### D4 — "Owner thread is the only writer to core state" is invariant-by-convention

**Source:** opus-gen-2

`_check_owner` is called only on methods that explicitly call it. Direct attribute access (e.g., `core.pi`, `core.server`) bypasses the check. `agent.py:695` has an explicit comment about a loop-thread write to `self.endpoint` not protected, relying on `start()` not having returned yet. **Discussion needed:** stricter enforcement (property setters with checks)? Or rely on review discipline?

### D5 — `HarnessRuntime` public/internal status unresolved

**Source:** opus-gen-2, codex-gen-1-v2

`HarnessRuntime`, `AsyncioLoopThread`, `get_default_runtime`, `close_default_runtime` are all in `__all__` (public). SESSION-STATE itself flags this as "open questions Q1-Q4." The Level 3 audit dev-note exists for this. **Recommended:** resolve before any external user freezes expectations.

______________________________________________________________________

## Documentation drift (already consolidated under M14 but listed for completeness)

| File                               | Line(s)         | Drift                                                              | Severity  |
| ---------------------------------- | --------------- | ------------------------------------------------------------------ | --------- |
| `docs/DESIGN.md`                   | 27, 50, 324/325 | Pre-F8 `hook_executor` references                                  | M14       |
| `docs/DESIGN.md`                   | 199-208         | Leads with `PiPythonHarness` as canonical                          | M14       |
| `docs/DESIGN.md`                   | 221             | Lists pi session wrappers as pending (done)                        | minor #14 |
| `docs/AGENT_HOOKS.md`              | 129             | "decide\_\* runs on dedicated hook executor" (contradicts line 47) | M14       |
| `docs/AGENT_HOOKS.md`              | 170-173         | `pump_until` signature description is informal                     | minor     |
| `src/libharness/pi/agent_class.py` | 36-49, 41       | `Agent` docstring describes pre-F8 model                           | M14       |
| `src/libharness/pi/runtime.py`     | 128             | `HarnessRuntime` docstring lists `hook_executor`                   | M14       |
| `dev-notes/SESSION-STATE.md`       | 18, 67, 69      | Stale branch context + pending-task list                           | minor #14 |

**Suggested fix order:** one editing pass through DESIGN.md + AGENT_HOOKS.md + the two docstrings, then re-run mdformat + markdownlint.

______________________________________________________________________

## Considered and ruled out (consolidated)

Each item below was spot-checked by at least one reviewer and explicitly not flagged. Listed so future reviewers don't re-walk the same ground.

- **F14 reader-failure visibility path** — logged, stderr-printed, counted; `capsys` coverage exists in `test_f14_reader_death.py`; counter updates are single-threaded on the event loop. (codex-f14-v2, codex-gen-json-lastmessage)
- **F14 catastrophic reader death → fail-fast** — `send()` checks `_reader_failure` and raises before request registration; the narrow race is closed because there's no `await` between check and register. (codex-f14-v2, codex-gen-2-v2)
- **`set_running_or_notify_cancel()` + `CancelledError` path in `_process_item`** — pre-dispatch cancellations are dropped; `_handle_continuation` avoids invalid-state writes on cancelled futures. (codex-threading-v2)
- **`_WAKE` lost-wake** — done-callback fires synchronously if future is already done; stale wakes are explicit no-ops. The bug is `_STOP` overtaking later items (C1), not `_WAKE` itself. (codex-threading-v2, codex-gen-2-v2)
- **FIFO ordering of sync hooks within one harness** — `_HookCall`s produced from the single asyncio loop thread; `queue.Queue` preserves order. Continuations can interleave but no hook-before-hook reordering. (codex-threading-v2)
- **`_close_lock` / `_owner_start_lock` deadlock** — `start_owner_thread()` is called before taking `_close_lock`; no nesting. (opus-gen-1)
- **JSONL split-chunk buffering** — record-by-record advance correctness; chunk boundary inside a record doesn't corrupt state. (opus-gen-1, codex-f14-v2)
- **`next_event` close-event race** — `_close_event` is loop-agnostic at construction; `asyncio.wait(..., FIRST_COMPLETED)` is clean. (opus-gen-1)
- **Cross-harness hook serialization removed** — intentional v9 change; each harness has its own queue + owner thread. (opus-gen-1, codex-gen-1-v2)
- **SHA-256 verification** — `hashlib.sha256()` + hex-string equality is fine; `compare_digest()` wouldn't materially change the threat model (no remote timing oracle). (codex-vendoring-v2)
- **`urlopen()` TLS defaults** — Python uses normal CA/hostname verification with no custom context; `LIBHARNESS_PI_DOWNLOAD_BASE` is not an integrity bypass because the hardcoded sha256 still gates the payload. (codex-vendoring-v2)
- **Channel separation** — enforced by code shape, not just tests; bridge decision events enter through `server.py:201`, raw RPC subscribers are only fed by `rpc.py:641`. (codex-hooks-v2)
- **`HookContext.cancelled` cooperative model** — flag flips on disconnect/timeouts via `server.py:239`; a hook that blocks in CPU/IO misses it by design, matching the docs' polling model. (codex-hooks-v2)
- **`server.py:watch_disconnect` (F13)** — `CancelledError` re-raised without setting flag; only EOF/reader exception sets it. (opus-gen-1)
- **`HarnessRuntime.tool_executor` shutdown** — `cancel_futures=True`, default Python behavior; correct. (opus-gen-1)
- **No `$PATH` fallback in resolver** — consistent between code and tests; no hidden fallback path. (codex-gen-2-v2)
- **Threaded=False blocking-hook limitation** — documented as accepted in `docs/AGENT_HOOKS.md:61`; reviewers consciously did not flag it as a bug. (codex-gen-1-v2)

______________________________________________________________________

## Process / methodology observations

- **v1 wedged batch**: 6 codex reviews launched without `--json` and without `< /dev/null` ran for 8 hours with 0 CPU time consumed before being killed. The v2 batch with both flags completed in ~10 min each. Root cause documented in the codex-cli skill (the stdin-hang hazard); skill updated.
- **Medium effort still found 3 MODERATE bugs the xhigh runs missed.** Specifically the `wait_for_event` deadline bug, `prompt_and_wait` not request-scoped, and the destructive-refetch bug. Worth keeping medium-effort runs in the toolkit alongside xhigh.
- **3-reviewer consensus signal** — C1 (close race), M14 (stale hook_executor docs), and `_handler_ctx_mode` \*\*kwargs miss are each flagged independently by 3+ reviewers. High-confidence bugs.
- **Opus generalist #1's contributor-lens approach** (opus-gen-2.md) caught the most surprising-to-a-new-reader items: `PiPythonHarness` silent tool-executor omission (M15), half-init deadlock (M16), `_EVENT_NAMES` private-name confusion. Different findings from the bug-hunt reviewers.
- **Specialized aspect reviews** each found bugs in their scope that the generalists missed (e.g., codex-vendoring-v2's C3 universal-wheel bug, codex-hooks-v2's M12 strict-mode bridge-observation gap). The specialist/generalist split was worth the orchestration cost.

______________________________________________________________________

## Recommended next steps

1. **Immediate:** Fix C1, C2, C3 (one commit each, with paired tests T1, repro from C2, and the wheel-build approach decision from C3).
1. **Soon (single batch):** M1, M2, M5, M7, M10, M11 + their tests (T2, T3, T7). All small + localized.
1. **Next batch:** M3, M4, M6, M8, M9 (RPC API + vendoring cleanup). + T4, T5, T6.
1. **Docs pass:** M14 single-editing-pass + minor #14 / SESSION-STATE refresh.
1. **Co-design session:** D1-D5 each get a yes/no decision and either a fix or a documented "accepted limitation."
1. **MINOR cleanup pass:** Pick top 5-7 minors per session as bandwidth allows. The 3-way-consensus #1 (handler_ctx_mode \*\*kwargs) should be among the first; treat as effectively MODERATE.

If implemented in this order, the F8/F14/vendoring work goes from "conceptually correct but with real correctness gaps" to "production-ready" in ~2-3 focused sessions.

______________________________________________________________________

## References

- Raw review outputs: `/tmp/libharness-review/*.md` (9 files)
- Codex-cli JSON streaming dev-note: `dev-notes/2026-05-25-codex-cli-json-streaming.md`
- v9 intent: `dev-notes/2026-05-24-v8-vs-v9-intent-discussion.md`
- Level 3 audit (status: Partial): `dev-notes/2026-05-24-level-3-runtime-audit.md`
- Deferred items doc (post-F8/F14): `dev-notes/2026-05-17-v8-port-deferred-items.md`
