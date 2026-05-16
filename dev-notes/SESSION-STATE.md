---
status: Active
created: '2026-05-14'
---

# Session state

This is the handoff document for any fresh session in libharness. CLAUDE.md directs new sessions to read this file first.

## Fresh-session quickstart

If you're a new session starting in `~/git/github/libharness`:

1. **Read this file first** (you're doing it). Skim the rest of it once for context.
1. **Then `docs/DESIGN.md`.** Architecture and the Resolved decisions table at the bottom.
1. **Then the two open co-design proposals**, in this order:
   - `dev-notes/2026-05-15-concurrency-model-discussion.md` — recommends C+D (threads on standard 3.11; freethreaded 3.14t as supported runtime). Decide this *first* — the answer affects the rewrite scope for everything else (RPC client, server, tool registry).
   - `dev-notes/2026-05-14-event-bridge-proposal.md` — three approaches sketched (notify-only, full-roundtrip, hybrid). Decide this *second*; some details depend on the concurrency model.
1. **For pi-internals context**, the reference doc is `dev-notes/2026-05-14-pi-internals-notes.md` (bridge transport rationale, multi-pi `HOME`-sharing safety, session-tree structure). Read on demand, not up-front.

The library predecessor reviews in `dev-notes/2026-05-14-pi-python-harness-*-review.md` are background; only relevant if questions arise about why v5 was chosen as the port baseline.

## Current state

- v5 baseline ported into `src/libharness/pi/`. `set_model` bug fixed with regression test; manifest `protocolVersion=1` handshake added; `docs/DESIGN.md` written; resolved decisions captured (TCP-not-UDS, use pi-native sessions).
- 10 tests pass: 7 unit + 2 live (faux-provider integration + real LLM via ChatGPT OAuth → gpt-5.5) + 1 set_model regression.
- `make all` clean: ruff, mypy strict, pyright basic, pytest (excluding live by default).
- `make test-live` runs the live tests against sandboxed pi.
- Markdown toolchain wired in: `make {lint-md, lint-md-tables, format-md, format-md-check}`.
  Mdformat (Python venv) + markdownlint-cli2 (Node, in
  `/opt/miniforge/envs/dev-tools/`). `.markdownlint.json` at root.
  Markdown targets are NOT in `make all`. Per-file workflow in
  CLAUDE.md.
- OAuth credential copied from sibling sandbox at `~/Downloads/pi_python_harness/.sandbox/pi-home/.pi/agent/auth.json`. Untracked (lives under `.sandbox/`). To set up from scratch: `make login`.

## Pending tasks

In priority order. First two are blocking gates — do not act unilaterally on either.

- **CO-DESIGN: Concurrency model.** Doc at `dev-notes/2026-05-15-concurrency-model-discussion.md`. Open question: asyncio (current) vs threads (matches hildy / simple-harness / not-pi-2) vs threads-on-freethreaded-3.14t. Author's design intent (Agent class with sync `on_*` hooks) plus three-iteration family precedent point to threads. Recommendation **C+D** (threads on 3.11+, with 3.14t as a supported runtime) at high confidence; awaiting sign-off. Halt before any rewrite.
- **CO-DESIGN: Event bridge.** Doc at `dev-notes/2026-05-14-event-bridge-proposal.md`. Three approaches sketched; eight decision points enumerated. The concurrency-model decision should land first (it changes the protocol shape's idiom on the Python side).
- **Expose pi-native session API as typed methods on `PiRpcClient`.** Decision recorded in `docs/DESIGN.md` § Resolved decisions: we use pi-native sessions, not a Python-side model. Methods to add as thin wrappers around `client.send({"type": "..."})`: `fork(entry_id)`, `clone()`, `switch_session(session_path)`, `get_session_stats()`, `export_html(output_path=...)`, `set_session_name(name)`, `get_fork_messages()`. Plus a `list_sessions(cwd, session_dir=...)` helper. Cost is ~100-150 lines + tests. **Defer until after the concurrency-model decision** — if we move to threads, this code needs to land in the threaded form, not as asyncio wrappers that get rewritten next week.
- Command bridge, state bridge, UI bridge (after event bridge lands; see `docs/DESIGN.md` Roadmap).

## Recent activity

Commits, newest first:

- `7f9c54e` — docs: pi internals notes + record session-model intent.
- `b4d2ccd` — feat(pi): manifest `protocolVersion` + shim handshake (P2).
- `0058390` — fix(pi): `set_model` wire shape `model` → `modelId` (P1).
- `ff498a2` — test(pi): real-LLM live test (P2).
- `eae9f67` — docs: session state + commit plan for v5 port.
- `7d0d125` — feat(pi): port v5 source as `libharness.pi` subpackage.
- `864a60b` — chore: initial scaffold.

Plus content/format cleanup commits between these by the author.

Concurrency-model discussion doc landing in this commit alongside the SESSION-STATE refresh.

## Notes for the next session

- The two open co-design gates are independent in principle but the concurrency decision should land first; many details of the event bridge (`async def` handler vs sync, `ctx.update`-style emission, etc.) collapse once the model is chosen.
- Per `CLAUDE.md`: all commits go through the commit-plans skill. No direct `git commit` / `git add` / `git push`. Read-only git is fine.
- Per the project's markdown workflow (`feedback_markdown_workflow` memory): after editing **any** `.md`, run `./local.venv/bin/python -m mdformat --wrap keep <FILE>` then `/opt/miniforge/envs/dev-tools/bin/node /opt/miniforge/envs/dev-tools/bin/markdownlint-cli2 <FILE>` and `make lint-md-tables`. Table rows ≤150 cols. Sidecar pattern for overflow (never drop tables).
- The model selection in `test_real_llm.py` is implicit (pi reads the default from `.sandbox/pi-home/.pi/agent/settings.json`). For determinism across contributor environments, pin via `PiLaunchConfig(provider=..., model=...)`. Not urgent.
- One v3 idea we did *not* port: handlers that are sync generators yielding multiple `update` frames are supported in `tools.py:collect_tool_result`, but there's no test that exercises a generator end-to-end through the bridge. Worth a regression test before we rely on it. If concurrency moves to threads, this whole code path may simplify away (the family precedent doesn't have tool-side update streaming at all).
- There is a separate sandbox at `~/Downloads/pi_python_harness/.sandbox/` from the v1–v5 review work. Independent from this repo's sandbox; mentioned only because the OAuth token was copied from it.
- For a fresh session resumed from this state: ground every recommendation in the relevant dev-notes doc and `docs/DESIGN.md`, not in your training-time priors about asyncio vs threads.
