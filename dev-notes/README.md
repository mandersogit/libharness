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
- **`2026-05-14-pi-python-harness-version-review.md`** — historical
  review of the v1–v4 proof-of-concept implementations that preceded
  libharness. References cite the sibling workspace at
  `~/Downloads/pi_python_harness/` by absolute path.
- **`2026-05-14-pi-python-harness-v5-synthesis-review.md`** —
  historical review of v5 (the synthesis ChatGPT 5.5 Pro produced over
  v1–v4). v5's bugs / gaps drove the first wave of libharness work; the
  fixes have since landed.
- **`2026-05-14-event-bridge-proposal.md`** — in co-design. First
  customization surface beyond tool execution. Implementation gated on
  author sign-off of the proposal's "Decision points" section.
