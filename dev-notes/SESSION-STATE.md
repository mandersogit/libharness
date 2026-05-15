---
status: "Active"
created: "2026-05-14"
---

# Session state

## Current state

- Scaffold exists. Python package importable as `libharness`. Empty
  `__init__.py` only.
- Pi sandbox: `.sandbox/` not bootstrapped in this repo yet. (A working
  sandbox exists in `~/Downloads/pi_python_harness/.sandbox/` from the
  v1–v5 review work, with a valid ChatGPT OAuth token. That is a
  separate sandbox; do not depend on it from here.)
- Toolchain: Makefile, pyproject (setuptools + ruff + mypy + pyright +
  pytest + hypothesis), `.gitignore` set for both `local.venv/` and
  `.sandbox/`.
- Conventions captured in `CLAUDE.md`.

## Pending tasks

In priority order. P1 first.

- **P1: Port v5 source into `src/libharness/`.** Source lives at
  `~/Downloads/pi_python_harness/version-5-synthesis/pi-python-harness-synthesis/src/pi_python_harness/`.
  Rename module paths from `pi_python_harness.*` to `libharness.*`.
- **P1: Fix `set_model` bug** (`rpc.py:301-302`, sends `model` instead
  of `modelId`). Add regression test.
- **P1: Port v5 tests** (`test_jsonl.py`, `test_tools.py`,
  `test_server.py`, `test_rpc_fake.py`, `test_real_pi_integration.py`).
  Mark `test_real_pi_integration.py` with the `live` pytest marker.
- **P2: Add manifest `protocolVersion`** (from v3's design). TS shim
  enforces handshake; Python emits version 1.
- **P2: Add real-LLM smoke test** (`tests/test_real_llm.py`) gated by
  `live` marker. Uses ChatGPT OAuth → `openai-codex-responses` provider
  → real model → Python tool roundtrip.
- **P3: Write `docs/DESIGN.md` properly.** Base on v3's design doc plus
  v5's current implementation. Include roadmap section for the event /
  command / state / UI bridges.
- **P3 (CO-DESIGN REQUIRED): Event bridge.** First customization surface
  beyond tools. Decide protocol shape (in-extension `pi.on(...)`
  forwarding to Python over the same JSONL bridge?).

## Recent activity

- Project initialized at `~/git/github/libharness/`. `main` branch, no
  commits yet.
- Toolchain scaffolded matching the `not-pi-2` pattern (Makefile-driven,
  `local.venv/` from miniforge 3.11, ruff/mypy/pyright/hypothesis).
- Pi sandbox scripts ported from `~/Downloads/pi_python_harness/scripts/`
  with the multi-version slug indirection stripped (single
  implementation now).
- Prior reviews copied into `dev-notes/`:
  - `2026-05-14-pi-python-harness-version-review.md` (v1–v4)
  - `2026-05-14-pi-python-harness-v5-synthesis-review.md` (v5)

## Notes for the next session

- First action after `make install`: run `make lint` and `make
  typecheck` against the empty scaffold to confirm tooling works
  end-to-end before adding code.
- Then start P1 port. Do it as one commit ("port v5 baseline") rather
  than file-by-file — it's a verbatim copy with module rename, no
  decisions to defer.
- The set_model fix should be the SECOND commit, with a regression test.
  Keep it isolated so the fix is reviewable.
