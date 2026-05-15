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

```text
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

## Explicit tool paths

For any tool not installed by `apt`/`dnf`, invoke by absolute path. Never
rely on `$PATH` for non-system tools. This includes anything in a venv
(`./local.venv/bin/*`), anything in a conda env
(`/opt/miniforge/envs/*/bin/*`), or anything installed via `pipx`/`npm`/
`cargo`/`go`. OK to rely on `$PATH` for `bash`, `git`, `make`, `find`,
`grep`, `sed`, `awk`, `curl`, etc.

For tools that internally shebang to a sub-interpreter (e.g.
`markdownlint-cli2` is a Node script with `#!/usr/bin/env node`), call the
interpreter explicitly too: `$(NODE) $(MARKDOWNLINT_CLI2) <args>`. The
Makefile pre-flights the paths and fails loudly with the missing path in
the error message.

Specific non-apt tools used here:

- `./local.venv/bin/python` — 3.11 dev venv (managed by `make install`)
- `/opt/miniforge/envs/dev-tools/bin/node` — Node interpreter (for
  markdownlint-cli2)
- `/opt/miniforge/envs/dev-tools/bin/markdownlint-cli2` — Node script
- `.sandbox/nodeenv/bin/node` — sandboxed Node for pi (separate from
  dev-tools)
- `.sandbox/pi-install/.../cli.js` — pi CLI

## Markdown authoring

All markdown files in `dev-notes/` must have YAML frontmatter:

```yaml
---
status: "Draft"  # or "In co-design", "Ready for implementation", "Implemented"
created: "2026-05-14"
---
```

**Per-file workflow.** After editing **any** markdown file, run these two
commands on that single file before considering the edit done:

```bash
./local.venv/bin/python -m mdformat --wrap keep <FILE>
/opt/miniforge/envs/dev-tools/bin/node /opt/miniforge/envs/dev-tools/bin/markdownlint-cli2 <FILE>
```

Tight feedback loop: mdformat reflows tables and lists once; markdownlint
catches the 150-column-on-tables rule while you remember the structure.
`make format-md` / `make lint-md` exist for batch use but are NOT in
`make all` (so the default dev loop stays fast).

**Paragraph wrapping.** Do **not** line-wrap prose. mdformat's
`--wrap keep` preserves single-long-line paragraphs; if you wrap, every
re-edit will fight you. Long lines in prose are fine.

**Tables stay under 150 source columns.** When a table exceeds 150
columns, do **not** drop the table, drop columns, or aggressively
abbreviate cells. Use the **sidecar pattern**: keep the table compact
(short cells) and put a `**Detail:**` list immediately below that
elaborates each row. The table stays scannable; the list carries the
depth. Example:

```markdown
| Feature       | Owner | Status      |
| ------------- | ----- | ----------- |
| Auth rewrite  | alice | in progress |
| Cache rewrite | bob   | blocked     |

**Detail:**

- *Auth rewrite:* originally scoped for Q1; slipped because the SSO
  vendor's webhook contract changed mid-flight. Targeted for unblock
  by 2026-06-01.
- *Cache rewrite:* blocked on the auth rewrite completing first.
```

When in doubt: shorten cell headers first (`Description` → `Notes`), then
unambiguously shorten cell values (`sequential` → `seq` is fine;
`lifecycle method` → `hook` is not — the abbreviation loses information),
then fall back to the sidecar.
