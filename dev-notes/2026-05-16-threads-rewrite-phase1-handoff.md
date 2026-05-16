---
status: "In progress"
created: "2026-05-16"
---

# Threads-rewrite phase 1 handoff (mid-flight)

Handoff document written at ~93% context utilization in the session that drove phase 1 implementation + tests. Captures state so a fresh session can pick up at the phase-1 review-finding-fix-commit point and continue through phases 2-4.

## Where we are

**Phase 1 of the threads rewrite is implemented and tested.** Adversarial reviews are running in background; review findings have NOT yet been read or addressed. No commit yet.

## Per-phase loop (decided this session)

Per user proposal + my push-back, each phase uses this loop:

1. Implement (against v4 plan + v4-synthesis Tier-1 TODOs).
2. Write implementation summary (~500-800 words, what built, what chose, what deviated, what TODOs addressed).
3. Codex (gpt-5.5 xhigh) generates a test plan from the diff + summary + plan + synthesis. Single pass. Output to `/tmp/phase{N}-test-plan.md`.
4. Implement tests; achieve all-pass on both 3.11 and 3.14t venvs; flake check (3 consecutive runs, zero flakes).
5. Adversarial code review with 3 reviewers (2 codex gpt-5.5 xhigh + 1 Opus subagent). Each reads the diff and may run the tests.
6. Fix review findings.
7. Commit via commit-plans skill.
8. Move to next phase.

After phase 3: pause and propose a phase 4 design (the v4 plan's harness lifecycle had 4 Tier-1 findings; redesign with phase 1-3 evidence as input).

## Mechanism decisions

- **(a) Keep `pytest-asyncio` through phase 3.** Drop only at phase 4 when the async test files (`test_real_*.py`) get rewritten. Phases 1-3 commits keep the suite green; the AST contract guard widens scope at each phase.
- **3 reviewers per phase** (not 6 like plan-review passes). Code is concrete enough that 3 independent passes catch most issues.
- **gpt-5.5 default** for all codex invocations (codex-cli skill already updated, memory saved).

## Phase 1 scope (revised down from v4 plan)

The v4 plan's phase 1 included sync `ctx.update`, sync `collect_tool_result`, async-tool registration rejection, and the runtime guard rejecting awaitable/async-gen/generator/`AsyncIterable`. **All deferred** because they would break the still-asyncio server.py / rpc.py / harness.py and the existing async-tool tests.

**Implemented (additive only):**

- `src/libharness/pi/tools.py`:
  - `import threading`, `import copy`.
  - `ToolRegistry.__init__` adds `self._lock = threading.Lock()`.
  - `register`, `get`, `__contains__`, `__iter__`, `manifest` all guarded by `self._lock` (with deferred-release pattern in `__iter__` to prevent deadlock if handlers re-enter).
  - New `ImmutableRegistry` class with `__slots__ = ("_tools",)`, explicit `_tools: dict[str, RegisteredTool]` annotation. `get/__contains__/__iter__/__len__/manifest`. Constructor **deepcopies** the tools dict (so `ToolSpec.parameters` mutation in live registry doesn't leak into snapshot).
  - New `ToolRegistry.snapshot() -> ImmutableRegistry` method (acquires lock, returns deepcopied frozen view).
  - `ToolContext._cancelled` type widened from `asyncio.Event | None` to `asyncio.Event | threading.Event | None`. Both expose `.is_set()`.
- `pyproject.toml`: added `pytest-timeout>=2.0` to dev deps.
- `tests/pi/test_runtime_meta.py` (new, ~95 LOC): 3.11-safe via `getattr(sys, "_is_gil_enabled", lambda: True)()`. Writes `.pytest-runtime-meta/<tag>.json`. Tag from `LIBHARNESS_VENV` env var.
- `tests/pi/test_async_contract_guard.py` (new, ~225 LOC): AST walker scoped to phase-1 files (`test_tools.py`, `test_jsonl.py`, `test_runtime_meta.py`, `test_async_contract_guard.py`). 9 tests including detector self-tests for async-def/fixture/mark/imports, plus the pyproject `pytest-timeout` dep check.
- `tests/pi/test_tools.py`: extended from 2 tests to 14 tests. Added cancellation dual-event support, snapshot isolation, snapshot deepcopy freezing, immutable-registry surface, `__iter__` point-in-time, concurrent register/snapshot/manifest stress under FT.
- `dev-notes/rewrite-bisect-baseline.md` (new): seeded with initial row `257247a0530a2037a19646e5dee7d95f36f7db89 14 a329639e9917f3627e3d6d963e22228c3e4b50de27a5715fcfe30d2e057737de`.

**Test results: 31/31 pass on both 3.11 and 3.14t**. Lint + mypy + pyright clean. Flake check (6 consecutive runs): zero flakes. ~0.5s suite time.

## Files modified (uncommitted)

```text
src/libharness/pi/tools.py            modified (~80 LOC added)
pyproject.toml                        modified (+5 LOC for pytest-timeout)
tests/pi/test_tools.py                modified (+~310 LOC, 2 → 14 tests)
tests/pi/test_runtime_meta.py         new (~95 LOC)
tests/pi/test_async_contract_guard.py new (~225 LOC)
dev-notes/rewrite-bisect-baseline.md  new (~20 LOC)
dev-notes/2026-05-16-threads-rewrite-phase1-handoff.md  new (this file)
```

## In-flight: phase 1 adversarial reviews

Three reviewers launched in background (running at handoff time):

- `bkh5nt9lp` — codex gpt-5.5 xhigh, output `/tmp/phase1-review-codex-A.md`.
- `bmnjfdgte` — codex gpt-5.5 xhigh, output `/tmp/phase1-review-codex-B.md`.
- Opus subagent — output `/tmp/phase1-review-opus.md`.

Prompt: `/tmp/phase1-review-prompt.md`. Inputs given to reviewers:
- `dev-notes/2026-05-15-threads-rewrite-plan.md` (v4 plan)
- `dev-notes/2026-05-16-threads-rewrite-plan-v4-review-synthesis.md` (v4 synthesis)
- `/tmp/phase1-implementation-summary.md`
- `/tmp/phase1-test-plan.md` (codex's plan)
- `/tmp/phase1-full-diff.diff`
- Current source files

## Pickup instructions for the fresh session

1. **Read this handoff doc, then `git status` and `git diff`** to see the uncommitted phase 1 changes.
2. **Read the v4 plan + v4 synthesis** (cited above) so design choices are fresh.
3. **Check phase 1 review output files**: `/tmp/phase1-review-codex-A.md`, `/tmp/phase1-review-codex-B.md`, `/tmp/phase1-review-opus.md`. They should be complete by the time you read this (each takes ~5-10 min wall clock).
4. **Synthesize findings** across the 3 reviews. Triage: must-fix (CRITICAL/HIGH) vs nice-to-have (MINOR).
5. **Fix the must-fix findings.** Re-run `make all` on both venvs after each fix.
6. **Commit phase 1** via the commit-plans skill. Suggested commit message:

    ```
    feat(threads-rewrite): phase 1 — tools.py thread-safety + test infra

    Phase 1 of the threads rewrite. Additive only (no signature changes).
    Sync ctx.update / collect_tool_result and async-tool rejection deferred
    to phase 3/4 per the per-phase-greenness discipline.

    See dev-notes/2026-05-15-threads-rewrite-plan.md (v4) for the full plan
    and dev-notes/2026-05-16-threads-rewrite-phase1-handoff.md for phase 1
    decisions and review-finding fixes.
    ```

7. **Move to phase 2** (server.py). Same loop: implement → summary → codex test plan → tests → adversarial review → fix → commit.

## Phase 2 scope (preview)

Per v4 plan § Phase 2 — `server.py`:

- `ThreadingTCPServer` subclass; `process_request` override acquires `_handler_slots` before thread spawn.
- `_active_handler_threads: set[Thread]` for orderly close.
- Handler entry: `request.settimeout(max(1.0, timeout_ms/1000))` → read initial frame within 8 MiB cap → `request.settimeout(None)`.
- Sidecar `recv(1)` distinguishes `b""` (cancel only) from non-empty byte (cancel + `_protocol_violation` flag + warning).
- `PythonToolServer.start()` takes private `ToolRegistry.snapshot()` (uses the `snapshot()` method added in phase 1).
- **v4-synthesis T1.4 fix**: do NOT use `request.settimeout` for `bridge_write_timeout`; use `select.select` write-readiness check instead. (Plan deviated; this is an implementation-time TODO.)
- Test infrastructure: extended `fake_pi_rpc.py` to mode-driven (basic / ui-order / late-response / fatal-invalid-json / parallel-execute / slow-consumer).
- Widen `test_async_contract_guard.py` scope to include `test_server.py`.
- Update bisect-baseline.

**Key v4-synthesis Tier-1 items that hit in phase 2:**

- **T1.4-v4 `bridge_write_timeout`**: implement via `select.select`, not `socket.settimeout` (shared socket race with sidecar).
- **Active handler tracking timing**: track threads inside `process_request` (the override that owns thread creation), at the same point as `_handler_slots.acquire`. Use a barrier in tests to verify the tracking races are closed.

## Phase 3 scope (preview)

Per v4 plan § Phase 3 — `rpc.py`. Major delta. Includes:

- `subprocess.Popen` + reader threads.
- Sync `ctx.update`, sync `collect_tool_result` (drops deferred-async branches; adds runtime guard for awaitable/async-gen/generator/`AsyncIterable`).
- `_pending_lock` with atomic fatal/pending transitions; `_pop_pending` + `_complete_future` helpers (NOT a single `_settle_future`).
- `_closing: threading.Event` for fast-fail; separate from `_send_lock`.
- `_handlers_lock` for event/UI handler registries.
- `_in_dispatch: threading.local` wrapping BOTH `_dispatch_event` AND `_handle_extension_ui_request`.
- UI response priority via **literal v3-synthesis pattern**: `send()` acquires `_send_lock` FIRST, rechecks `_ui_response_pending`, releases+waits+retries if set. (NOT v4's layered locking — that was the v4 review's main CRITICAL finding.)
- New exception classes: `PiRpcCommandError`, `PiRpcProcessError`, `ReentrantRPCError`, `EventQueueEmpty`. All exported.
- `_events = queue.Queue(maxsize=4096)` with rate-limited overflow logging.
- `threading.Timer` watchdog per dispatch.

**Key v4-synthesis Tier-1 items that hit in phase 3:**

- **T1.1-v4 UI ordering**: use literal v3-synthesis pattern, not v4's layered locking. v4 review caught the layered-locking deviation immediately.
- **T1.2-v4 Future settlement**: split into `_pop_pending(req_id)` + `_complete_future(fut, ...)`. NOT a single polymorphic `_settle_future`.
- **T1.3-v4 "no callbacks after close"**: weaken contract to "no NEW callbacks after close begins; in-flight callbacks may continue."
- **Update frame wire shape**: `{"id": req_id, "type": "update", "data": result.to_wire()}` — NOT v4's `{"type": "update", "result": ...}` (preserves shim contract).

## Phase 4 design proposal scope (preview)

After phase 3 lands and is committed: **pause** before phase 4. Then do a focused design pass on harness.py (the lifecycle state machine had 4 of 6 v4 Tier-1 findings). Inform by:

- What phase 1-3 code actually looks like.
- v4 synthesis Tier-1 findings on harness lifecycle: close-during-start race, "no callbacks after close" postcondition, `_start_complete` deadlock, active-handler tracking timing, partial-start `try/finally` for finalization.
- The lessons learned from v2/v3/v4 plan revisions: don't pin contracts the implementation can't deliver; don't add new features (like `bridge_write_timeout`) without thinking through shared-state interactions; follow synthesis recommendations literally instead of paraphrasing.

Phase 4 proposal should be a 1-2 page design doc (not a 1000-line plan). Then run adversarial review on the design (3 reviewers). Then implement.

## Decisions made this session (reference)

- Phase 1 scope reduced to additive-only (per-phase greenness forces this).
- Per-phase loop = implement → summary → codex test plan → tests → 3-reviewer adversarial → fix → commit.
- pytest-asyncio kept through phase 3.
- `_pending_lock` lock-order rule: never held across blocking I/O; never simultaneous with `_send_lock`.
- AST contract guard scope widens at each phase (phase 1: 4 files; later phases add more).
- Bisect-baseline format: `<commit-sha>\t<test-count>\t<sha256-of-sorted-nodeids>`.
- Single codex pass for test-plan generation (vs multi-reviewer for adversarial review).

## What's at risk

- **Reviewer findings may force scope changes.** If a reviewer finds a real bug in `_lock` usage or snapshot semantics, phase 1 commit blocks until fixed.
- **Phase 2 has the `bridge_write_timeout` socket-shared race** (v4-synthesis Tier-1, NOT fixed in v4 plan). The plan text describes the buggy approach; implementation must deviate to `select.select`.
- **Phase 3 has the UI ordering race** (v4-synthesis Tier-1). Plan text describes the buggy "layered locking" approach. Implementation must use the literal v3-synthesis recommendation.
- **Phase 4 design**: the v4 plan's harness lifecycle had 4 Tier-1 findings. The design needs a substantive rework, not just tweaks.

## Suggested check at session start (fresh session)

```bash
# Verify uncommitted state matches this handoff
cd /home/manderso/git/github/libharness
git status
git diff --stat
make all  # should be 31 passed on both venvs

# Review files (should exist by the time fresh session starts)
ls -la /tmp/phase1-review-*.md
```

If `make all` fails or review files are missing, something has drifted from this handoff — investigate before proceeding.
