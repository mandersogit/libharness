---
status: Active
created: '2026-05-14'
---

# dev-notes

## Convention

All files have YAML frontmatter with `status` and `created`. Dated
files use `YYYY-MM-DD-slug.md`.

## Index

- **`SESSION-STATE.md`** — volatile handoff (read first).
- **`commit-plans/`** — staged commit plans for the `commit-plans` skill.
- **`predecessors/`** — vendored snapshots of the v1–v5 proof-of-concept
  iterations (source + design docs + prompt.txt). Frozen historical
  artifacts; see `predecessors/README.md` for layout and editorial policy.
- **`2026-05-14-pi-python-harness-version-review.md`** — historical
  review of the v1–v4 proof-of-concept implementations that preceded
  libharness. Cites `dev-notes/predecessors/v{1,2,3,4}/...` for
  predecessor source and `links/pi/...` for pi-source grounding.
- **`2026-05-14-pi-python-harness-v5-synthesis-review.md`** —
  historical review of v5 (the synthesis ChatGPT 5.5 Pro produced over
  v1–v4). v5's bugs / gaps drove the first wave of libharness work; the
  fixes have since landed.
- **`2026-05-14-pi-internals-notes.md`** — reference notes on pi
  internals (bridge transport, session structure, multi-pi safety).
- **`2026-05-14-event-bridge-proposal.md`** — in co-design. First
  customization surface beyond tool execution. Implementation gated on
  author sign-off of the proposal's "Decision points" section.
- **`2026-05-15-concurrency-model-discussion.md`** — resolved (threads,
  freethreading-first). Retained for rationale.
