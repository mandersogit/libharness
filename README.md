# libharness

A Python-first harness around the [Pi agent harness](https://pi.dev).

Tools, environment customization, and orchestration are written in Python.
Pi runs as a subprocess in its RPC mode. A small generic TypeScript shim
registers Python-authored tools with pi and forwards execution back across
a local bridge. No per-tool TypeScript.

**Status:** pre-alpha. Scaffold only; implementation is being synthesized
from the prior experiments documented in `dev-notes/`.

## Setup

```bash
make bootstrap          # local.venv (3.11) + .sandbox/{nodeenv,pi-install}
make login              # one-time OAuth into a pi provider (TUI)
make test               # run pytest (excludes live tests)
```

## Commands

```bash
make help               # list all targets
make install            # Python venv at local.venv/
make install-pi         # Node + pi into .sandbox/
make test               # pytest, excluding live
make test-live          # pytest with live marker (requires pi + auth)
make lint               # ruff check
make typecheck          # mypy + pyright
make format             # ruff format
make all                # lint + typecheck + test
make clean              # Python caches
make clean-sandbox      # .sandbox/
make clean-all          # everything (venv + sandbox + caches)
make pi ARGS="..."      # run the sandboxed pi CLI directly
```

Live tests auto-skip if pi or OAuth credentials are missing. `make test`
excludes them via the `live` pytest marker.

## Layout

```
libharness/
  Makefile               primary user-facing surface
  src/libharness/        Python implementation (TBD)
  tests/                 pytest suite
  scripts/               sandbox setup + dev helpers (Make calls into these)
  docs/DESIGN.md         architecture
  dev-notes/             reviews, design docs, session state, commit plans
  local.venv/            Python venv (gitignored)
  .sandbox/              Node + pi install (gitignored)
```

## Background

The design and implementation choices are informed by reviewing five
parallel proof-of-concept implementations (v1–v5) of this same idea. The
reviews live in `dev-notes/`. Headlines:

- All five converged on "Python parent + `pi --mode rpc` + minimal TS
  shim for tool registration."
- v5 was the strongest implementation; v3 had the strongest design doc.
- Known issues v5 carries that this implementation should fix: wrong
  `set_model` argument shape, no real-LLM integration test, no event /
  command / state / UI bridges beyond tool execution, no manifest
  protocol versioning.

See `CLAUDE.md` for project conventions and `docs/DESIGN.md` for the
in-progress architecture.
