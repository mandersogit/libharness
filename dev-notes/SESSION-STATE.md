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

- v5 baseline ported into `src/libharness/pi/` (unchanged on this branch). `set_model` bug fixed with regression test; manifest `protocolVersion=1` handshake added; `docs/DESIGN.md` written; resolved decisions captured (TCP-not-UDS, use pi-native sessions, concurrency-model — see below).
- **Direction shift in progress (2026-05-17):** the 2026-05-15 concurrency-model decision (primarily D = threads end-to-end) is being superseded in favor of the v6/v7/v8 asyncio-in-thread + Agent class with opt-in participation direction. Decision register in `docs/DESIGN.md` not yet updated (it gets updated when the port lands).
  - **v6** explored extracting asyncio I/O into a dedicated thread with a sync caller facade (`PiAgentHarness` proxy). Vendored at `dev-notes/predecessors/v6-threaded/`.
  - **v7** added the Agent class with notification hooks (`on_X` / `async_on_X`) for 10 core events. Vendored at `dev-notes/predecessors/v7-threaded-agent-hooks/`.
  - **v8** added decision hooks (`decide_X` / `async_decide_X`) for 19 participation events via the always-notify + gated-decide architecture. Vendored at `dev-notes/predecessors/v8-decision-hooks/`. **This is the baseline we're adopting.**
- **Dual-venv scaffold for 3.11 + 3.14t still in place.** `local.venv/` is 3.11 standard; `local-ft.venv/` is 3.14t freethreading. The FT verification motivation softened under the v8 direction (the asyncio core stays on one loop thread, so FT only buys parallelism for the tool pool), but the dual-venv setup carries no real cost; keep it.
- 7 unit tests + 1 set_model regression pass on both venvs. 2 live tests via `make test-live`. `make all` clean. Markdown toolchain wired (mdformat + markdownlint-cli2).
- OAuth credential at `.sandbox/pi-home/.pi/agent/auth.json` (untracked). `make login` bootstraps from scratch.

## Direction shift summary (2026-05-17)

For full detail, see `dev-notes/2026-05-17-v8-analysis.md`. Brief:

- **Architecture:** keep v6's `HarnessRuntime` + dedicated asyncio loop thread + thread-owned `PiAgentHarness` proxy + shared tool executor. Layer `Agent` (v7) with notification hooks + (v8) decision hooks on top.
- **Notification hooks:** `on_X` / `async_on_X` for 37 events total (18 RPC notification + 19 decision-event observation slots). Sync handlers run on a dedicated hook executor (`max_workers=1`); async handlers run on the loop thread. Return-less; pi doesn't wait.
- **Decision hooks:** `decide_X` / `async_decide_X` for 19 participation events. Sync round-trip — pi blocks until Python returns. Opt-in: defining the method opens the gate at extension load.
- **Gate model:** the TS shim subscribes to every participation event up front. Per fire, the handler picks `notify_event` (fire-and-forget, gate closed) or `event` (sync round-trip, gate open). One bridge call per fire either way.
- **Resolved (author, 2026-05-17):** decision-hook naming = `decide_X` + `async_decide_X` (symmetric); opt-in trigger = method existence; result shape = raw dict; cancellation = `HookContext` with cooperative `cancelled` polling; timeout = no default + opt-in mechanism; channel separation = decision events visible only via Agent hooks, not via `client.on_event`; mixin layout = `AgentHookSurface` (public, no leading underscore); file layout for port = 3 files (`events.py`, `hook_surface.py`, `agent_class.py`).
- **Deferred:** runtime opt-in (`agent.enable_decision` / `disable_decision`); TypedDicts per decision event for return-shape safety; pi-native session method wrappers; other bridges.

## Pending tasks

Reshuffled for the new direction. Top item is the port itself, queued for the spark environment.

- **Port v8 into `src/libharness/pi/` (executes in spark, not this checkout).** See `dev-notes/2026-05-17-v8-analysis.md` § Recommendation for the libharness port for the full sequence. Headlines:
  - Vendor v8 source → rename `pi_python_harness` → `libharness.pi`.
  - Apply mixin refinement (extract `AgentHookSurface` per the author's `agent_class_mca.py`).
  - Close the 38-declaration gap (add `on_X` / `async_on_X` for the 19 decision events).
  - Split into 3 files: `events.py` + `hook_surface.py` + `agent_class.py`.
  - Add the consistency-check helper (validates declarations match the event sets).
  - Address items C (strict-mode end-to-end test) and E (`subscribe_client_events` thread-bounce simplification). Defer items A, B, D, F.
  - Update `docs/DESIGN.md` § Resolved decisions: supersede 2026-05-15 concurrency row; add 2026-05-17 row(s) for asyncio-in-thread + Agent class + opt-in participation.
  - Mark `dev-notes/2026-05-15-threads-rewrite-plan.md` status as Superseded (preserve body).
  - Update this SESSION-STATE.md to reflect the post-port state.
  - Run `make all` + `make test-live` on both venvs.
  - The spark environment has a ready-to-use codex-cli adversarial review skill that's the right tool for the implementation pass.
- **Pi-native session method wrappers** (deferred from earlier sessions): `fork`, `clone`, `switch_session`, `get_session_stats`, `export_html`, `set_session_name`, `get_fork_messages` as typed methods on `PiRpcClient`. ~100-150 LOC. Independent of the v8 port; can happen in either branch.
- **Command bridge, state bridge, UI bridge** (after participation lands; see `docs/DESIGN.md` Roadmap).

## Recent activity

Commits, newest first:

- `79aa613` — build: dual-venv scaffold + docs: concurrency-model decision.
- `12aaf7b` — docs: vendor v1–v5 predecessors + sweep ~/Downloads citations.
- `ad687a1` — docs: concurrency-model discussion + fresh-session handoff.
- `7f9c54e` — docs: pi internals notes + record session-model intent.
- `b4d2ccd` — feat(pi): manifest `protocolVersion` + shim handshake (P2).
- `0058390` — fix(pi): `set_model` wire shape `model` → `modelId` (P1).
- `ff498a2` — test(pi): real-LLM live test (P2).
- `eae9f67` — docs: session state + commit plan for v5 port.
- `7d0d125` — feat(pi): port v5 source as `libharness.pi` subpackage.
- `864a60b` — chore: initial scaffold.

Plus content/format cleanup commits between these by the author.

`make test-live` validated on both venvs after `79aa613`: real pi + real LLM (gpt-5.5 via ChatGPT OAuth) green on 3.11 and 3.14t.

## Notes for the next session

- **Branch awareness:** the 2026-05-15 concurrency decision (option D, threads end-to-end) was the original direction. The 2026-05-17 session shifted toward the v8 asyncio-in-thread direction. The two branches (this one = `asyncio-in-thread`; spark's = `threads-rewrite`) both exist; the final adoption call lives in whichever branch's port lands first. If you arrive on `asyncio-in-thread`, the design is locked toward v8 (see § Direction shift summary); the port itself is queued for the spark environment.
- **Don't assume the port has happened on this branch.** `src/libharness/pi/` is still the v5 baseline. The v6/v7/v8 code lives only in `dev-notes/predecessors/v*/`. Until the port lands, the running code IS still v5-port-asyncio.
- **Concurrency decision in `docs/DESIGN.md` is stale-but-still-current-truth.** The 2026-05-15 row reads "Resolved" because that's what the code currently reflects. The supersession (2026-05-17 row) lands during the port, not before.
- **Never activate a venv.** Always invoke interpreter/tool binaries by absolute path. Make targets do this for you.
- Per `CLAUDE.md`: all commits go through the commit-plans skill. Read-only git is fine.
- Per the project's markdown workflow (`feedback_markdown_workflow` memory): after editing any `.md`, run `./local.venv/bin/python -m mdformat --wrap keep <FILE>` then `/opt/miniforge/envs/dev-tools/bin/node /opt/miniforge/envs/dev-tools/bin/markdownlint-cli2 <FILE>` and `make lint-md-tables`. Table rows ≤150 cols. Sidecar pattern for overflow (never drop tables).
- The model selection in `test_real_llm.py` is implicit (pi reads the default from `.sandbox/pi-home/.pi/agent/settings.json`). For determinism across contributor environments, pin via `PiLaunchConfig(provider=..., model=...)`. Not urgent.
- One v3 idea we did *not* port: handlers that are sync generators yielding multiple `update` frames are supported in `tools.py:collect_tool_result`, but there's no test that exercises a generator end-to-end through the bridge. Worth a regression test before we rely on it. The v8 architecture preserves the existing `collect_tool_result` shape (asyncio internals stay), so this code path survives — the regression-test gap is still real.
- The v1–v5 proof-of-concept iterations are vendored at `dev-notes/predecessors/v*/` (source + design docs). The original `~/Downloads/pi_python_harness/` working folder is no longer required and can be deleted; everything we cite lives in-tree (`dev-notes/predecessors/`) or via `links/pi/` (the pi source checkout at `~/git/external/pi/`).
- For a fresh session resumed from this state: ground every recommendation in the relevant dev-notes doc and `docs/DESIGN.md`, not in your training-time priors about asyncio vs threads. The concurrency model is settled (threads + FT-first); don't relitigate.
