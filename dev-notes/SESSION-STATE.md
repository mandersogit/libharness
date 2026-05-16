---
status: Active
created: '2026-05-14'
---

# Session state

This is the handoff document for any fresh session in libharness. CLAUDE.md directs new sessions to read this file first.

## Fresh-session quickstart

If you're a new session starting in `~/git/github/libharness`:

1. **Read this file first** (you're doing it). Skim the rest of it once for context.
1. **Then `docs/DESIGN.md`.** Architecture and the Resolved decisions table at the bottom — the concurrency-model decision (2026-05-15) is the most load-bearing entry for current work.
1. **Ready for implementation (next milestone):**
   - `dev-notes/2026-05-15-threads-rewrite-plan.md` — asyncio → threads rewrite of `src/libharness/pi/`. Four phases, ~1100 LOC, 10 enumerated regression tests. **Blocked on Agent-class co-design** (the `on_*` hook set); the plan prepares the dispatch mechanism but doesn't pick hook names.
1. **The remaining open co-design proposal:**
   - `dev-notes/2026-05-14-event-bridge-proposal.md` — three approaches sketched (notify-only, full-roundtrip, hybrid). Now that concurrency is resolved (threads + FT-first), the protocol shape can be designed against sync hooks and `ctx.update`-style emission.
1. **Resolved (historical reference):**
   - `dev-notes/2026-05-15-concurrency-model-discussion.md` — concurrency model. Decision: primarily D, backwards compatible with C. Worth reading for the rationale and the Decision points that informed the call.
1. **For pi-internals context**, the reference doc is `dev-notes/2026-05-14-pi-internals-notes.md` (bridge transport rationale, multi-pi `HOME`-sharing safety, session-tree structure). Read on demand, not up-front.

The library predecessor reviews in `dev-notes/2026-05-14-pi-python-harness-*-review.md` are background; only relevant if questions arise about why v5 was chosen as the port baseline.

## Current state

- **Phase 2 of the threads rewrite landed.** `src/libharness/pi/server.py` is sync threaded (`socketserver.ThreadingTCPServer` + per-execute `asyncio.run` to keep async tool bodies working during the phase 2-3 transition). Async shims preserved on `PythonToolServer.start/close/__aenter__/__aexit__` so the still-asyncio harness + live tests stay green. 87/87 tests pass on both venvs; six consecutive runs, zero flakes. Three adversarial reviewers (2 codex gpt-5.5 xhigh + 1 Opus) found ~11 must-fix bugs (CancelledError escape, partial-start hang, out-of-band race, etc.); all fixed before commit. See `dev-notes/2026-05-16-threads-rewrite-plan-phase2-review-synthesis.md` for the synthesis. **Phase 3 (rpc.py rewrite) next**, then phase 4 design proposal.
- v5 baseline ported into `src/libharness/pi/`. `set_model` bug fixed with regression test; manifest `protocolVersion=1` handshake added; `docs/DESIGN.md` written; resolved decisions captured (TCP-not-UDS, use pi-native sessions, and now concurrency-model — see below).
- **Concurrency model decided (2026-05-15):** primarily **D** (threads on freethreaded CPython 3.14t — optimize for FT parallelism opportunities), backwards-compatible with **C** (threads on standard CPython 3.11+ with the GIL). Sync `def` for tool functions and `on_*` hooks; `async def` rejected at decoration. Source rewrite (asyncio → threads in RPC client/server, tool registry, server) is the next implementation milestone. Rationale: `dev-notes/2026-05-15-concurrency-model-discussion.md`; table-row summary in `docs/DESIGN.md` § Resolved decisions.
- **Dual-venv scaffold for C+D in place** (interpreter-level only — no Python source changed yet). `local.venv/` is 3.11 standard; `local-ft.venv/` is 3.14t freethreading (`sys._is_gil_enabled() == False`). Both built from `/opt/miniforge/envs/base-py3-{11,14-nogil}/`. All dev deps available as cp314t wheels (mypy 2.1, pyright 1.1.409, ruff 0.15, pytest 9, hypothesis 6, coverage 7). `LIBHARNESS_VENV=<path>` env override on `scripts/test.sh` is how non-default venvs get targeted; no `activate` ever used.
- 7 unit tests + 1 set_model regression pass on **both** venvs (mypy/pyright run with `--python-version 3.14` for the ft variant). 2 live tests (faux-provider integration + real LLM via ChatGPT OAuth → gpt-5.5) currently wired symmetrically — running `make test-live` exercises both venvs against a real LLM. Cut to one venv if cost/rate-limit becomes an issue.
- `make all` clean on both venvs: ruff, mypy strict, pyright basic, pytest (excluding live by default). `make all-311` / `make all-ft` for single-venv runs.
- `make test-live` runs the live tests against sandboxed pi on both venvs.
- Markdown toolchain wired in: `make {lint-md, lint-md-tables, format-md, format-md-check}`.
  Mdformat (Python venv) + markdownlint-cli2 (Node, in
  `/opt/miniforge/envs/dev-tools/`). `.markdownlint.json` at root.
  Markdown targets are NOT in `make all`. Per-file workflow in
  CLAUDE.md.
- OAuth credential currently sits at `.sandbox/pi-home/.pi/agent/auth.json` (untracked). Was bootstrapped by copying from an earlier sibling sandbox; to set up from scratch on a new machine, run `make login`.

## Pending tasks

In priority order. The first task is now an implementation milestone (the concurrency decision has landed); the second remains a blocking co-design gate.

- **Rewrite asyncio → threads (primarily D, compatible with C).** Decision recorded above. **Plan landed at `dev-notes/2026-05-15-threads-rewrite-plan.md`** (status "Ready for implementation"): 4-phase feature branch (`tools.py` → `server.py` → `rpc.py` → `harness.py`+cleanup), `ThreadingTCPServer` for the bridge, `subprocess.Popen` + reader threads for pi I/O, `dict[id, queue.Queue(maxsize=1)]` for RPC correlation, sidecar disconnect-watcher thread per execute, sync-only tool handlers (`ctx.update` is the one streaming mechanism; sync-gen dropped per v4 precedent), `async def` rejected at decoration. ~1100 LOC touched (~600 genuinely new). 10 concrete regression tests enumerated. **Blocked on Agent-class co-design** — the rewrite prepares the dispatch *mechanism* (single `_dispatch_event` site on the reader thread; write-back via `_send_lock`) but does not commit to specific `on_*` hook names; agree with the author on the Agent shape before starting.
- **CO-DESIGN: Event bridge.** Doc at `dev-notes/2026-05-14-event-bridge-proposal.md`. Three approaches sketched; eight decision points enumerated. Now that concurrency is resolved, design against sync `on_*` hooks and synchronous `ctx.update(...)` emission rather than asyncio idioms.
- **Expose pi-native session API as typed methods on `PiRpcClient`.** Decision recorded in `docs/DESIGN.md` § Resolved decisions: we use pi-native sessions, not a Python-side model. Methods to add as thin wrappers around `client.send({"type": "..."})`: `fork(entry_id)`, `clone()`, `switch_session(session_path)`, `get_session_stats()`, `export_html(output_path=...)`, `set_session_name(name)`, `get_fork_messages()`. Plus a `list_sessions(cwd, session_dir=...)` helper. Cost is ~100-150 lines + tests. **Land as part of (or after) the threaded rewrite** — these wrappers should be sync from the start, not asyncio code that gets rewritten immediately.
- Command bridge, state bridge, UI bridge (after event bridge lands; see `docs/DESIGN.md` Roadmap).

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

- Concurrency model is **resolved** (threads + FT-first). The remaining gate is the event bridge — design it against sync hooks and synchronous emission, not async idioms.
- **Never activate a venv.** Always invoke interpreter/tool binaries by absolute path (e.g. `./local.venv/bin/python`, `./local-ft.venv/bin/pytest`). Make targets do this for you. To run something against the ft venv from a script, set `LIBHARNESS_VENV=$(pwd)/local-ft.venv` and call into `scripts/test.sh` (or read `$VENV` from `scripts/lib/env.sh`).
- Per `CLAUDE.md`: all commits go through the commit-plans skill. No direct `git commit` / `git add` / `git push`. Read-only git is fine.
- Per the project's markdown workflow (`feedback_markdown_workflow` memory): after editing **any** `.md`, run `./local.venv/bin/python -m mdformat --wrap keep <FILE>` then `/opt/miniforge/envs/dev-tools/bin/node /opt/miniforge/envs/dev-tools/bin/markdownlint-cli2 <FILE>` and `make lint-md-tables`. Table rows ≤150 cols. Sidecar pattern for overflow (never drop tables).
- The model selection in `test_real_llm.py` is implicit (pi reads the default from `.sandbox/pi-home/.pi/agent/settings.json`). For determinism across contributor environments, pin via `PiLaunchConfig(provider=..., model=...)`. Not urgent.
- One v3 idea we did *not* port: handlers that are sync generators yielding multiple `update` frames are supported in `tools.py:collect_tool_result`, but there's no test that exercises a generator end-to-end through the bridge. Worth a regression test before we rely on it. Now that concurrency is moving to threads, this whole code path may simplify away (the family precedent doesn't have tool-side update streaming at all); revisit during the rewrite.
- The v1–v5 proof-of-concept iterations are vendored at `dev-notes/predecessors/v*/` (source + design docs). The original `~/Downloads/pi_python_harness/` working folder is no longer required and can be deleted; everything we cite lives in-tree (`dev-notes/predecessors/`) or via `links/pi/` (the pi source checkout at `~/git/external/pi/`).
- For a fresh session resumed from this state: ground every recommendation in the relevant dev-notes doc and `docs/DESIGN.md`, not in your training-time priors about asyncio vs threads. The concurrency model is settled (threads + FT-first); don't relitigate.
