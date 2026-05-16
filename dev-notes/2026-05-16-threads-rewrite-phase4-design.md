---
status: "v2 — pending second design review"
created: "2026-05-16"
---

# Phase 4 design proposal — harness.py rewrite + async-shim removal (v2)

Per the phase-1 handoff doc: pause after phase 3, write a 1-2 page design doc, run 3 reviewers, then implement. v1 of this doc went through 3-reviewer adversarial review; all three said "not ready as an implementation contract." Synthesis at `dev-notes/2026-05-16-threads-rewrite-phase4-design-review-synthesis.md`. This **v2** addresses every Tier-1 and Tier-2 finding from that synthesis. (v1 archived in git history at the previous commit of this file.)

**Status:** v2 — pending second design review. Once reviewed and approved, this becomes the implementation contract for phase 4.

## Phase 4 scope

1. **Rewrite `src/libharness/pi/harness.py`** as sync: lifecycle state machine (5 states), single-owner cleanup, close-during-start abort, always-finalizing close.
1. **Drop async shims** from `PiRpcClient` (phase 3) and `PythonToolServer` (phase 2). Rename `_*_sync` core methods to the public names. Update **every in-tree caller** that uses `_*_sync` form (tests, harness, anything else).
1. **Drop per-execute `asyncio.run`** in `server.py`. Tool bodies run directly on the handler thread. `collect_tool_result` becomes sync. `ctx.update` becomes sync. **Keep the explicit `except asyncio.CancelledError:` arm** (per F1 review fix — see § Risks for why dropping it would re-introduce phase 2's F1 bug).
1. **Reject `async def` / generator / async-gen** tool functions at registration in `tools.py`. Runtime guard rejects descriptor-bypass cases.
1. **Rewrite live tests as sync**: `test_real_pi_integration.py`, `test_real_llm.py`. `with` not `async with`. Inline async tool bodies → sync.
1. **Rewrite in-tree `_*_sync` callers**: `tests/pi/test_rpc_fake.py`, `tests/pi/test_server.py`, `tests/pi/test_set_model.py` — replace `client._start_sync()` etc. with `client.start()` etc. as part of the same commit that drops the suffix.
1. **Drop `pytest-asyncio`** dev dep and `asyncio_mode = "auto"` ini option.
1. **Widen the AST contract guard** to all of `tests/pi/`.
1. **Phase-4 Makefile guard** with a small Python helper script (not shell parsing — v1's `tail | grep` design was broken per F6 review fix). Enforces: previous baseline row's SHA matches HEAD's parent; test count non-decreasing; phase-N commit must substitute any `PHASE*_PLACEHOLDER` from the previous row.
1. **Bisect-baseline retroactive cleanup**: substitute the real phase-3 commit SHA for the `PHASE3_PLACEHOLDER` row; append phase-4's placeholder row.
1. **docs/DESIGN.md migration + § Breaking changes** (14-row table per v4 plan + 1 new row per F12: "blocking client calls from async code require `asyncio.to_thread`").
1. **CLAUDE.md update**: drop the "Async-first" convention; pin "threading-first" + helper-thread reentry caveat + single-use lifecycle (applies to harness, client, AND server per F11 review fix).

## Lifecycle state machine

Five states. (v1 of this doc collapsed v4's seven states; v2 keeps the five-state collapse but pins single-owner cleanup per F3.)

### States

| State      | Meaning                                                                              |
| ---------- | ------------------------------------------------------------------------------------ |
| `new`      | Constructed, not started.                                                            |
| `starting` | `start()` running; may transition to `started`, or via `_aborted` to `closed`.       |
| `started`  | All cut points succeeded; harness is live.                                           |
| `closing`  | `close()` running; lower layers are being torn down.                                 |
| `closed`   | Terminal state. Either succeeded cleanup or failed cleanup (logged, not raised).     |

### Transitions (with explicit caller-visible behavior per F8 review fix)

```text
new ── start() ──→ starting ── (success) ──→ started ── close() ──→ closing ──→ closed
        │              │
        │              ├── (cut-point failure) ──→ raises original exception;
        │              │                           sets _start_complete; close() owns
        │              │                           cleanup; instance becomes closed.
        │              │
        │              └── (close() during starting) ──→ start() raises
        │                                                PiRpcProcessError("harness start aborted");
        │                                                sets _start_complete; close() owns
        │                                                cleanup; instance becomes closed.
        │
        └── close() ──→ closed (no-op cleanup; single-use sentinel set per F9 review fix)
```

`new → close() → closed` is **explicit** (F9 review fix): double-close from `new` is harmless; subsequent `start()` from `closed` raises `RuntimeError("harness is single-use")`.

### Single-owner cleanup (F3 review fix)

**v1's bug:** both `start()`'s except handler AND `close()`'s post-`_start_complete` arm called `_cleanup()`. No lock. Racy on FT.

**v2:** `close()` is the SOLE cleanup owner. `start()` never calls `_cleanup()`. On any cut-point failure:

1. `start()` re-raises the cut-point's exception to the caller (so the user sees the actual error, not a generic "cleanup failed").
1. `start()`'s `finally` sets `_lifecycle_state = "aborted"` and `_start_complete.set()`.
1. The caller (user code or harness `__exit__`) is expected to call `close()` to clean up. If they don't (test catches the exception and never calls close), the cleanup is deferred to GC of `_tempdir` and process exit (daemon threads).
1. If a concurrent thread is in `close()` waiting on `_start_complete`, that thread proceeds with cleanup.

This means: **resource leaks possible if `start()` fails and the caller forgets to call `close()`**. The mitigation is the harness context manager (`with PiPythonHarness(...) as h:`) which always calls `close()` in `__exit__`. Document this as the recommended usage pattern.

### Concurrent close-during-start: unbounded wait with active unblock (after cut-point reordering per F4)

`close()` from `starting`:

1. Acquires `_lifecycle_lock`. Sets `_aborted = True` (sentinel for `start()` to observe between cut points). Releases.
1. **Actively unblocks** each cut point that has launched a thread or socket (cut-point list reordered per F4 — see below):
   - If `self.pi` is set AND `self.pi.process is not None`: call `self.pi._force_terminate_subprocess(self.pi.process)`. This is phase-3's bounded SIGTERM/kill helper. Does NOT call `self.pi._close_sync()` directly — that interleaves unsafely with `_start_sync` per F5. Instead, wake the start thread by killing the subprocess; `_start_sync`'s startup probe will see the dead process and raise.
   - If `self.server._http_server` is set: shut down the listener socket. The server thread exits; `_start_sync`'s `server.start()` already returned (this cut point doesn't block long).
1. `close()` waits on `_start_complete` **unbounded** (the unblocks above bound it; the reordered cut-point list has no unbounded steps remaining after this point).
1. `start()` observes `_aborted` between cut points OR is unblocked by the active steps; either way reaches its `finally`, sets `_start_complete`.
1. `close()` transitions to `closing`, runs `_cleanup()`, transitions to `closed`, sets `_close_complete`.

### Cut points (reordered per F4 review fix)

v1's bug: shim writes (no unblock) were AFTER server start (which has unblock). v2 reorders so non-unblockable cut points come FIRST:

1. **`_artifact_root()`** (tempdir creation if needed) — fast local I/O.
2. **`write_bridge_shim(shim_path, ...)`** — file write; no unblock, but ALL OTHER cut points haven't launched threads yet so close-during-this is harmless (just leaves a tempdir behind for `_cleanup` to remove).
3. **`write_faux_toolcall_provider_extension(...)`** (only if `fake_provider=True`) — same as above.
4. **`self.server.start()`** (bridge bind) — fast; serve thread launched. Unblock = shut down listener socket.
5. **`self.pi = PiRpcClient(config)`** (construction) — no I/O.
6. **`self.pi.start(extension_paths=...)`** (subprocess launch + reader threads) — unblock = `_force_terminate_subprocess`.

Now `close()`'s active unblock list is non-empty exactly when partial start has launched threads/processes. Close during cut points 1-3 doesn't need to unblock anything (the cut points are fast and synchronous).

### `start()` body skeleton (per F2 review fix — explicit `_start_complete.set()`)

```python
def start(self) -> PiPythonHarness:
    with self._lifecycle_lock:
        if self._lifecycle_state != "new":
            raise RuntimeError("PiPythonHarness is single-use")
        self._lifecycle_state = "starting"

    try:
        # Cut point 1: tempdir (fast local I/O).
        self._tempdir = _make_tempdir_if_needed(self.workdir, self.keep_temp)
        if self._aborted: raise _AbortedStart()

        # Cut point 2: bridge shim write.
        shim_path = self.shim_path or self._artifact_root() / "python_tools_extension.ts"
        write_bridge_shim(shim_path, ...)
        if self._aborted: raise _AbortedStart()

        # Cut point 3 (optional): faux provider.
        extension_paths = [shim_path]
        if self.fake_provider:
            faux_path = self._artifact_root() / "faux_toolcall_provider.ts"
            write_faux_toolcall_provider_extension(faux_path, ...)
            extension_paths.append(faux_path)
        if self._aborted: raise _AbortedStart()

        # Cut point 4: server bind. Launches the bridge serve thread.
        endpoint = self.server.start()
        if self._aborted: raise _AbortedStart()

        # Cut point 5: PiRpcClient construction (no I/O).
        config = _build_pi_config(self.config, endpoint, ...)
        self.pi = PiRpcClient(config)
        if self._aborted: raise _AbortedStart()

        # Cut point 6: pi subprocess launch + reader threads.
        self.pi.start(extension_paths=extension_paths)

        with self._lifecycle_lock:
            if self._aborted:
                # close() killed pi while we were blocking in pi.start
                # → pi.start raised already; this branch is defensive.
                raise _AbortedStart()
            self._lifecycle_state = "started"
        return self
    except _AbortedStart:
        with self._lifecycle_lock:
            self._lifecycle_state = "aborted"
        raise PiRpcProcessError("harness start aborted by concurrent close")
    except BaseException:
        with self._lifecycle_lock:
            self._lifecycle_state = "aborted"
        raise
    finally:
        self._start_complete.set()  # F2 — ALWAYS, regardless of exit path
```

### `close()` body skeleton

```python
def close(self) -> None:
    with self._close_lock:
        if self._close_complete.is_set():
            return
        with self._lifecycle_lock:
            state = self._lifecycle_state
            if state in ("closing", "closed"):
                # Already being closed by another thread; wait for it.
                outer_close_owner = False
            elif state == "starting":
                self._aborted = True
                outer_close_owner = True
                # Will wait for _start_complete after releasing lock.
            elif state in ("new", "started", "aborted"):
                self._lifecycle_state = "closing"
                outer_close_owner = True
            else:
                raise AssertionError(f"unknown state {state}")

    if not outer_close_owner:
        self._close_complete.wait()  # Unbounded; the owner sets it (F10 review fix from phase 3).
        return

    # If we're aborting a start, actively unblock and wait for start to finish.
    with self._lifecycle_lock:
        was_starting = self._lifecycle_state in ("starting", "aborted")
    if was_starting:
        self._active_unblock_start_in_progress()
        self._start_complete.wait()  # Unbounded; bounded in practice by the unblock list.
        with self._lifecycle_lock:
            self._lifecycle_state = "closing"

    try:
        self._cleanup()
    except Exception:
        logger.exception("close: cleanup raised; state will still be 'closed'")
    finally:
        with self._lifecycle_lock:
            self._lifecycle_state = "closed"
        self._close_complete.set()


def _active_unblock_start_in_progress(self) -> None:
    """Wake any in-flight cut point so start() reaches its finally."""
    pi = self.pi
    if pi is not None and pi.process is not None:
        pi._force_terminate_subprocess(pi.process)
    srv = self.server
    if srv._http_server is not None:
        with contextlib.suppress(Exception):
            srv._http_server.shutdown()


def _cleanup(self) -> None:
    """Single-owner cleanup. Only ever called from close()."""
    # Reverse-order undo. _close_sync() methods are self-serializing
    # via their own locks (phase 2 / phase 3).
    pi = self.pi
    if pi is not None:
        with contextlib.suppress(Exception):
            pi._close_sync()
        self.pi = None
    with contextlib.suppress(Exception):
        self.server._close_sync()
    if self._tempdir is not None and not self.keep_temp:
        with contextlib.suppress(Exception):
            self._tempdir.cleanup()
        self._tempdir = None
```

## Async-shim removal

### `server.py`
- Drop `async def start/close/__aenter__/__aexit__`. Rename `_start_sync`/`_close_sync` to `start`/`close`. Add sync `__enter__`/`__exit__`.
- Drop the per-execute `asyncio.run(collect_tool_result(...))`. The handler thread invokes `collect_tool_result(raw, ctx)` directly (now sync).
- `_BridgeHandler._handle_execute` no longer needs the `async def _run_tool()` wrapper.
- **KEEP the explicit `except asyncio.CancelledError:` arm** (F1 review fix — see § Risks #1).

### `rpc.py`
- Drop all `async def` shims (the 20+ methods at the bottom of the class). Rename `_*_sync` core methods to public names.
- Drop `async def __aenter__/__aexit__`; add sync `__enter__/__exit__`.
- The `_force_terminate_subprocess` helper stays as a public-ish primitive (used by `harness._active_unblock_start_in_progress`).
- Add `PythonToolServer` single-use check (per F11 review fix): `start()` raises `RuntimeError` if `close()` has run.

### `tools.py`
- `ctx.update` becomes `def`. Body: writes via `_update_callback`. No `await`.
- `collect_tool_result` becomes `def`. Drops the `async for` branch. Drops the `inspect.isawaitable(value)` branch (rejected at registration). **Drops the sync-gen branch (F10 review fix — commit to drop, no deprecation cycle).**
- `ToolRegistry.register` rejects:
  - `inspect.iscoroutinefunction(fn)` → `ToolError("async def tools are not supported")`.
  - `inspect.isasyncgenfunction(fn)` → `ToolError`.
  - `inspect.isgeneratorfunction(fn)` → `ToolError`.
- New runtime guard in `collect_tool_result`: rejects awaitable / async-gen / generator / `AsyncIterable` (descriptor-bypass detection).

### `harness.py`
- `async def start/close/__aenter__/__aexit__` → sync. Body: lifecycle state machine above.

### In-tree `_*_sync` caller rewrites (F7 review fix)

ALL of the following files contain direct `_*_sync` calls and MUST be rewritten in the same commit that drops the suffix:

- `tests/pi/test_rpc_fake.py` — many calls (35+ tests use `_start_sync`, `_send_sync`, `_close_sync`, etc.).
- `tests/pi/test_server.py` — uses `server._start_sync()` and `server._close_sync()` in the `_started_server` context manager.
- `tests/pi/test_set_model.py` — uses `client._start_sync()`, `client._set_model_sync()`, `client._close_sync()`.

After the rename, every call becomes the public name (no suffix). This is the natural migration since phase 4 unifies the public API anyway.

## pytest-asyncio removal

- `pyproject.toml` removes `pytest-asyncio` from dev deps.
- `pyproject.toml` removes `[tool.pytest.ini_options]` `asyncio_mode = "auto"`.
- AST contract guard already asserts no `pytest_asyncio` imports; widening scope (below) catches regressions.

## Phase-4 Makefile guard (per F6 review fix)

v1's shell parsing was broken (Markdown file with prose trailer; SHA contains digits). v2 uses a small Python helper script:

```python
# scripts/check-bisect-baseline.py
import re, sys, subprocess
ROW_RE = re.compile(r"^([0-9a-f]{40}|PHASE\d+_PLACEHOLDER)\t(\d+)\t([0-9a-f]{64})$")
lines = open("dev-notes/rewrite-bisect-baseline.md").read().splitlines()
rows = [m.groups() for ln in lines if (m := ROW_RE.match(ln))]
last_sha, last_count, last_hash = rows[-1]
# 1. Substitute placeholder if previous row was PHASE*_PLACEHOLDER.
prev_sha = rows[-2][0] if len(rows) >= 2 else None
if prev_sha and prev_sha.startswith("PHASE"):
    parent = subprocess.check_output(["git", "rev-parse", "HEAD^"]).decode().strip()
    print(f"ERROR: previous row is {prev_sha}; phase commit must substitute it with {parent}")
    sys.exit(1)
# 2. Test count non-decreasing.
current_count = int(subprocess.check_output(
    ["./local.venv/bin/pytest", "--collect-only", "-qq",
     "tests/pi/", "-m", "not live"]
).decode().count("::"))
if current_count < int(last_count):
    print(f"ERROR: test count decreased ({last_count} → {current_count})")
    sys.exit(1)
# 3. sha256 of sorted nodeids matches the last row.
current_hash = ...  # subprocess
if current_hash != last_hash:
    print(f"ERROR: nodeid signature mismatch")
    sys.exit(1)
print("baseline OK")
```

Makefile target:

```makefile
threads-rewrite-phase4-guard:
	$(PY_311) scripts/check-bisect-baseline.py
```

Pre-commit hook on phase branches (optional): assert `dev-notes/rewrite-bisect-baseline.md` is in `git diff --cached --name-only` for any phase commit.

## Bisect-baseline retroactive cleanup (per F14)

Phase 4's first action: substitute the real phase-3 SHA (now `6c5a809...`) for the `PHASE3_PLACEHOLDER` row in `dev-notes/rewrite-bisect-baseline.md`. Append phase-4's placeholder row.

The Python guard script (above) catches the case where a future phase commit forgets to substitute.

## Test enumeration (per F13 review fix)

v1 said "10-15 tests"; v2 enumerates them. New lifecycle tests in a new `tests/pi/test_lifecycle.py`:

| #  | Name                                                                              |
| -- | --------------------------------------------------------------------------------- |
| 1  | `test_lifecycle_new_to_started_to_closed`                                         |
| 2  | `test_close_from_new_is_noop_and_state_becomes_closed`                            |
| 3  | `test_start_from_closed_raises_runtime_error`                                     |
| 4  | `test_start_from_started_raises_runtime_error`                                    |
| 5  | `test_double_close_from_two_threads_one_owner_other_waits`                        |
| 6  | `test_close_during_starting_at_each_cut_point` (parametrized × 6 cut points)      |
| 7  | `test_start_failure_at_each_cut_point_raises_original_exc` (parametrized × 6)     |
| 8  | `test_start_failure_then_close_runs_cleanup_exactly_once`                         |
| 9  | `test_cleanup_exception_still_sets_closed_state`                                  |
| 10 | `test_async_def_tool_rejected_at_registration`                                    |
| 11 | `test_async_gen_tool_rejected_at_registration`                                    |
| 12 | `test_sync_gen_tool_rejected_at_registration`                                     |
| 13 | `test_descriptor_bypass_async_call_rejected_at_runtime` (lambda→coro)             |
| 14 | `test_ctx_update_is_sync_no_await_needed`                                         |
| 15 | `test_cancelled_error_in_tool_body_still_returns_response_after_asyncio_run_drop` |
| 16 | `test_python_tool_server_single_use_close_then_start_raises`                      |
| 17 | `test_async_with_harness_raises_typeerror` (migration sentinel)                   |

Each test uses `@pytest.mark.timeout(10)` for hang protection. Tests 6 and 7 use **injectable cut-point callbacks** per Opus M-test recommendation — `PiPythonHarness` exposes a `_test_cut_point_hook` Optional callable that's invoked between cut points; tests monkey-patch it to inject failures or signal close.

## Risks / open questions (v2 — corrected)

### #1. `asyncio.CancelledError` after dropping `asyncio.run` (F1 — corrected from v1)

`asyncio.CancelledError` inherits from `BaseException`, not `Exception`. Loop state is irrelevant; the MRO is fixed at class-definition time:

```text
$ python -c "import asyncio; print(asyncio.CancelledError.__mro__)"
(<class 'asyncio.CancelledError'>, <class 'BaseException'>, <class 'object'>)
```

A sync tool body that explicitly raises `asyncio.CancelledError` would propagate up through `socketserver.process_request_thread` (which catches `except Exception:`) and leave the client with EOF + no response — re-introducing phase 2's F1 bug.

**Decision:** **Keep the explicit `except asyncio.CancelledError:` arm** in the new sync `_handle_execute`. Map to "tool execution cancelled" response, same observable as today. Add regression test (#15 above).

### #2. `ctx.update` without `bridge_write_timeout` blocks forever

Same as today. Phase 4 doesn't change this. Documented in phase 2's docstring.

### #3. Live test infrastructure

`test_real_pi_integration.py` needs real pi installed. `make test-live` runs them; not in `make all`. Phase 4 rewrites the test bodies but doesn't change the infrastructure.

### #4. `docs/DESIGN.md` migration

The 14-row breaking-changes table is in the v4 plan; v2 adds 1 more row (per F12: "blocking client calls from async code require `asyncio.to_thread`"). Estimated 300-500 lines of doc churn.

### #5. Helper-thread reentrancy

Documented in phase 3 as unsupported; phase 4 doesn't change. Phase 5+ may add `concurrent.futures.ThreadPoolExecutor` if needed.

### #6. Resource leak if `start()` fails and caller forgets `close()` (F3 trade-off)

Per the single-owner-cleanup decision (F3 review fix), `start()` doesn't clean up on failure — `close()` does. If the caller catches the exception and never calls `close()`, the partial state leaks until process exit (daemon threads + TemporaryDirectory weakref cleanup).

**Mitigation:** the documented usage pattern is `with PiPythonHarness(...) as h:` — `__exit__` always calls `close()`. Test that exits via exception still trigger close. Add a `__del__` that calls close as a defensive fallback (best-effort; not load-bearing because GC timing is undefined under FT).

### #7. `_active_unblock_start_in_progress` interleaves with `_start_sync` (per F5)

`close()` calls `pi._force_terminate_subprocess(pi.process)` while `_start_sync` may still be running. Phase 3's `_force_terminate_subprocess` is bounded (SIGTERM/wait/kill/wait). The interleave is safe BECAUSE `_force_terminate_subprocess` doesn't touch internal `PiRpcClient` state — it just calls `proc.terminate()`/`proc.kill()`. `_start_sync`'s startup probe (`self.process.poll() != None`) detects the dead process and raises; that raise reaches `start()`'s try/finally and sets `_start_complete`. close() then waits for `_start_complete` and proceeds.

## Approval gates (v2 — corrected per F11)

Before implementation begins:

1. **5-state machine** with single-owner cleanup (v2 design above). Author sign-off.
1. **`close()` unbounded wait + active unblock + cut-point reordering** (v2 design). Author sign-off.
1. **Drop sync generators at phase 4** (F10 — drop now, no deprecation cycle).
1. **Bisect-baseline Python guard script** (F6 — instead of shell parsing).
1. **`PiRpcClient`, `PiPythonHarness`, AND `PythonToolServer` all single-use** (F11). Author sign-off on the server addition.
1. **Three adversarial reviewers on v2** — same loop as plan reviews. If v2 converges to "approved with minor tweaks," implementation begins. If v2 surfaces new structural issues, revise to v3.

`PiRpcError` hierarchy (phase 3) is **locked-in**, not under design review.

After review-loop convergence, phase 4 implementation begins. Same per-phase loop as phases 1-3.

## Estimated cost (mostly unchanged from v1)

| Component                                  | LOC change |
| ------------------------------------------ | ---------- |
| `harness.py` rewrite (lifecycle state machine + skeleton) | ~270 (was 220 in v1) |
| `server.py` async-shim removal + sync ctx.update + keep CancelledError arm | ~50 deleted, ~5 added |
| `rpc.py` async-shim removal                | ~80 deleted |
| `tools.py` rejection logic + sync collect_tool_result | ~60 |
| `test_real_*.py` rewrites                  | ~40        |
| `test_rpc_fake.py` / `test_server.py` / `test_set_model.py` `_*_sync` → public rename (F7) | ~30 (mechanical) |
| `test_lifecycle.py` (new, 17 tests per F13) | ~250 (was implicit in v1) |
| `test_async_contract_guard.py` widening    | ~10        |
| `pyproject.toml` cleanup                   | ~5         |
| `docs/DESIGN.md` migration + 15-row breaking-changes table | ~420 |
| `CLAUDE.md` Conventions update             | ~10        |
| `scripts/check-bisect-baseline.py` (new, per F6) | ~50 |
| `Makefile` phase-4 guard                   | ~10        |
| Bisect-baseline SHA substitution + rows    | ~10        |

Total: ~1100 LOC touched, ~150 deleted, ~700 new (the new test_lifecycle.py is most of the additions). Larger than v1's estimate because v2 enumerated tests explicitly per F13.
