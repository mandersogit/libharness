# libharness

A Python-first harness around the Pi agent harness. Python owns tool
authoring, orchestration, and lifecycle; Pi runs as a subprocess in its
RPC mode; a small generic TypeScript shim registers Python tools with pi.

## Start here (session onboarding)

Read in order:

1. **`dev-notes/SESSION-STATE.md`** — short volatile handoff: current
   state, pending tasks, priority for next action. Supersedes anything
   static elsewhere.
1. **`docs/DESIGN.md`** — architecture; locked decisions when present.
1. **`dev-notes/2026-05-14-pi-python-harness-version-review.md`** and
   **`dev-notes/2026-05-14-pi-python-harness-v5-synthesis-review.md`** —
   reviews of the five proof-of-concept predecessors. The synthesis
   plan in libharness is informed by these.

If SESSION-STATE flags a task as CO-DESIGN REQUIRED, do not design
unilaterally — enter an explicit co-design conversation with the author
first.

## Setup and dev loop

```bash
make bootstrap          # local.venv + .sandbox (one-time)
make login              # one-time OAuth into pi provider
make all                # lint + typecheck + test
```

`make help` lists all targets. The Makefile is the primary surface; the
shell scripts in `scripts/` are the implementation it calls into.

**Never call python outside the venv.** Use `make` targets or
`./local.venv/bin/python`.

## Conventions

- **Modern type hints**: `list[str]`, `X | None`. No `Optional`, no
  `List`/`Dict`/`Tuple` from `typing`.
- **Frozen dataclasses** for all data types unless mutation is required.
- **Line length**: 100. Ruff format enforces.
- **Python**: target 3.11, compatible with 3.11+.
- **Async-first**: the harness owns subprocesses and sockets; prefer
  `asyncio` over threads.
- **Type-checked**: mypy strict + pyright basic must pass before any
  commit.

## Architecture sketch

```
Python app
  ├─ ToolRegistry (decorators, JSON Schema from type hints)
  ├─ PythonToolServer (local JSONL bridge, token-protected)
  └─ PiRpcClient (subprocess: pi --mode rpc, JSONL stdin/stdout)
                      │
                      ▼
                  Pi process
                      └─ generic TS extension (loaded via --extension)
                           ├─ pi.registerTool(...) per manifest entry
                           └─ on execute(): call back into Python bridge
```

The TypeScript shim is generated/vendored, generic, and protocol-
versioned. No per-tool TypeScript.

## What's in scope vs not

**In scope:**

- Python-authored tools and a generic TS shim that registers them.
- Lifecycle: Python launches/owns pi.
- Subscription-aware test paths (faux provider for unit tests; real LLM
  for live tests).
- Eventually: event bridge, command bridge, state bridge — the
  customization surfaces beyond tools that the predecessors only
  designed but didn't ship.

**Not in scope (deliberately):**

- Reimplementing pi in Python.
- Replacing pi's provider/model registry, session manager, or TUI.
- A separate Pi SDK sidecar — RPC is the boundary.

## Git policy

All commits go through the **commit-plans** skill. Do not use `git add`,
`git commit`, or `git push` directly. Read-only git commands
(`git status`, `git log`, `git diff`) are always fine. Commit plans live
in `dev-notes/commit-plans/`.

## Markdown authoring

All markdown files in `dev-notes/` must have YAML frontmatter:

```yaml
---
status: "Draft"  # or "In co-design", "Ready for implementation", "Implemented"
created: "2026-05-14"
---
```

Tables under 150 columns. Paragraph text not line-wrapped.
