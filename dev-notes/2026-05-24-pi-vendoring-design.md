---
status: Implemented (with Q1/Q3/Q4/Q5 deferred)
created: '2026-05-24'
---

# pi vendoring design — install-time fetch with future opt-in pre-baked wheel

Companion to `dev-notes/2026-05-23-deno-as-pi-runtime-spike.md`, which holds the evidence that the precompiled pi binary works as a drop-in for libharness under v8. This note records the design decisions about *how libharness ships pi to PyPI users*.

**Status (2026-05-24):** Phases 1+2+3 implemented and validated end-to-end. The vendoring + resolver paths are live in source; the setup.py wheel-build hook is wired and verified to produce a wheel with the pi binary baked in. Q2 (dev-loop) is closed (resolved as: rename old target to `install-pi-node-deprecated`, new `install-pi` invokes `python -m libharness._pi_vendor install`). Q1/Q3/Q4/Q5 remain deferred — their leans below are the implementation defaults but not yet stress-tested.

## Settled (2026-05-24)

- **Default distribution shape.** Pure-Python wheel published to PyPI; `setup.py` downloads the pinned precompiled pi binary from earendil-works's GitHub release at end-user install time, sha256-verifies against a hardcoded per-platform hash table, extracts to `site-packages/libharness/_vendor/pi/`. Cmake/ninja shape, but the binary download happens on the user's box rather than at libharness CI time.
- **Future opt-in.** A pre-baked wheel variant (binary inside the wheel via cibuildwheel CI matrix) is planned for enterprise / air-gapped / firewall-blocked / EDR-sensitive users who can't tolerate install-time network fetch. Probably `libharness[bundled-pi]` extra or sibling `libharness-bundled` package. Not on critical path; deferred until a concrete user need surfaces.
- **Opt-out today.** `PiLaunchConfig(pi_command=[...])` accepts user-supplied paths. Already supported by the existing API; no new design needed for users who want to bring their own pi.
- **Runtime resolver.** Stop at vendored. Order: (1) explicit `pi_command=[...]` passed to `PiLaunchConfig`, (2) `LIBHARNESS_PI_PATH` env var, (3) vendored at `site-packages/libharness/_vendor/pi/pi`, (4) fail loud with a diagnostic message pointing at the two opt-out paths. **No `$PATH` fallback** — silent install failures masking as "working pi from somewhere" would produce wrong-version behavior that's hard to debug; legit "use my own pi" cases go through the explicit (1) or (2).
- **Etiquette.** Before either approach lands on PyPI, send a heads-up note to earendil-works covering: attribution wording, bug-routing language, release-cadence intent. Bundle their LICENSE in the wheel. README must clearly attribute the bundled binary and route pi-internal bugs upstream. See \[[user-npm-avoidance]\] and \[[project-pi-distribution]\] in agent memory for the supporting context that drove these decisions.

## Implementation outcomes (2026-05-24)

- **Q2 — RESOLVED.** Renamed legacy `install-pi` → `install-pi-node-deprecated`; new `install-pi` invokes `python -m libharness._pi_vendor install` which fetches → sha256-verifies → extracts pi 0.75.5 to `src/libharness/_vendor/pi/`. Both targets coexist; the deprecated one will be removed once the binary path is thoroughly vetted in real use. `make bootstrap` calls the new (binary) target. `scripts/lib/env.sh` prefers vendored pi over node-pi for `PI_CLI` so `make test`/`make test-live` automatically use the new binary when present.

## Open questions (implemented defaults; not yet stress-tested)

- **Q1 — Sdist install when GitHub is unreachable.** Implementation: `setup.py`'s `BuildPyWithPi` raises with a clear actionable message that points at `LIBHARNESS_SKIP_PI_VENDOR=1` as the escape hatch. The opt-out path skips the download and proceeds with a binary-less install; the runtime resolver will then fail loud at first use unless `LIBHARNESS_PI_PATH` or explicit `pi_command` is set. **Untested:** behavior under real network partition, rate-limit, or sha256-mismatch from a corrupted mirror. To revisit if such a case is encountered in real use.

- **Q3 — Pi-version bump cadence + quarantine policy.** Implementation: `PI_VERSION` constant in `src/libharness/_pi_vendor.py` plus a per-platform sha256 table. Bumping = changing both. **No automation yet** — every bump is manual. The quarantine policy (3-7 days after a new pi release before adoption) is documented here but not enforced anywhere. Revisit when the first post-implementation pi bump happens.

- **Q4 — Diagnostic CLI shape.** Implementation: `python -m libharness._pi_vendor` with subcommands `install [--force]`, `--version`, `--which`. No `[project.scripts]` entry-point. Discoverable via `make install-pi` and the help text in the Makefile. **Untested:** Windows path handling for `pi.exe` resolution from `--which`.

- **Q5 — Wheel data layout + cross-platform edge cases.** Implementation: `[tool.setuptools.package-data] libharness = ["py.typed", "_vendor/pi/**/*"]` glob picks up the entire vendored tree. Wheel build verified to produce a 45.7 MB `.whl` containing the 113 MB `_vendor/pi/pi` executable plus assets (197 vendored entries). **Untested:** (a) read-only `site-packages` environments — current behavior is "fails during pip install with stdlib `urllib`/extract error" rather than a guided "your site-packages is read-only" message; (b) Windows exec-bit handling — the chmod `+x` step is a no-op on Windows, but pi.exe should be runnable from a zip-extracted state without needing chmod; (c) `pip install -U libharness` upgrade semantics — relying on pip's standard wheel-replacement logic; not load-tested.

## Not in scope

- **Deno-pi runtime.** Stays documented as Plan B in the spike note. Invoked only if (a) earendil-works stops shipping precompiled binaries OR (b) the bun trajectory becomes actually action-worthy. Currently pi 0.75.5 ships against bun 1.2.x (confirmed via binary `strings`), which predates the 1.3 Rust-rewrite that yt-dlp issue 16766 flags. Watch-item, not action-item.
- **cibuildwheel CI setup.** In scope for the future opt-in pre-baked wheel, but deferred until that path is picked up. When picked up, the `setup.py` download logic from the default path can be reused — cibuildwheel just runs it in CI rather than on user boxes.
- **Multi-version pi handling.** Enterprise environments running mixed pi versions are already handled by `PiLaunchConfig.pi_command` user overrides. No design work needed.

## Evidence trail

- `dev-notes/2026-05-23-deno-as-pi-runtime-spike.md` — spike findings + v8 re-verification (four smokes + regression test passing on both venvs).
- `experiments/precompiled-pi-spike/` (gitignored) — sha256-verified pi 0.75.5 linux-arm64 binary + four smoke scripts (faux/live × `PiAgentHarness`/`Agent`-subclass).
- `tests/pi/test_precompiled_pi_binary.py` — regression, `live`-marked, locks in the load-bearing "precompiled binary works as a drop-in" claim; updated 2026-05-24 to auto-pick the vendored binary when `PI_PRECOMPILED` is unset.
- `src/libharness/_pi_vendor.py` (~250 LOC) — canonical fetch + resolver module; CLI for `install`/`--version`/`--which`.
- `setup.py` (~50 LOC) — minimal shim wiring `BuildPyWithPi` into setuptools.
- `tests/pi/test_pi_vendor.py` — 26 unit cases covering platform detection (6), hash-table sanity (2), vendored-path helpers (4), resolver precedence (5), PiLaunchConfig defer-to-resolver (4), fetch sha256-mismatch + idempotency (2), no-PATH-fallback guarantee (1), Windows path handling (1).
- Wheel-build verification (2026-05-24): `python -m build --wheel` produces `libharness-0.0.1-py3-none-any.whl` (45.7 MB) containing 197 vendored entries including the 113 MB `pi` executable. Confirms `BuildPyWithPi` fires during wheel build and `package_data` glob picks up the tree.
