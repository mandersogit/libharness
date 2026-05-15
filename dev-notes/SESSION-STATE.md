---
status: Active
created: '2026-05-14'
---

# Session state

## Current state

- v5 baseline ported into `src/libharness/pi/`, `set_model` bug fixed
  with regression test, manifest `protocolVersion=1` handshake added,
  `docs/DESIGN.md` written, event-bridge proposal in co-design.
- All 10 tests pass: 7 unit + 2 live (faux-provider integration + real
  LLM via ChatGPT OAuth → gpt-5.5) + 1 set_model regression.
- `make all` clean: ruff, mypy strict, pyright basic, pytest (excluding
  live by default).
- `make test-live` runs the live tests against sandboxed pi.
- Markdown toolchain wired in: `make {lint-md, lint-md-tables, format-md, format-md-check}`. Inherited from hildy/not-pi-2:
  mdformat (Python venv) + markdownlint-cli2 (Node, in
  `/opt/miniforge/envs/dev-tools/`). `.markdownlint.json` at root.
  Markdown targets are NOT in `make all`. Per-file workflow in
  CLAUDE.md.
- OAuth credential copied from sibling sandbox at
  `~/Downloads/pi_python_harness/.sandbox/pi-home/.pi/agent/auth.json`.
  Untracked (lives under `.sandbox/`). To set up from scratch:
  `make login`.

## Pending tasks

- **CO-DESIGN: Event bridge.** Proposal lives at `dev-notes/2026-05-14-event-bridge-proposal.md`. First customization surface beyond tool execution. Three approaches sketched; decisions deferred to discussion. Halt here until the author signs off on the shape.
- **Expose pi-native session API as typed methods on `PiRpcClient`.** Decision recorded in `docs/DESIGN.md` § Resolved decisions: we use pi-native sessions, not a Python-side model. Methods to add as thin wrappers around `client.send({"type": "..."})`: `fork(entry_id)`, `clone()`, `switch_session(session_path)`, `get_session_stats()`, `export_html(output_path=...)`, `set_session_name(name)`, `get_fork_messages()`. Plus a `list_sessions(cwd, session_dir=...)` helper that reads the session directory. Cost is ~100-150 lines + tests.
- Command bridge, state bridge, UI bridge (after event bridge lands; see `docs/DESIGN.md` Roadmap).

## Recent activity

- Scaffold committed (`864a60b`).
- v5 source ported into `libharness.pi` subpackage (`7d0d125`).
- Real-LLM live test added (`ff498a2`).
- `set_model` wire-shape bug fixed; regression test added (`0058390`).
- Manifest `protocolVersion=1` + shim handshake (`b4d2ccd`).
- `docs/DESIGN.md` written. Tool execution surface fully documented;
  roadmap calls out event/command/state/UI bridges.
- Event-bridge proposal at
  `dev-notes/2026-05-14-event-bridge-proposal.md`; in co-design.
- Markdown toolchain wired in (mdformat + markdownlint-cli2 + sidecar
  table rule). All existing markdown reformatted; wide tables in the
  version-review, event-bridge proposal, and DESIGN.md restructured
  to use the sidecar pattern.
- Pi internals investigated: bridge transport choice (TCP-not-UDS), multi-pi `HOME`-sharing safety (pi uses `proper-lockfile` on shared mutable files), and session-tree structure (cross-file, parentId-based, typically degenerate linear chains within a file). Findings recorded in `dev-notes/2026-05-14-pi-internals-notes.md`. Two resolved decisions added to `docs/DESIGN.md` § Resolved decisions.

## Notes for the next session

- The event-bridge proposal is the next gate. Do not implement until
  the author signs off on §"Decision points" of the proposal doc.
- The model selection in `test_real_llm.py` is implicit (pi reads the
  default from `.sandbox/pi-home/.pi/agent/settings.json`). If we want
  determinism across contributor environments, pin via
  `PiLaunchConfig(provider=..., model=...)`.
- One v3 idea we did *not* port: handlers that are sync generators
  yielding multiple `update` frames are supported in `tools.py: collect_tool_result`, but there's no test that exercises a generator
  end-to-end through the bridge. Worth a regression test before we
  rely on it.
