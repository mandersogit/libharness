---
status: "Draft"
created: "2026-05-16"
---

# Phase 4 design proposal — harness.py rewrite + async-shim removal

Per the phase-1 handoff doc's recommendation: pause after phase 3, write a 1-2 page design doc (not a 1000-line plan) informed by phase 1-3 code, run 3 reviewers on the design, then implement.

**Status:** Draft, awaiting design review. Once reviewed and approved, this becomes the implementation contract for phase 4. The implementation will follow the per-phase loop (implement → impl summary → codex test plan → tests + flake check → 3 adversarial reviewers → fix → commit).

## Phase 4 scope

1. **Rewrite `src/libharness/pi/harness.py`** as sync: lifecycle state machine, partial-start cleanup, close-during-start handling, always-finalizing close.
1. **Drop async shims** from `PiRpcClient` (phase 3) and `PythonToolServer` (phase 2). Rename `_*_sync` core methods to the public names.
1. **Drop per-execute `asyncio.run`** in `server.py`. Tool bodies run directly on the handler thread. `collect_tool_result` becomes sync. `ctx.update` becomes sync.
1. **Reject `async def` / generator / async-gen** tool functions at registration in `tools.py`.
1. **Rewrite live tests as sync**: `test_real_pi_integration.py`, `test_real_llm.py`. `with` not `async with`. Inline async tool bodies → sync.
1. **Drop `pytest-asyncio`** dev dep and `asyncio_mode = "auto"` ini option.
1. **Widen the AST contract guard** to all of `tests/pi/`.
1. **Makefile phase-4 guard**: `threads-rewrite-phase4-guard` asserts AST guard passes + `pytest --collect-only` count matches baseline + sha256 of sorted nodeids matches baseline.
1. **Bisect-baseline retroactive cleanup**: substitute the real phase-2 commit SHA for the `PHASE2_PLACEHOLDER` row; append phase-3 + phase-4 rows.
1. **docs/DESIGN.md migration + § Breaking changes** (14-row table — per v4 plan).
1. **CLAUDE.md update**: drop the "Async-first" convention; pin "threading-first" + helper-thread reentry caveat + single-use lifecycle.

## Lifecycle state machine

The author's lessons from phase 2-3 (T1.3-v4 weakening, close-during-start hazards, the v4 plan's `_start_complete` deadlock) inform the design. The state machine is **simpler than the v4 plan's seven-state proposal** because phase 1-3 already shipped most of the hazardous primitives (close serialized via lock, fatal under lock, weakened "no callbacks after close" postcondition). The harness mostly delegates to the lower layers.

### States

Five states. (v4 plan proposed seven; the `aborting` and `failed` states collapse into the same cleanup path on this design.)

| State      | Meaning                                                                              |
| ---------- | ------------------------------------------------------------------------------------ |
| `new`      | Constructed, not started.                                                            |
| `starting` | `start()` running; may transition to `started`, or via `_aborted` to `closed`.       |
| `started`  | All cut points succeeded; harness is live.                                           |
| `closing`  | `close()` running; lower layers are being torn down.                                 |
| `closed`   | Terminal state. Either succeeded cleanup or failed cleanup (logged, not raised).     |

### Transitions

```text
            ┌──────────────────────────────────────────────────────────┐
            │                                                          │
new ──start()──→ starting ─────────────────────→ started ─close()─→ closing ──→ closed
                    │                              ↑                            ↑
                    │                              │                            │
                    └──── (cut-point failure OR ───┘ _aborted set during start; │
                          _aborted set) ──→ _cleanup() ─────────────────────────┘
```

There's NO `aborting` or `failed` intermediate state. Both abort-during-start and start-failure go through the same `_cleanup()` function and end up at `closed`. This is the v4-synthesis T1.2/T1.3 lesson: don't pin states the implementation can't deliver cleanly.

### Concurrent close-during-start: active unblock, NOT bounded wait

The v4 plan had `close()` wait up to 10 s on `_start_complete` before running cleanup. The v4 synthesis (T1.2-v4) caught that as a re-introduction of the v3 race: if start blocks longer than 10 s (slow pi launch, etc.), cleanup runs concurrently with start.

**Design:** unbounded wait, with **active unblock** of each cut point. `close()`:

1. Acquires `_lifecycle_lock`.
2. If state is `closed`/`closing`: waits on `_close_complete` and returns.
3. If state is `starting`: sets `_aborted = True`. Releases the lock. **Actively unblocks each cut point** in reverse order (the cut points are ordered; we unblock the latest-reached one and walk back):
   - If `self.pi` is set: call `self.pi._close_sync()` (synchronous; bounded by phase-3 close logic).
   - If `self.server` was started: call `self.server._close_sync()` (synchronous; bounded by phase-2 close logic).
   - If `self._tempdir` is set: it's a `TemporaryDirectory`; safe to leave for the start path's exception handler (we don't double-cleanup).
   - The shim/faux-extension write step has no unblock; if start is mid-write, we wait for it to finish or fail naturally.
4. Now waits on `_start_complete` **unbounded** (the unblocks above bound it). When set, transitions to `closing`, runs `_cleanup()`, transitions to `closed`, sets `_close_complete`.

Net: `close()` cleanup is bounded by the cut-point timeouts (`PiRpcClient.config.startup_timeout` etc.), not an arbitrary timer. This is the v4-synthesis recommendation (a) verbatim.

### Partial-start cleanup

Cut points in `start()` (in order):

1. `self.server.start()` (bridge bind).
2. `_artifact_root()` (tempdir creation if needed).
3. `write_bridge_shim(shim_path, ...)`.
4. `write_faux_toolcall_provider_extension(...)` (only if `fake_provider=True`).
5. `self.pi = PiRpcClient(config)` (construction).
6. `self.pi.start(extension_paths=...)` (subprocess launch).

Each cut point that successfully completed must be undone by `_cleanup()` on either failure or abort. `_cleanup()` is **idempotent** and runs lower-layer cleanups in **reverse order**:

```python
def _cleanup(self) -> None:
    # Reverse-order undo.
    if self.pi is not None:
        with contextlib.suppress(Exception):
            self.pi._close_sync()
        self.pi = None
    if self.server._http_server is not None:
        with contextlib.suppress(Exception):
            self.server._close_sync()
    if self._tempdir is not None and not self.keep_temp:
        with contextlib.suppress(Exception):
            self._tempdir.cleanup()
        self._tempdir = None
```

`start()`'s `try` wraps the whole sequence; the `except` calls `_cleanup()` and re-raises. The state machine guarantees `_cleanup()` runs at most once per harness instance.

### Restart: unsupported (single-use)

`start()` from any state other than `new` raises `RuntimeError("harness cannot be restarted; create a new instance")`. The v4-synthesis Tier-3 noted this is the simplest correctness story — no need to reason about post-close state revival. Documented in `docs/DESIGN.md` § Breaking changes.

### Always-finalizing close

`close()` body is wrapped in `try/finally`. Final `try/finally` block:

```python
try:
    self._cleanup()
except Exception:
    logger.exception("close: cleanup raised; state will still be 'closed'")
finally:
    with self._lifecycle_lock:
        self._lifecycle_state = "closed"
    self._close_complete.set()
```

`_close_complete.set()` always runs, regardless of cleanup exceptions. Concurrent waiters return.

## Async-shim removal

### `server.py`
- Drop `async def start/close/__aenter__/__aexit__`. Rename `_start_sync`/`_close_sync` to `start`/`close`. Add sync `__enter__`/`__exit__`.
- Drop the per-execute `asyncio.run(collect_tool_result(...))`. The handler thread invokes `collect_tool_result(raw, ctx)` directly (now sync).
- `_BridgeHandler._handle_execute` no longer needs the `async def _run_tool()` wrapper.

### `rpc.py`
- Drop all `async def` shims (the 20+ methods at the bottom of the class). Rename `_*_sync` core methods to public names. Drop the `_sync` suffix everywhere.
- Drop `async def __aenter__/__aexit__`; add sync `__enter__/__exit__`.

### `tools.py`
- `ctx.update` becomes `def`. Body: writes via `_update_callback`. No `await`.
- `collect_tool_result` becomes `def`. Drops the `async for` (async-gen) branch. Drops the `inspect.isawaitable(value)` branch (rejected at registration). Keeps the sync-gen branch for one release (or drops, per breaking-changes table — leaning drop).
- `ToolRegistry.register` rejects:
  - `inspect.iscoroutinefunction(fn)` → `ToolError("async def tools are not supported; see docs/DESIGN.md § Breaking changes")`.
  - `inspect.isasyncgenfunction(fn)` → `ToolError`.
  - `inspect.isgeneratorfunction(fn)` → `ToolError` (per v4 plan; "no sync generators" breaking change).
- New runtime guard in `collect_tool_result`: rejects awaitable / async-gen / generator / `AsyncIterable` (descriptor-bypass detection).

### `harness.py`
- `async def start/close/__aenter__/__aexit__` → sync. Body: delegates to `self.server.start/close` (now sync) and `self.pi.start/close` (now sync).

## Test rewrites

- `test_real_pi_integration.py`: `async with PiPythonHarness(...)` → `with PiPythonHarness(...)`. Inline `async def echo` → `def echo`. `await ctx.update(...)` → `ctx.update(...)`. `await client.X()` → `client.X()`.
- `test_real_llm.py`: same shape.
- Existing async test in `test_server.py` (`test_async_shim_preserves_manifest_execute_and_updates`) is **deleted** (the async shims are gone). The phase-2 sync tests cover the protocol.
- `tests/pi/test_async_contract_guard.py`: `SCOPED_FILES` widens to include `test_real_pi_integration.py` + `test_real_llm.py`. After phase 4, `SCOPED_FILES == {every test file}`.

## pytest-asyncio removal

- `pyproject.toml` removes `pytest-asyncio` from dev deps.
- `pyproject.toml` `[tool.pytest.ini_options]` removes `asyncio_mode = "auto"`.
- The AST contract guard already asserts no `pytest_asyncio` imports; widening its scope (above) catches any regressions.

## Phase-4 Makefile guard

```makefile
threads-rewrite-phase4-guard:
    # 1. AST contract guard (scope is now all tests/pi/).
    $(PY_311) -m pytest tests/pi/test_async_contract_guard.py -q
    # 2. Collect-only count matches the last baseline row.
    @count=$$($(PY_311) -m pytest --collect-only -qq tests/pi/ -m "not live" | grep -c '::'); \
        last_count=$$(tail -n 1 dev-notes/rewrite-bisect-baseline.md | grep -oE '\b[0-9]+\b' | head -1); \
        [ "$$count" = "$$last_count" ] || { echo "collect count mismatch: $$count vs $$last_count"; exit 1; }
    # 3. sha256 of sorted nodeids matches the last baseline row.
    @hash=$$($(PY_311) -m pytest --collect-only -qq tests/pi/ -m "not live" | grep '::' | sort | sha256sum | cut -d' ' -f1); \
        last_hash=$$(tail -n 1 dev-notes/rewrite-bisect-baseline.md | grep -oE '[a-f0-9]{64}$$'); \
        [ "$$hash" = "$$last_hash" ] || { echo "nodeid signature mismatch"; exit 1; }
```

Optional pre-commit hook on phase branches: assert `dev-notes/rewrite-bisect-baseline.md` is in `git diff --cached --name-only` for any phase commit.

## Bisect-baseline retroactive cleanup

Phase 2 used `PHASE2_PLACEHOLDER` for the SHA (couldn't know it pre-commit). Phase 4's first action: substitute the real phase-2 SHA (now `30c4560...`) in `dev-notes/rewrite-bisect-baseline.md`, append phase-3's row (SHA known after phase 3 commits), and append phase-4's placeholder row.

Future phase commits land the SHA-substitution in the same commit as the new phase's row.

## Risks / open questions

1. **Drop sync generators or keep?** The v4 plan dropped them (one of the 14 breaking changes). Phase 1+2 left them in tools.py untouched. Phase 4 has to decide: keep + add deprecation warning, or drop now? **Recommendation: drop now.** Per v4 plan; no need to maintain a path that ships once and then gets removed.

2. **`asyncio.CancelledError` from tool bodies after dropping `asyncio.run`.** The server's handler thread no longer runs an event loop. A tool body that raises `asyncio.CancelledError` (no loop active) is just a regular exception — the existing `except Exception` catches it. The phase 2 special case (CancelledError outside Exception) goes away. Test: tool raises `asyncio.CancelledError` → response carries the exception, like any other.

3. **`ctx.update` without `bridge_write_timeout` blocks forever.** Same as today. Phase 4 doesn't change this. Documented in phase 2's docstring.

4. **Live test infra**: `test_real_pi_integration.py` needs real pi installed. `make test-live` runs them; not in `make all`. Phase 4 doesn't change this — just rewrites the test bodies.

5. **`docs/DESIGN.md` migration size.** The 14-row breaking-changes table is in the v4 plan; that's the source. Phase 4 transcribes it into `docs/DESIGN.md` with the migration prose. Estimated 300-500 lines of doc churn.

6. **Helper-thread reentrancy** (handler spawns worker that calls `client.send`) — phase 3 documented as unsupported; phase 4 doesn't change. Phase 5+ may add `concurrent.futures.ThreadPoolExecutor` if needed.

## Estimated cost

| Component                                  | LOC change |
| ------------------------------------------ | ---------- |
| `harness.py` rewrite (lifecycle state machine) | ~220       |
| `server.py` async-shim removal + sync ctx.update | ~50 deleted |
| `rpc.py` async-shim removal                | ~80 deleted |
| `tools.py` rejection logic + sync collect_tool_result | ~60 |
| `test_real_*.py` rewrites                  | ~40        |
| `test_async_contract_guard.py` widening    | ~10        |
| `pyproject.toml` cleanup                   | ~5         |
| `docs/DESIGN.md` migration + breaking-changes table | ~400 |
| `CLAUDE.md` Conventions update             | ~10        |
| `Makefile` phase-4 guard                   | ~30        |
| Bisect-baseline SHA substitution + rows    | ~10        |

Total: ~700 LOC touched, ~250 deleted, ~450 new. Tests: ~10-15 new (lifecycle state machine + restart-rejected + partial-start cleanup parametrized × 6 cut points + async-tool rejection at registration).

## What's NOT in phase 4

- Event bridge co-design (separate gate; `dev-notes/2026-05-14-event-bridge-proposal.md`).
- Helper-thread reentrancy detection.
- Per-frame deadline option (the slowloris mitigation phase 2 review flagged as MODERATE; defer).
- Pi-native session API typed methods (per SESSION-STATE.md pending tasks; land "as part of or after the threaded rewrite" — phase 4 or 5).

## Approval gates

Before implementation begins:

1. Author signs off on the **5-state state machine** (vs v4 plan's 7-state).
1. Author signs off on **`close()` unbounded wait with active unblock** (vs v4 plan's 10s-bounded wait).
1. Author signs off on **dropping sync generators** at phase 4 (vs deprecation cycle).
1. Author signs off on the **bisect-baseline retroactive SHA substitution** as a "good enough" alternative to pre-commit-hook enforcement.
1. Three adversarial reviewers on this doc — same loop as plan reviews.

After review-loop convergence, phase 4 implementation begins. Same per-phase loop as phases 1-3.
