---
status: "Active"
created: "2026-05-14"
---

# Session state

## Current state

- Scaffold committed (`chore: initial scaffold`).
- v5 baseline ported into `src/libharness/pi/` (subpackage chosen so
  future non-pi harnesses get their own siblings; generalization is
  deferred).
- All 7 tests pass: 6 unit tests + 1 live integration test against real
  pi (`@earendil-works/pi-coding-agent@0.74.0`).
- `make all` clean: ruff, mypy strict, pyright basic, pytest (excluding
  live by default).
- `make test-live` runs the integration test against the sandboxed pi.

## Port-pass changes vs v5 verbatim

The port is semantically verbatim. The deltas that were needed to keep
mypy strict + ruff happy:

- `pi/server.py:55` — `asyncio.AbstractServer | None` →
  `asyncio.Server | None` (concrete type has `.sockets`).
- `pi/server.py:87` — added `object` type annotations on `__aexit__`
  params to satisfy `disallow_untyped_defs`.
- `pi/tools.py:222` — gated `dataclasses.asdict()` behind
  `not isinstance(value, type)` to narrow `is_dataclass`; kept a
  one-line `# type: ignore[arg-type]` because mypy's TypeGuard widens
  to instance-or-type.
- `pi/tools.py:351` — removed unused `# type: ignore[comparison-overlap]`.
- `pi/rpc.py:131-134` — `try/except ValueError: pass` →
  `contextlib.suppress(ValueError)` (SIM105). Added `import contextlib`.
- Ruff autofix on the rest (12 fixes — quoted self-types, sorted
  imports, unused `AsyncIterator` import).

No semantic changes. No bug fixes were applied during the port; the
`set_model` bug from the v5 review is intentionally still there
(next commit).

## Pending tasks

In priority order. P1 first.

- **P1: Fix `set_model` bug** (`pi/rpc.py`, sends `model` instead of
  `modelId`). Pi expects `modelId` per
  `pi-main/packages/coding-agent/src/modes/rpc/rpc-types.ts:31`. Add
  regression test.
- **P2: Add manifest `protocolVersion`** (from v3's design). TS shim
  enforces handshake; Python emits version 1.
- **P2: Add real-LLM smoke test** (`tests/pi/test_real_llm.py`) gated
  by `live` marker. Uses ChatGPT OAuth →
  `openai-codex-responses` provider → real model → Python tool
  roundtrip. (OAuth must be done in this sandbox first: `make login`.)
- **P3: Write `docs/DESIGN.md` properly.** Base on v3's design doc plus
  v5's current implementation. Include roadmap section for the event /
  command / state / UI bridges.
- **P3 (CO-DESIGN REQUIRED): Event bridge.** First customization surface
  beyond tools. Decide protocol shape (in-extension `pi.on(...)`
  forwarding to Python over the same JSONL bridge?).

## Recent activity

- Scaffold committed (`864a60b`).
- v5 source ported into `libharness.pi` subpackage; tests ported into
  `tests/pi/`. Mechanical changes only (module rename in tests +
  minimal lint/type fixes — see above).
- pi sandbox bootstrapped in this repo (`.sandbox/`). No OAuth token
  yet in this sandbox; live LLM tests will require `make login` first.

## Notes for the next session

- Start with the `set_model` fix. One commit: the fix +
  `tests/pi/test_set_model_regression.py`. Should be a 10-line change.
- After that, the protocolVersion work touches both the Python manifest
  emitter (`pi/tools.py:ToolRegistry.manifest`) and the TS shim
  (`pi/shim.py:PRODUCTION_TS_SHIM`). One commit.
- For the real-LLM test, you'll need to `make login` first to get an
  OAuth token in `.sandbox/pi-home/.pi/agent/auth.json`. The test
  itself should `skipif` on the auth file's absence (separate from the
  `live` marker — `live` means "needs sandbox pi"; the LLM check is
  finer-grained).
