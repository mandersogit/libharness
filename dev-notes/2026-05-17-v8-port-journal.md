---
status: Active
created: '2026-05-17'
---

# v8 port — implementation journal

Running log of the v8 port sprint, kept by the executing Claude session. Half for the author, half for future-me (post-compaction or post-handoff) to reconstruct context.

**Sprint frame:**

- Plan-of-record: `dev-notes/2026-05-17-v8-port-plan.md` (16 phases/gates total; see § Phased sequencing).
- Companion design doc: `dev-notes/2026-05-17-v8-analysis.md`.
- External advisor input: `dev-notes/2026-05-17-claude-recommendations-for-v8-port.md`.
- Self-handoff (volatile, post-compaction recovery): `dev-notes/2026-05-17-v8-port-handoff.md`.

**Operating mode (granted 2026-05-17):**

- `commit-plans` skill authorized for all sprint commits; no per-commit user review.
- Mid-port design surprises resolved via 4-LLM discussion (2× codex gpt-5.5 xhigh + 2× Opus); proceed with logged resolution if confident, defer otherwise.
- Gate A.5: 8 reviewers (2× codex generalist + 4× codex specialist + 2× Opus generalist, all gpt-5.5 xhigh on codex) + Opus subagent synthesizer.
- Check-in with user at Gate A (source-side done), Gate A.5 (synthesis fix list), Gate C (final diff).
- Journal updated per-phase; handoff doc refreshed frequently as context fills.

## Entry format

Each phase/gate gets a § entry with:

- **Start time** (UTC) and the task ID claimed.
- **Action taken** — what files changed, what shape, with paths.
- **Surprises encountered** — anything the plan didn't anticipate, with resolution path (proceed with note vs defer).
- **Tests/validation** — what ran, what passed, any flakes.
- **Commit** — message + hash once landed.
- **End time** and any open follow-ups added back to TaskList.

If a phase spawns a 4-LLM discussion (mid-port surprise) or an 8-reviewer call (Gate A.5), the prompts, the parallel invocation, the per-reviewer output paths, and the synthesis go into the same § entry.

## Pre-sprint state (2026-05-17, before Phase 1 starts)

Captured at journal creation, for grounding.

**Branch:** `asyncio-in-thread` (spark checkout). Clean working tree apart from two staged setup-tooling fixes (`scripts/lib/env.sh` + `pyproject.toml`) that are the subject of Task #1.

**Source under `src/libharness/pi/`:** v5 baseline — `harness.py`, `__init__.py`, `jsonl.py`, `rpc.py`, `server.py`, `shim.py`, `tools.py`. Total ~1500 LOC.

**v8 source vendored at:** `dev-notes/predecessors/v8-decision-hooks/src/pi_python_harness/` — `runtime.py`, `agent.py`, `agent_class.py` (original), `agent_class_mca.py` (mixin variant, the layout-of-record), plus the v5-equivalent files. Total ~3600 LOC.

**Tests under `tests/pi/`:** v5-era. v8 tests live at `dev-notes/predecessors/v8-decision-hooks/tests/` — 36 tests across 9 files; will get ported in Phase 6.

**Environment:**

- `local.venv/` (3.11) and `local-ft.venv/` (3.14t) built via `make bootstrap` this session.
- `.sandbox/nodeenv/` Node 24.15.0 + npm 11.12.1; `.sandbox/pi-install/` pi 0.74.0.
- `.sandbox/pi-home/.pi/agent/auth.json` copied from sibling checkout; access expires 2026-05-25, refresh token auto-rolls.
- `make smoke-pi` and `make test-live-311` confirmed green at the start of the sprint.
- `codex` v0.125.0 at `~/.local/bin/codex`, verified gpt-5.5 default works.

**External advisor findings carried forward into the port:**

- Check 1 (CRITICAL, `rpc.py:411`): dict-merge key order silently corrupts UI response correlation id. **Fix in Phase 5.5.**
- Check 4 (HIGH, `server.py:235`): `params or {}` falsy coercion bypasses isinstance type check. **Fix in Phase 5.5.**
- Check 7 (MODERATE, `rpc.py:178-183`): startup probe raises without cleaning up reader tasks. **Fix in Phase 5.5.**
- Other checks (2/3/5/6/8) verified not-applicable in v8; documented in the recommendations doc.

**Author resolutions in force (from analysis § Resolutions):**

1. v8 is the libharness baseline (port happens).
1. `AgentHookSurface` is public (no leading underscore).
1. `PiPythonHarness` async-first surface stays for v1.
1. Address items C (strict-mode E2E test) and E (`subscribe_client_events` simplification) during port; defer A, B, D, F.
1. Three-file split: `events.py` + `hook_surface.py` + `agent_class.py`.
1. `_decision_timeout_ms` default stays `None` (HITL case; recommendation to default to 30s rejected).
1. No v9 ask to ChatGPT for now.

**Sprint sequencing reminder (full detail in port plan):**

- Pre-sprint: Task #1 (setup-tooling commit), Task #2 (this journal + handoff).
- Source side: Phases 1-5 (vendor, mixin, gap close, 3-file split, consistency check) → Gate A.
- Bug fixes: Phase 5.5 (cherry-picks) → Gate A.5 (8-reviewer review + Opus synthesis).
- Tests: Phase 6 → Gate B.
- Docs: Phases 7-9.
- Final: Gate C.

______________________________________________________________________

## Phase entries

### 2026-05-17 — Task #2 — Pre-sprint scaffolding (journal + handoff)

Created `dev-notes/2026-05-17-v8-port-journal.md` (this file) and `dev-notes/2026-05-17-v8-port-handoff.md`. Both per-file mdformat + markdownlint clean. Initial revision of the handoff used tables for the paths/commands cheat-sheets; mdformat's pipe alignment pushed widths past 150 cols → converted to bulleted lists, which fit better for the long absolute paths and codex command lines anyway. Files untracked for now; will be committed at first natural milestone (likely post-Gate A docs sweep, or rolled into a "v8 port sprint scaffolding" commit).

No surprises. No tests run for this task.

### 2026-05-17 — Task #1 — Setup-tooling fix (env.sh + pyproject.toml)

**Files changed:** `pyproject.toml`, `scripts/lib/env.sh`.

**What:**

- `pyproject.toml`: added `nodeenv>=1.10` to `[project.optional-dependencies] dev` with an explanatory comment (pyright pulls it transitively; explicit pin makes install-pi's dependency deliberate).
- `scripts/lib/env.sh`: changed `NODEENV_PYTHON` from a hardcoded `/opt/miniforge/envs/base-py3-14/bin/python` to `$VENV/bin/python`. Matches the sibling checkout's working-tree change.

**Why:** bootstrapping on a machine without nodeenv pre-installed in the miniforge env (this spark checkout, before the fix) failed at `make install-pi`. The fix avoids polluting shared conda envs and makes the dependency portable.

**Commit:** `71eadae` — `fix(setup): pin nodeenv as explicit dev dep + point NODEENV_PYTHON at $VENV`.

**Surprises:** none. Sibling had the same change uncommitted, so the resolution was straightforward.

**Tests:** no test changes; the fix is verified by the prior `make bootstrap` + `make smoke-pi` + `make test-live-311` runs in this session.

**Handoff refresh:** Current state updated to reflect commit landed; next task is #18 (journal: post-#2) then #3 (Phase 1).

### 2026-05-17 — Task #3 — Phase 1 — Vendor v8 source + rename namespace

**Action:** copied 12 files from `dev-notes/predecessors/v8-decision-hooks/src/pi_python_harness/` into `src/libharness/pi/`. Skipped `agent_class_mca.py` (Phase 2 swaps to that layout). All v8 source uses relative imports, so zero `pi_python_harness` references survived the copy — namespace rename was a no-op beyond the directory placement.

**Files changed (12 source + 2 commit-plan YAMLs = 14 total):**

- New: `agent.py`, `agent_class.py`, `runtime.py`, `cli.py`.
- Replaced: `__init__.py`, `harness.py`, `jsonl.py`, `rpc.py`, `server.py`, `shim.py`, `tools.py`, `py.typed`.

**Surprise — ruff version-skew:** v8 source was lint-clean against ruff 0.5; libharness uses ruff 0.15.13. 29 issues surfaced (26 auto-fixed via `ruff check --fix`, 3 manual):

- UP037 ×26 (auto-fixed): unquoted type annotations made redundant by `from __future__ import annotations`.
- C408 ×1 (manual): `dict(...)` → dict literal in `agent.py:_core_kwargs`.
- SIM105 ×2 (manual): `try/except/pass` → `contextlib.suppress(...)` in `rpc.py` and `runtime.py`. Added `import contextlib` to both.

All three manual fixes are stylistically equivalent to the v8 originals; no semantic change. Logged here so a future reviewer asking "why does our source diverge from v8?" finds the trail. Not deferred to Phase 5.5 (those are bug fixes, not style fixups).

**Tests/validation:**

- `./local.venv/bin/python -c "from libharness.pi import Agent, PiAgentHarness, HarnessRuntime, AgentEvent, HookContext, UnhandledEventError, close_default_runtime, get_default_runtime"` → all imports OK.
- `make all` green on `local.venv` (3.11): 7 unit tests pass.
- `make all` green on `local-ft.venv` (3.14t): 7 unit tests pass.
- Set_model regression test (`test_set_model.py`) passes — confirms v8 preserved the v5 `model → modelId` wire fix.

**Commit:** `9a57b99` — `feat(pi): vendor v8 source as libharness.pi (decision hooks + opt-in participation)`. Bundled the orphan setup-tooling commit-plan YAML alongside (the prior `71eadae` shipped without its YAML; catch-up here).

**Open follow-ups:** none. Phase 2 (mixin refinement) is next.

**Handoff refresh:** Current state updated; next task is #4 (Phase 2 — apply AgentHookSurface mixin).

### 2026-05-17 — Task #4 — Phase 2 — Apply AgentHookSurface mixin refinement

**Action:** wholesale swap of `src/libharness/pi/agent_class.py` with the contents of `dev-notes/predecessors/v8-decision-hooks/src/pi_python_harness/agent_class_mca.py`. The author had already refactored the v8 source into the mixin layout in-tree post-v8-review; Phase 2 is just "adopt that layout in libharness".

**What changed structurally:**

- The 74 hook ClassVars + 4 frozensets + config ClassVars + 4 type aliases + 3 `_validate_*` classmethods were extracted into a new `AgentHookSurface` class.
- `Agent` now inherits as `class Agent(AgentHookSurface, PiAgentHarness)`.
- Hook declarations reorganized into four colocated blocks (sync notification → sync decision → async notification → async decision) rather than interleaved.

**Tests/validation:**

- MRO: `[Agent, AgentHookSurface, PiAgentHarness, object]` — confirmed via `Agent.__mro__`.
- Hook ClassVar count on `AgentHookSurface`: 74 (matches v8 baseline; Phase 3 closes the gap to 112).
- `make all` green on both venvs; 7 tests pass.

**Surprises:** none of note. Ruff version-skew (continuation of Phase 1's pattern) flagged 2 issues in the mca file, both auto-fixed without semantic change.

**Commit:** `9945438` — `refactor(pi): extract AgentHookSurface mixin from Agent class`.

**Note:** `AgentHookSurface` is NOT yet re-exported from `__init__.py`. Phase 4 (three-file split) is where the import surface gets reorganized; until then, users would access it as `libharness.pi.agent_class.AgentHookSurface`.

**Handoff refresh:** next task #5 (Phase 3 — close the 38-declaration gap).

### 2026-05-17 — Task #5 — Phase 3 — Close the 38-declaration gap

**Action:** added 38 ClassVar declarations to `AgentHookSurface` in `src/libharness/pi/agent_class.py`. For each of the 19 `_DECISION_EVENT_NAMES`: one `on_<event>: ClassVar[SyncAgentHook | None] = None` at the end of the sync-notification quadrant; one `async_on_<event>: ClassVar[AsyncAgentHook | None] = None` at the end of the async-notification quadrant. Order matches the `_DECISION_EVENT_NAMES` frozenset declaration (logical lifecycle, not alphabetical).

**Mechanism:** no behavioral change. v8 already accepted these names via `_OBSERVABLE_EVENT_NAMES = _EVENT_NAMES | _DECISION_EVENT_NAMES` in the dispatcher's validation; this commit just makes the declarative surface match. IDE autocomplete and type-checker override recognition now work for these slots.

**Verified counts (post-Phase-3):**

- `on_*`: 37 (18 RPC notification events + 19 decision-event observation slots)
- `async_on_*`: 37 (same shape)
- `decide_*`: 19
- `async_decide_*`: 19
- **Total: 112** (matches analysis § Gap target)

**Tests/validation:** `make all` green on both venvs. No new tests required for this phase — Phase 5's consistency-check helper will pin the invariant.

**Commit:** `3aada26` — `feat(pi): declare observation hooks for decision events`.

**Handoff refresh:** next task #6 (Phase 4 — three-file split).

### 2026-05-17 — Task #6 — Phase 4 — Three-file split

**Action:** carved the post-Phase-3 monolithic `agent_class.py` (~612 lines) into three files with a one-direction dependency chain (`agent_class → hook_surface → events`).

**File shape after split:**

- `events.py` (~75 lines): AgentEvent, HookContext, UnhandledEventError. No upward deps.
- `hook_surface.py` (~270 lines): AgentHookSurface mixin + 4 frozensets + config ClassVars + 4 hook type aliases + 112 hook ClassVars (4-quadrant order, with sub-headers) + the three `_validate_*` classmethods (moved from Agent, since they read class-level state from the surface).
- `agent_class.py` (~225 lines): Agent class with `__init_subclass__` + lifecycle + dispatchers + install/uninstall helpers + manifest-wiring classmethods (`_initial_open_gates_for_class`, `_decision_timeouts_for_manifest`) + module helpers (`_call_decision_handler`, `_handler_wants_context`) + the `cast(object, Agent)` pyright escape hatch.
- `__init__.py`: re-routes data type imports through `.events`, adds `AgentHookSurface` to exports.

**Surprises:** none. The `_validate_*` move from Agent to AgentHookSurface was straightforward — `Agent.__init_subclass__` calls them via `cls._validate_*` which resolves through MRO.

**Tests/validation:**

- All four import paths from the plan's acceptance work.
- MRO unchanged: `[Agent, AgentHookSurface, PiAgentHarness, object]`.
- AgentHookSurface hook count: 112 (verified at import time).
- `make all` green on both venvs; 7 tests pass.

**Commit:** `5928508` — `refactor(pi): split agent_class.py into events / hook_surface / agent_class`.

**Handoff refresh:** next task #7 (Phase 5 — consistency-check helper).

### 2026-05-17 — Task #7 — Phase 5 — Consistency-check helper

**Action:** added module-level `_assert_declarations_match_event_sets()` helper at the bottom of `src/libharness/pi/hook_surface.py`. Runs on `AgentHookSurface` at import time. Validates:

- Every name in `_EVENT_NAMES | _DECISION_EVENT_NAMES` has `on_<event>` + `async_on_<event>` declared on `AgentHookSurface.__dict__`.
- Every name in `_DECISION_EVENT_NAMES` additionally has `decide_<event>` + `async_decide_<event>`.
- No orphan ClassVars (declarations for non-existent events).

On any mismatch, raises `AssertionError` listing the offenders. Helper is ~40 LOC, slightly over the plan's 15-25 LOC estimate because the four-direction check (missing-obs / extra-obs / missing-decision / extra-decision) wanted explicit messaging for each.

**Surprises:** my initial break-on-purpose probe was misleading — testing on a subclass of `AgentHookSurface` reports all 112 inherited ClassVars as "missing" since the check inspects `cls.__dict__` only. The correct probe mutates `AgentHookSurface` directly. Re-verified: mutating `AgentHookSurface._EVENT_NAMES` to add a fake event triggers `AssertionError` naming `fake_drift_event` in the missing-observation list. Confirmed the assertion mechanism works as designed for the actual target.

**Tests/validation:** `make all` green on both venvs.

**Commit:** `4c1a615` — `feat(pi): assert hook-surface declarations match event sets at import time`.

**Handoff refresh:** next task #8 (Gate A — `make all` on both venvs as a checkpoint, then user check-in).

### 2026-05-17 — Task #8 — Gate A — `make all` on both venvs

**Build sanity gate after Phases 1-5.**

| Venv                  | Lint | Typecheck | Tests    |
| --------------------- | ---- | --------- | -------- |
| local.venv (3.11)     | ✓    | ✓         | 7 passed |
| local-ft.venv (3.14t) | ✓    | ✓         | 7 passed |

**Detail:** `make all` runs ruff lint → mypy → pyright → pytest. 7 tests = v5-era suite (`test_jsonl.py` 2, `test_rpc_fake.py` 1, `test_server.py` 1, `test_set_model.py` 1, `test_tools.py` 2). The broader v8 test port lands in Phase 6.

**Sanity import check:** `from libharness.pi import Agent, AgentEvent, AgentHookSurface, HarnessRuntime, HookContext, PiAgentHarness, UnhandledEventError` clean on both venvs.

**State at Gate A:**

- 6 commits landed since `main`: `71eadae` (setup tooling) → `9a57b99` (Phase 1 vendor) → `9945438` (Phase 2 mixin) → `3aada26` (Phase 3 gap closure) → `5928508` (Phase 4 split) → `4c1a615` (Phase 5 consistency check).
- 14 of 31 tasks completed (work tasks 1-8 except Phase 5.5/Gate A.5/etc.; journal tasks 17-23).
- Source-side port work is complete (Phases 1-5).
- Phase 5.5 (cherry-picks) is the next work; Gate A.5 (8-reviewer review) follows.

**Decision:** this is a user check-in point per the operating mode. Awaiting user confirmation before proceeding to Phase 5.5. Journal + handoff get committed now (together) as the Gate A docs snapshot, since they've grown substantially since the start and a clean recovery point is useful before the larger Phase 5.5 / Gate A.5 work.

**Handoff refresh:** Gate A complete; awaiting user check-in; next work = Task #9 (Phase 5.5 cherry-picks).
