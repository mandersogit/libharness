---
status: Active
created: '2026-05-17'
---

# v8 port — self-handoff (volatile)

Working-memory snapshot for the executing Claude session. **Refreshed frequently** as context fills, so a post-compaction or post-handoff session can resume without re-deriving state.

**If you are reading this as a fresh session:** read this file first, then `dev-notes/2026-05-17-v8-port-journal.md` (the full sprint log), then `dev-notes/2026-05-17-v8-port-plan.md` (the plan-of-record), then resume.

## Current state

**Phase:** Phases 1-5 + 5.5 + Gate A.5 Tier-1 fixes complete. 9 code commits + 1 docs snapshot since `main`. Currently mid Task #26 (Gate A.5 journal); next work = Phase 6 (test port).

**Branch:** `asyncio-in-thread`.

**Working tree:** uncommitted: updated journal + handoff + `dev-notes/2026-05-17-v8-port-review-synthesis.md` (synthesizer output). About to commit as the Gate A.5 docs snapshot.

**Pi source state under `src/libharness/pi/`:** events.py + hook_surface.py + agent_class.py three-file layout. AgentHookSurface mixin with 112 ClassVars + 3 _validate_\* classmethods + `_assert_declarations_match_event_sets()` invariant guard at module bottom. Agent class with dispatchers + lifecycle + manifest-wiring classmethods. Existing v8 source (rpc.py, server.py, shim.py, etc.) carries the three Phase-5.5 bugs to fix next.

**Next concrete action:** commit the Gate A.5 docs snapshot (synthesis doc + updated journal + updated handoff). Then claim Task #11 (Phase 6 — port v8 tests + items C and E). Then Gate B (`make all` + `make test-live` on both venvs). Then Phases 7-9 (docs). Then Gate C (final verification).

## Permissions and operating mode

User granted on 2026-05-17:

- `commit-plans` skill authorized for ALL sprint commits; do not ask per-commit.
- Mid-port design surprises: spawn 4-LLM discussion (2× codex gpt-5.5 xhigh via `~/.local/bin/codex exec -c model_reasoning_effort="xhigh" --sandbox danger-full-access`, plus 2× Opus via Agent tool); proceed with noted resolution if confident; defer otherwise; log either way in journal.
- Gate A.5: 8 reviewers (2× codex generalist + 4× codex specialist + 2× Opus generalist) → Opus subagent synthesizes into `dev-notes/2026-05-17-v8-port-review-synthesis.md`. Apply Tier-1 fixes autonomously (the Opus synthesizer is the impartial arbiter; no user gate needed).
- **NO scheduled user check-ins** through the sprint. Run continuously from Phase 1 through Gate C. The only mid-stream stops are: (a) a design surprise the 4-LLM council can't resolve, in which case defer that part and continue with what's resolvable; (b) the natural end of work at Gate C, where the user reviews the full diff. (Earlier handoff iterations had me pausing at Gate A / Gate A.5 / Gate C; the user clarified 2026-05-17 that those mid-stream pauses were a self-imposed misreading.)

User explicit asks:

- Keep this journal complete and useful — half for them, half for future-me.
- Track via TaskCreate/TaskUpdate (31 tasks created: IDs 1-16 are work items; IDs 17-31 are interleaved "Journal: post-#N" tasks; see mapping below).
- Watch context utilization; refresh THIS handoff doc frequently.

## Task pointer

Active task: **#26 — Journal: post-#10 (Gate A.5)** (in progress; about to commit and complete).

Next: **#11 — Phase 6 — Port v8 tests + items C and E**.

Completed so far: work items #1-9 + Gate A (#8) + Gate A.5 source fixes (#10 in progress until journal commits). Journal items #17-25. 22 of 31 tasks done.

Sprint structure (post-restructure): existing tasks #1-16 are work items; tasks #17-31 are interleaved journal-update tasks ("Journal: post-#N"). Claim in mostly numerical order: #N → #(N+16) → #(N+1) → #(N+17) → ... — i.e., after each work task #N, do its journal-step task before the next work task.

Mapping (work task → journal task after it):

- #1 → #17, #2 → #18, #3 → #19, #4 → #20, #5 → #21, #6 → #22, #7 → #23, #8 → #24
- #9 → #25, #10 → #26, #11 → #27, #12 → #28, #13 → #29, #14 → #30, #15 → #31
- (#16 / Gate C: final journal entry rolled into the task itself; no separate journal-step.)

## Key paths cheat-sheet

- **Repo root:** `/home/manderso/git/github/libharness--asyncio-in-thread`
- **Plan of record:** `dev-notes/2026-05-17-v8-port-plan.md`
- **Design analysis (sister doc):** `dev-notes/2026-05-17-v8-analysis.md`
- **Advisor recommendations:** `dev-notes/2026-05-17-claude-recommendations-for-v8-port.md`
- **Implementation journal:** `dev-notes/2026-05-17-v8-port-journal.md`
- **Self-handoff (this file):** `dev-notes/2026-05-17-v8-port-handoff.md`
- **Current pi source (v5 baseline):** `src/libharness/pi/`
- **v8 source-of-record:** `dev-notes/predecessors/v8-decision-hooks/src/pi_python_harness/`
- **v8 mixin variant (layout-of-record):** `dev-notes/predecessors/v8-decision-hooks/src/pi_python_harness/agent_class_mca.py`
- **v8 tests (to port in Phase 6):** `dev-notes/predecessors/v8-decision-hooks/tests/`
- **Sibling threads-rewrite synthesis (Phase 2 + Phase 3):** `/home/manderso/git/github/libharness/dev-notes/2026-05-16-threads-rewrite-plan-phase2-review-synthesis.md` and `…-phase3-review-synthesis.md` — format templates for Gate A.5's own synthesis doc; advisor recommendations doc points at them too; Phase 3 synthesis (commit `6c5a809` era) contains the dict-merge-order finding that became our Phase 5.5 Check 1.

## Key commands cheat-sheet

- **Build sanity (3.11):** `make all`
- **Build sanity (3.14t):** `LIBHARNESS_VENV=$(pwd)/local-ft.venv make all`
- **Live test (3.11):** `make test-live-311`
- **Live test (3.14t):** `make test-live-ft`
- **Pi smoke (no auth):** `make smoke-pi`
- **Per-file md format:** `./local.venv/bin/python -m mdformat --wrap keep <FILE>`
- **Per-file md lint:** `/opt/miniforge/envs/dev-tools/bin/node /opt/miniforge/envs/dev-tools/bin/markdownlint-cli2 <FILE>`
- **Table 150-col check:** `awk -v max=150 '/^[[:space:]]*\|/ { if (length > max) { print FILENAME":"FNR; failed=1 } } END { exit failed }' <FILE>`
- **Codex xhigh adversarial review:** `~/.local/bin/codex exec -c model_reasoning_effort="xhigh" --sandbox danger-full-access -o <out.md> "<prompt>"`

## Carried decisions (must not relitigate)

- v8 is the baseline (author resolution).
- `AgentHookSurface` is public (no leading underscore).
- `PiPythonHarness` stays for v1.
- Three-file split: `events.py` + `hook_surface.py` + `agent_class.py`.
- `_decision_timeout_ms` default stays `None` — HITL case allows decision hooks to legitimately block for days; do not default to 30s.
- Items C and E during port; A, B, D, F deferred.

## Carried bug fixes (Phase 5.5)

All three pre-identified, verified against v8 source at the cited lines:

1. **CRITICAL** `rpc.py:411` — dict-merge key order. Fix: `{**response, "type": "extension_ui_response", "id": request_id}`. Regression test: UI handler returns `{"id": "EVIL", "type": "evil-type"}`; framework keys must win.
1. **HIGH** `server.py:235` — `params or {}` falsy coercion. Fix: `request.get("params", {})` + None check + isinstance. Regression test: parametrize `params=[]/0/""/False`; each must error.
1. **MODERATE** `rpc.py:178-183` — startup-probe leak. Fix: try/except around the probe; cleanup reader tasks and proc.wait() before re-raise. Regression test: mock pi exiting code 1; assert PiRpcProcessError + reader_tasks both done().

## When to refresh this file

- After every phase commit lands.
- After any 4-LLM design discussion concludes.
- After any sub-task addition / TaskCreate.
- When context utilization feels heavy (>60% felt usage by intuition — there's no exact gauge but turn count + tool output volume are good proxies).

Refresh = rewrite the "Current state" + "Task pointer" sections; everything else is reasonably stable.
