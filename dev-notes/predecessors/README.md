---
status: Historical
created: '2026-05-15'
---

# Predecessor reference material

Vendored snapshots of the **libharness v1–v5 proof-of-concept iterations** — the AI experiments whose review (and the v5 synthesis) became this project's port baseline. Originally lived at `~/Downloads/pi_python_harness/` (a zip download given to ChatGPT to drive the experiments). Brought into the repo on 2026-05-15 so every citation in the review docs is verifiable in-tree and `~/Downloads/` can be retired.

## Layout

Each `v*/` directory mirrors the layout of the corresponding ChatGPT-authored package — the per-version source, tests, examples, `pyproject.toml`, README, and design docs.

```text
dev-notes/predecessors/
├── README.md                    (this file)
├── prompt.txt                   (the prompt that drove the v1–v4 runs)
├── v1/                          version-1/pi_python_harness/
├── v2/                          version-2/pi_python_harness_artifacts/
├── v3/                          version-3/pi-python-harness/
├── v4/                          version-4/pi_python_harness/
├── v5-synthesis/                version-5-synthesis/pi-python-harness-synthesis/
├── v6-threaded/                 thread-owned reshaping of v5 (asyncio in dedicated thread)
├── v7-threaded-agent-hooks/     v6 + Agent class with notification hooks (10 events)
└── v8-decision-hooks/           v7 + decision hooks for all 19 participation events
```

v1–v5 were the original Pi-harness exploration (2026-05-14 era). v6–v8 are 2026-05-17 follow-on revisions from the same ChatGPT 5.5 Pro Extended Thinking conversation, vendored as we received them. Each `vN+1` builds on `vN` rather than being an independent synthesis. The `vN-threaded-agent-hooks`-style names describe the substantive addition; the underlying conversation thread is preserved at `<dir>/prompt-sequence.md` where present.

The v6 → v7 → v8 chain is the "asyncio-in-thread direction" we're adopting (or planning to adopt) for `src/libharness/pi/`. The v6-as-base discussion lives at `dev-notes/2026-05-17-v6-as-base-direction.md`; per-version analyses at `dev-notes/2026-05-17-v{6,7,8}-*.md`.

Total size on disk: ~5 MB (v1–v8 combined).

## What's intentionally NOT here

- **`pi-main/`** — pi's source. Reachable via `links/pi/` (symlink to the local pi checkout at `~/git/external/pi/`).
- **The version-level `scripts/`** — experimental bootstrap scripts superseded by this project's `scripts/` and `Makefile`.
- **The original sandbox** (`~/Downloads/pi_python_harness/.sandbox/`) — ~700 MB of Node + npm install state. Not source. The OAuth token in there was copied into `.sandbox/pi-home/` of this repo at bootstrap time; everything else is rebuilt by `make install-pi`.

## Editorial policy

These trees are **frozen historical artifacts**. Do not reformat, re-flow, or lint-clean them — they are evidence of what each AI iteration produced.

Toolchain exclusions already in place to enforce this:

- **Markdown** — `make {lint-md, format-md, format-md-check}` exclude `dev-notes/predecessors/v*/*` (only this README is linted/formatted).
- **Python tests** — `pyproject.toml` `tool.pytest.ini_options.norecursedirs` lists `dev-notes` so a stray `pytest dev-notes/...` invocation can't collect the predecessor test files.
- **Type checking & lint** — `mypy` is scoped to `packages = ["libharness"]`; `ruff` is scoped to `src = ["src", "tests"]`. Neither walks `dev-notes/`.

If you need to annotate a predecessor (e.g., "this claim turned out to be wrong"), prefer adding a sibling note in `dev-notes/` that cites `dev-notes/predecessors/v*/...:LINE`, rather than editing the predecessor in place.

## Review docs that consume this material

- `dev-notes/2026-05-14-pi-python-harness-version-review.md` — review of v1–v4 (written *before* v5 existed).
- `dev-notes/2026-05-14-pi-python-harness-v5-synthesis-review.md` — review of v5; recommends it as the port baseline.

Both reviews cite paths under `dev-notes/predecessors/v*/...` (and `links/pi/...` for pi-source citations).
