---
status: Active
created: '2026-05-14'
---

# Session state

This is the handoff document for any fresh session in libharness. CLAUDE.md directs new sessions to read this file first.

## Fresh-session quickstart

If you're a new session starting in `~/git/github/libharness`:

1. **Read this file first** (you're doing it). Skim the rest of it once for context.

1. **Note the branch.** This repo has two parallel feature branches:

   - **`asyncio-in-thread`** (this branch) — design exploration for adopting the v6/v7/v8 architecture: keep the asyncio core, run it in a dedicated thread, layer an Agent class with hooks + opt-in participation on top. **No code changes to `src/libharness/pi/` yet** — the implementation is queued for the spark environment (which has codex-cli adversarial review skill better suited to the implementation work).
   - **`threads-rewrite`** (on spark, not this checkout) — the alternative direction from the 2026-05-15 concurrency-model decision: rip asyncio out, threads end-to-end, FT-first. Implementation in progress on spark.

   The 2026-05-15 concurrency decision currently still reads as "Resolved" in `docs/DESIGN.md`. When the spark-side port of v8 lands and we adopt the v8 architecture, that row gets superseded.

1. **Then `docs/DESIGN.md`.** Architecture and the Resolved decisions table at the bottom.

1. **For the current direction (v8 / asyncio-in-thread), read in order:**

   - `dev-notes/2026-05-17-v6-as-base-direction.md` — the direction-shift discussion. Frames why we're considering v6/v7/v8 vs the threads-rewrite plan.
   - `dev-notes/2026-05-17-v8-analysis.md` — the deliverable analysis with the port plan (§ Recommendation for the libharness port), the resolutions, the 3-file split layout.
   - `dev-notes/predecessors/v8-decision-hooks/` — the actual code we're porting. Read `src/pi_python_harness/agent_class_mca.py` (the author-refactored mixin variant) rather than `agent_class.py` (the original).

1. **For the v8 design rationale (read on demand):**

   - `dev-notes/2026-05-17-v7-analysis.md` — v7 analysis (the Agent class + hooks were added here).
   - `dev-notes/2026-05-17-v7-event-verification.md` — pi event taxonomy and the three-surface framing (notification / participation / RPC envelope).
   - `dev-notes/2026-05-17-v8-request.md` — the prompt sent to ChatGPT.
   - `dev-notes/2026-05-17-v8-request-notes.md` — the design discussion that became the v8 request.

1. **Historical/resolved reference:**

   - `dev-notes/2026-05-15-concurrency-model-discussion.md` — concurrency model. Decision was primarily D (threads end-to-end). Being superseded by the v8 direction.
   - `dev-notes/2026-05-15-threads-rewrite-plan.md` — the threads-rewrite implementation plan. Still "Ready for implementation" on the spark branch.
   - `dev-notes/2026-05-14-event-bridge-proposal.md` — open co-design for the event-bridge protocol. Effectively answered by v8's gated-decide architecture.

1. **For pi-internals context**, the reference doc is `dev-notes/2026-05-14-pi-internals-notes.md`. Read on demand.

The library predecessor reviews in `dev-notes/2026-05-14-pi-python-harness-*-review.md` are background.

## Current state

- **v8 ported into `src/libharness/pi/` (2026-05-17).** Source is `events.py` + `hook_surface.py` + `agent_class.py` (3-file split) + `agent.py` (PiAgentHarness proxy) + `runtime.py` (HarnessRuntime, AsyncioLoopThread) + the existing `rpc.py` + `server.py` + `shim.py` + `tools.py` + `jsonl.py` + `harness.py` + `cli.py`. The `Agent` class is a subclassable surface with 112 hook ClassVars across four flavors (on / async_on / decide / async_decide); the `AgentHookSurface` mixin holds the declarative surface and validation; an import-time consistency-check guard pins the invariant that the ClassVars match the event frozensets.
- **Architecture:** asyncio core preserved but running in a dedicated thread (`HarnessRuntime`'s `AsyncioLoopThread`). Each `PiAgentHarness` has its own owner thread. Shared tool executor + dedicated single-worker hook executor. The application main thread stays the application's.
- **Tests:** 56 unit tests + 2 live tests pass on both venvs. `make all` and `make test-live` clean. Test coverage: hooks dispatch (test_agent_class.py — including 3 strict-mode E2E tests for decision events from Item C), decision hooks (test_decision_hooks.py), channel separation (test_channel_separation.py), pi event taxonomy (test_pi_event_taxonomy.py — skips when pi-mono unavailable), threading model (test_threaded_agent.py), v5 regression (test_set_model.py, test_server.py, etc.), Phase 5.5 cherry-picks (test_v8_cherrypick_fixes.py — 6 cases), Gate A.5 Tier-1 fixes (test_gate_a5_tier1_fixes.py — 12 cases).
- **Docs:** `docs/DESIGN.md` updated with the asyncio-in-thread architecture, threading-model paragraph, Agent-class paragraph, and 5 new 2026-05-17 resolved-decision rows (concurrency, hook surface layout, decision-hook return shape, cancellation API, timeout config). `docs/AGENT_HOOKS.md` ported from v8 with per-event return-shape table. Three superseded design docs (threads-rewrite plan, concurrency-model discussion, event-bridge proposal) carry top-of-doc notes pointing readers at the v8 docs.
- **Dual-venv scaffold for 3.11 + 3.14t in place.** `local.venv/` (3.11) and `local-ft.venv/` (3.14t). Both build clean; both run the full suite. The FT verification motivation softened (asyncio core stays on one loop thread, so FT mostly only buys parallelism for the tool pool), but the dual-venv setup carries no real cost.
- **OAuth credential** at `.sandbox/pi-home/.pi/agent/auth.json` (untracked; refresh-token backed). `make login` bootstraps from scratch.

## Pending tasks (post-port)

Sorted by readiness.

- **Tier-2 ergonomic pass** (the deferred-from-Gate-A.5 findings). 22 items in `dev-notes/2026-05-17-v8-port-review-synthesis.md` Tier-2 section; most are MODERATE/MINOR with mitigations or no current load-bearing impact. Highlights: shared `hook_executor` contention (F8), `_call`/close race (F9), `_decision_timeouts_ms` mutable class default (F11), `_handler_wants_context` keyword-only edge case (F10 / Item D). No single-commit fix; revisit when an actual incident motivates the work.
- **Pi-native session method wrappers** (carried forward from earlier sessions): `fork`, `clone`, `switch_session`, `get_session_stats`, `export_html`, `set_session_name`, `get_fork_messages` as typed methods on `PiRpcClient`. ~100-150 LOC. Independent of any v8 work.
- **Item E — `subscribe_client_events` thread-bounce simplification.** Deferred during Phase 6 (advisor doc rated it "low value too. Defer if it's contentious."). Two thread hops to a list append. Land in the ergonomic pass if you want to clean up the path.
- **Generator-through-bridge regression test.** Sync-generator handlers yielding multiple `update` frames are supported in `tools.py:collect_tool_result` but not test-covered end-to-end. v8 preserves the path; the gap survives the port.
- **Command bridge, state bridge, UI bridge** (Roadmap items in `docs/DESIGN.md`; future work).

## Recent activity

Commits added during the v8 port sprint (2026-05-17), newest first:

- `a0aa043` — docs: mark threads-rewrite + concurrency-model docs superseded; close event-bridge proposal (Phase 8).
- `8dcf6cc` — docs: update DESIGN.md for asyncio-in-thread + Agent class; port AGENT_HOOKS.md (Phase 7).
- `90e93a9` — test(pi): port v8 test suite + strict-mode E2E for decision events (Phase 6 + Item C).
- `4b82fe7` — docs: Gate A.5 review synthesis + journal/handoff snapshot.
- `4d0bee1` — fix(pi): apply Gate A.5 Tier-1 findings (7 HIGH; F1-F7 + F20).
- `de0fe82` — fix(pi): cherry-pick three v8 bugs surfaced in threads-rewrite review (Phase 5.5).
- `e62b085` — docs: v8 port sprint scaffolding + Gate A snapshot.
- `4c1a615` — feat(pi): assert hook-surface declarations match event sets at import time (Phase 5).
- `5928508` — refactor(pi): split agent_class.py into events / hook_surface / agent_class (Phase 4).
- `3aada26` — feat(pi): declare observation hooks for decision events (Phase 3).
- `9945438` — refactor(pi): extract AgentHookSurface mixin from Agent class (Phase 2).
- `9a57b99` — feat(pi): vendor v8 source as libharness.pi (Phase 1).
- `71eadae` — fix(setup): pin nodeenv as explicit dev dep + point NODEENV_PYTHON at $VENV.

Plus a Phase 9 commit (this SESSION-STATE update) and a Gate C snapshot to follow.

`make all` + `make test-live` validated on both venvs through Gate B (commit `90e93a9`).

## Notes for the next session

- **Port is complete on this branch.** `src/libharness/pi/` reflects the v8 baseline with the three Phase 5.5 cherry-picks and seven Gate A.5 Tier-1 fixes applied. The architecture is in `docs/DESIGN.md`; the hook surface in `docs/AGENT_HOOKS.md`. The full sprint audit trail is in `dev-notes/2026-05-17-v8-port-journal.md` (running log) and `dev-notes/2026-05-17-v8-port-handoff.md` (volatile state snapshot).
- **The threads-rewrite branch (`origin/threads-rewrite`)** is preserved for audit but superseded. Three commits there are not adopted. The supersede notes at the top of `dev-notes/2026-05-15-threads-rewrite-plan.md` and `dev-notes/2026-05-15-concurrency-model-discussion.md` explain the direction shift.
- **Never activate a venv.** Always invoke interpreter/tool binaries by absolute path. Make targets do this for you.
- Per `CLAUDE.md`: all commits go through the commit-plans skill. Read-only git is fine.
- Per the project's markdown workflow: after editing any `.md`, run `./local.venv/bin/python -m mdformat --wrap keep <FILE>` then `/opt/miniforge/envs/dev-tools/bin/node /opt/miniforge/envs/dev-tools/bin/markdownlint-cli2 <FILE>` and `make lint-md-tables`. Table rows ≤150 cols. Sidecar pattern for overflow (never drop tables).
- The model selection in `test_real_llm.py` is implicit (pi reads the default from `.sandbox/pi-home/.pi/agent/settings.json`). For determinism across contributor environments, pin via `PiLaunchConfig(provider=..., model=...)`. Not urgent.
- The v1–v5 proof-of-concept iterations are vendored at `dev-notes/predecessors/v*/` (source + design docs). Original `~/Downloads/pi_python_harness/` working folder no longer required and can be deleted; everything we cite lives in-tree.
- For a fresh session resumed from this state: ground every recommendation in `docs/DESIGN.md`, `docs/AGENT_HOOKS.md`, and the v8 port journal. The architecture is settled (asyncio-in-thread + Agent class + opt-in participation); don't relitigate.
