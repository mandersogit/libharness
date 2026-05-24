---
status: Draft
created: '2026-05-23'
---

# Pi runtime spike findings — Deno and the precompiled binary

A spike record. Started as "can we run pi under Deno instead of Node," ended with the discovery that earendil-works publishes precompiled pi binaries for the full per-platform matrix and that they work as a drop-in for libharness. Both paths are now validated; the precompiled binary is the simpler one. Deno-pi remains viable as a supply-chain-conscious alternative.

## Workspace port note (2026-05-23)

This note was originally produced in the sibling `~/git/github/libharness/` checkout on the superseded `threads-rewrite` branch. Ported into the canonical `~/git/github/libharness--asyncio-in-thread/` workspace per its handoff document. The substantive findings (pi distribution shape, runtime compatibility, supply-chain reasoning) are branch-independent and survive the port verbatim. Two adjustments keep the record honest in this workspace:

- **Harness class names.** The historical smokes referenced below used `PiPythonHarness` (the async surface, which v8 preserved and still exports). The v8 port added `PiAgentHarness` — a synchronous, owner-thread-bound facade — and the `Agent` subclass that layers hooks on top. Re-validation in this workspace used the v8 surface (`PiAgentHarness` and `Agent`); see "Re-verification under v8 (this workspace)" below.
- **Artefact paths.** The historical experiment files live in the sibling workspace under `experiments/deno-pi-spike/` and `experiments/precompiled-pi-spike/`. This workspace re-ran the load-bearing validation in its own `experiments/precompiled-pi-spike/` and added a `live`-marked pytest case at `tests/pi/test_precompiled_pi_binary.py`. The deno smokes were not re-run; the deno findings are kept as a documented alternative path, not a current implementation target.

## Why we asked

The libharness mission is to wrap pi as a Python library implementation detail and ship it via `pip install`. That requires a story for *how pi reaches the user's box*. Today the dev loop relies on `make install-pi`, which `npm install`s pi into `.sandbox/pi-install/`. For distribution we need to put a working pi inside the wheel (or downloadable artefact) without making end users run `npm install`.

Three real options exist:

1. Ship a vendored `node_modules` tree alongside a Node binary, untarred at wheel-build time.
1. Compile pi to a single binary per platform ourselves via `bun build --compile` or `deno compile`.
1. **Use the precompiled binaries earendil-works already publishes** on their GitHub releases. This is the option we ended up validating most strongly — see "Round 4" below.

The Bun-compile path looks attractive on paper — pi's `package.json` already has a `build:binary` script. But there are reasons to be cautious about depending on the current Bun trajectory: yt-dlp recently pinned Bun support to versions `≤1.3.14` (issue [16766](https://github.com/yt-dlp/yt-dlp/issues/16766)), citing both a real lockfile-supply-chain bug below 1.2.11 and discomfort with the Rust rewrite of Bun being "vibe-coded." That doesn't make Bun unusable; it argues for caution about *us* depending on bun-the-toolchain as a build dependency. The precompiled artefacts earendil-works publishes are already-built outputs, which is a slightly different consumption shape (we're not running bun ourselves; we're trusting their CI's output).

The bigger motivation is **supply chain**. npm has the worst track record of the major package ecosystems for attacks — event-stream, ua-parser-js, colors/faker sabotage, node-ipc protestware, recurring shai-hulud-style worms. The structural reason isn't just culture: `npm install` runs arbitrary `postinstall` scripts by default, and that's the actual attack vector for most famous incidents. PyPI wheels can but rarely do; Cargo doesn't have the equivalent. **Deno does not execute npm `postinstall` scripts** when resolving `npm:` dependencies. Same registry, narrower attack surface. Even if we never ship pi through Deno to end users, doing the *vendor step* under Deno is safer than doing it under `npm install`.

## Going in: what I expected to break

Before running anything I predicted three classes of failure:

- **TUI / raw-stdin / terminal handling.** I was wrong about this. Deno's `Deno.stdin.setRaw()` is native; `process.stdin.setRawMode` works through node-compat; `Deno.consoleSize()` matches `process.stdout.columns/rows`; SIGWINCH works. Emitting ANSI escapes is identical across runtimes. None of this is the problem.
- **`jiti` (runtime CJS/TS loader used by pi-agent-core for extension loading).** I called this the "most likely import-time landmine." In practice, jiti loaded pi's TypeScript shim cleanly under Deno; the bridge worked end-to-end (proven by the faux-provider test below). This worry didn't materialise.
- **`undici` reaching into Node internals.** This one was real and was the *only* gap that surfaced. See below.

A point that's worth preserving: **Deno's permission sandbox doesn't actually buy us anything for pi.** Pi's job is to spawn arbitrary shell commands; that requires `--allow-run`, which in practice means `-A`. The argument for Deno is the supply-chain / postinstall-script story, not the permission model. We get the same JS engine V8, a different runtime façade, and one fewer category of supply-chain attack vector. Nothing more.

## Setup

Deno was not installed on `spark`. Conda-forge has current Deno (2.7.14) for aarch64 but only Deno 1.26.1 for linux-64 — the linux-64 channel is effectively abandoned. That asymmetry is itself a finding for the distribution story (see "Implications" below). For local dev on this machine, installing into the existing miniforge env is fine.

```bash
/opt/miniforge/bin/mamba install -n dev-tools -c conda-forge deno
# Resulting binary: /opt/miniforge/envs/dev-tools/bin/deno (2.7.14)
```

Spike artefacts live under `experiments/deno-pi-spike/` **in the sibling workspace** (gitignored via the `experiments/` entry on both checkouts):

- `smoke.py` — drives a real-LLM round-trip through libharness's `PiPythonHarness` (async surface, still exported under v8) with `pi_command=[deno, "run", "-A", "npm:...@0.74.0"]`.
- `smoke_faux.py` — same but using the faux-provider extension (deterministic tool-call without an LLM).
- `deno-node-compat-shim.mjs` — the 7-line `--import` preload (only needed for pi ≥ 0.75; see "Forward compatibility" below).

## What actually happened

### Round 1: pi --help under Deno (with no pinning)

```bash
deno run -A npm:@earendil-works/pi-coding-agent --help
```

Crash:

```text
TypeError: webidl.util.markAsUncloneable is not a function
  at new CacheStorage (.../undici/8.3.0/lib/web/cache/cachestorage.js:20:17)
```

Deno resolved pi `0.75.5` (latest at spike time, despite our vendored copy being `0.74.0`). Pi 0.75.5 pulls `undici 8.3.0`, whose `webidl/index.js` does an unconditional `const { markAsUncloneable } = require('node:worker_threads')` at module load. Deno's `node:worker_threads` shim doesn't expose `markAsUncloneable` (it's a Node-internal helper for `structuredClone`). The destructure silently produces `undefined`, the value gets stored on `webidl.util`, and the call site in `CacheStorage`'s constructor crashes.

My first reaction was "this is fixable only in Deno upstream." That was wrong. Treating the destructured undefined as a no-op fallback is a one-line patch (`webidl.util.markAsUncloneable = markAsUncloneable ?? (() => {})`), and it's harmless — `markAsUncloneable` is a `structuredClone` hint, and nothing in pi's code path structured-clones a `CacheStorage`. Patching the cached file made `--help`, `--version`, and `--list-models` all work.

A more interesting discovery came later. The **previous** undici release line, **7.25.0**, already had a runtime feature check for exactly this scenario:

```js
webidl.util.markAsUncloneable = runtimeFeatures.has('markAsUncloneable')
  ? require('node:worker_threads').markAsUncloneable
  : () => {}
```

So the unconditional crash is a regression in undici 8.x — upstream had a working fallback and then removed it. Pi 0.74.0 happens to be pinned to `undici ^7.19.1`, which resolves to 7.25.0, which has the fallback. **Deno-pi 0.74.0 needs no patch at all.**

### Round 2: full libharness round-trip

The real question isn't "does pi load," it's "does pi-under-deno still speak the JSONL RPC protocol that libharness's `PiRpcClient` expects, against real credentials, and does the bridge extension that registers Python tools still work?"

We have OAuth credentials for `openrouter` and `openai-codex` cached in `.sandbox/pi-home/.pi/agent/auth.json`. The smoke uses libharness's `PiPythonHarness` (the async surface — v8 also added `PiAgentHarness`/`Agent`, and the re-verification section below covers those) with `pi_command` overridden to `[deno, "run", "-A", "npm:@earendil-works/pi-coding-agent@0.74.0"]`, asks the model to call an `echo` tool, and asserts the tool was invoked with the expected argument.

One environment gotcha: setting `HOME=.sandbox/pi-home` (so pi finds its credentials) also moves Deno's cache to `$HOME/.cache/deno`. The first attempt failed because the relocated cache was empty (and the previous test had run *offline*, so Deno couldn't refetch). Fix: set `DENO_DIR` to a stable path so the cache stays put regardless of `HOME`. With that fixed, pi launched cleanly under Deno, returned a session ID, and accepted prompts via the JSONL RPC stream.

The faux-provider extension test passed first try:

```text
[faux] pi started, sessionId='019e57d6-7496-7947-a5c7-bc947e861c54'
[faux] OK: bridge worked end-to-end, calls=[{'message': 'hello from faux'}]
```

That proves: TypeScript shim loaded under Deno, Python tool server registered the tool, faux provider extension loaded (this is the path that goes through pi's `jiti`-based extension loader, which was my originally-feared landmine), pi's tool-call routing reached the Python bridge, and the result flowed back.

The live-LLM smoke initially looked broken: pi launched, accepted the prompt, ran to completion, but Mistral didn't call the tool. I almost concluded "deno-pi has a subtle bug in pi's openrouter tool-spec serialisation" — until I ran a control with node-pi at the same model and same prompt, and node-pi succeeded. Then I checked versions: node-pi was 0.74.0 (vendored), deno-pi was 0.75.5 (resolved fresh by Deno). Pinning Deno to pi 0.74.0 (`npm:@earendil-works/pi-coding-agent@0.74.0`) and re-running the smoke produced a clean pass on the first try.

```text
[spike] pi started, sessionId='019e57d8-...'
[spike] prompting model to call echo tool...
[spike] OK: echo received [{'message': 'hello from deno'}]
```

So the apparent deno-specific regression was actually a pi-version regression that we accidentally introduced by letting Deno float to latest. **Whether pi 0.75.5 also fails the Mistral tool-call test under Node is an open question** — we didn't reproduce that combination, because the only Node-pi we have installed is 0.74.0. The honest framing is: pi 0.75 has *something* that interacts badly with Mistral on OpenRouter; we don't yet know whether it's runtime-dependent.

### Round 3: shim instead of vendored-file patch

Editing pi 0.75.5's cached `undici/8.3.0/lib/web/webidl/index.js` was a useful proof of viability but not a shippable mechanism — the cache key changes per Deno upgrade, per HOME, etc. The cleaner path is to apply the fix as a preload shim that runs before any npm package imports undici:

```js
// experiments/deno-pi-spike/deno-node-compat-shim.mjs
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const wt = require("node:worker_threads");
if (typeof wt.markAsUncloneable !== "function") {
  wt.markAsUncloneable = () => {};
}
```

Run as:

```bash
deno run --import="./deno-node-compat-shim.mjs" -A npm:@earendil-works/pi-coding-agent@0.75.5 --help
```

Verified working on the pi-0.75.5-crash case. The mechanism: Deno's `--import` flag pre-imports the specified ESM module before the entrypoint. The shim uses `createRequire` to access Node's CJS module cache from ESM, requires `node:worker_threads` (which loads and caches Deno's shim module object), and adds the missing function to that cached object. When undici later does `require('node:worker_threads')` from inside the npm package, it gets the same cached object — now with the addition.

Properties of this approach:

- **No vendored-package edits.** The shim is a regular project file checked into source control, applied at invocation time.
- **Idempotent.** The `if (typeof !== "function")` guard makes the shim a no-op when not needed — so it's safe to ship unconditionally across undici 7.x (which already has the fallback), undici 8.x (which needs it), and future undici versions or Deno versions that close the gap.
- **Extensible.** If other Node-compat gaps surface, they go in the same shim file as additional guarded patches.

### Round 4: precompiled pi binary

Late in the session it surfaced that earendil-works publishes precompiled pi binaries on every release. The v0.75.5 release (the one that was failing under deno-pi for Mistral tool-use) ships six assets, covering the full per-platform matrix:

| Platform            | Asset                    | Size  |
| ------------------- | ------------------------ | ----- |
| macOS Apple Silicon | `pi-darwin-arm64.tar.gz` | 28 MB |
| macOS Intel         | `pi-darwin-x64.tar.gz`   | 30 MB |
| Linux ARM64         | `pi-linux-arm64.tar.gz`  | 44 MB |
| Linux x64           | `pi-linux-x64.tar.gz`    | 44 MB |
| Windows ARM64       | `pi-windows-arm64.zip`   | 44 MB |
| Windows x64         | `pi-windows-x64.zip`     | 46 MB |

Each tarball expands to a `pi/` directory containing a single ~113 MB executable (`pi/pi`), a small (1.4 MB) `node_modules/` carrying only the platform-specific native clipboard binding (`@mariozechner/clipboard-linux-arm64-gnu` and friends), the WASM blob for photon image processing, and runtime resources (themes, docs, examples). Pi's own modules (`@earendil-works/*`, `typebox`, undici, etc.) are bundled inside the executable. The binary's `strings` output confirms it's a bun-compiled standalone — references to `bun.lock`, `bun-profile`, `BUN_1.2` in the binary.

The open question for libharness compatibility was: when the libharness shim is passed via `--extension <path>`, can the bundled-into-the-binary modules be resolved as imports from an arbitrary external file? Pi's docs (`docs/extensions.md`) say extensions can `import type { ExtensionAPI } from "@earendil-works/pi-coding-agent"` and similar, but the docs frame extensions as files placed under `~/.pi/agent/extensions/` for auto-discovery; the docs are less explicit about external `--extension` paths used by host applications like libharness.

**Test 1 — faux-provider round-trip against compiled pi 0.75.5:** passed first try. The shim loaded, the Python tool was registered, the faux provider extension emitted a deterministic tool call, the bridge routed it through to the Python `echo` registry, and the result flowed back. This confirms that pi's TS extension loader (now Bun's native TS handling, not jiti) exposes bundled module specifiers to external files passed via `--extension`.

**Test 2 — live LLM round-trip against compiled pi 0.75.5:** also passed. Mistral-large via OpenRouter called the Python `echo` tool through the compiled binary, end to end.

The Test 2 result is also a useful *diagnostic* finding. Earlier we left it open whether the deno-pi 0.75.5 Mistral tool-call failure was a deno-specific compat issue or a pi-0.75 regression. With pi 0.75.5 as a bun-compiled binary running the same Mistral large model, the tool call works. So the failure under deno-pi 0.75.5 *is* deno-specific — most likely in undici 8.x SSE streaming under Deno's node-compat layer. It's not a pi-internal regression. This narrows the deno path's risk surface: deno-pi works for pi 0.74 (which uses undici 7.x with the runtime feature check) but has at least one open issue with pi 0.75 (undici 8.x streaming + tool calls).

The complete test matrix at the end of the spike:

| Setup                     | Faux provider | Live LLM (Mistral large)        |
| ------------------------- | ------------- | ------------------------------- |
| node-pi 0.74.0 (vendored) | not retested  | ✓ (control)                     |
| deno-pi 0.74.0            | ✓             | ✓                               |
| deno-pi 0.75.5            | ✓             | ✗ — deno-specific streaming gap |
| compiled pi 0.75.5 (bun)  | ✓             | ✓                               |

## Findings

- **The precompiled pi binary is a drop-in replacement for node-pi as far as libharness is concerned.** External `--extension` paths resolve their imports of `@earendil-works/*`, `typebox`, etc. against the modules bundled inside the binary. Faux-provider and real-LLM tool-call round-trips both pass.
- **Deno-pi also works** for the version we vendor today (0.74.0), with zero patches needed because undici 7.x has a runtime feature check that already covers the `markAsUncloneable` gap.
- **The undici 8.x `markAsUncloneable` crash is the only Node-compat gap encountered under Deno.** All other concerns I had going in (TUI, jiti, subprocess, native modules) were either unfounded or already handled by Deno's node-compat layer.
- **The undici 8.x crash is fixable from outside the vendored tree.** A 7-line `--import` preload using `createRequire` mutates the cached `node:worker_threads` module object before undici loads. No vendored-file edits needed. Idempotent across versions.
- **My initial "no application-layer fix" claim for the undici crash was wrong.** A one-line patch silences it; a 7-line preload shim handles it cleanly.
- **There is a deno-specific failure mode with pi 0.75.5 + Mistral tool-use.** Confirmed by comparing against the compiled binary, which uses the same pi 0.75.5 code path but Bun's runtime — and works. Likely undici 8.x SSE streaming under deno's node-compat. Not investigated further; the precompiled binary sidesteps it.
- **Always control for pi version.** Mid-spike I almost diagnosed a deno-specific bug that turned out to be Deno resolving a newer pi than the node baseline. `deno.lock` (or version-pinned `npm:pkg@version`) will be mandatory in any production setup.

## Implications for the distribution story

The precompiled-binary path now looks like the cleanest distribution shape:

1. **Per-platform Python wheels** (cibuildwheel-style), each bundling the matching `pi-<platform>.tar.gz` from earendil-works's GitHub release.
1. **Or a pure-Python wheel + first-run downloader** that fetches the right binary from the GitHub release, pinned to a specific version with a known sha256. Smaller wheel, requires network on first use.
1. **libharness invokes `pi/pi` as a subprocess** exactly as it does today against node-pi — same `--mode rpc`, same `--extension <path>`. No protocol changes.

Trade-offs vs Deno-pi:

- **Precompiled binary trust model.** We're consuming earendil-works's CI-produced artefacts. Risk is roughly "the version we pinned was not compromised when their CI built it." Mitigations: pin to a specific version, pin the sha256 from the GitHub release API, optionally wait a quarantine interval (a few days) after a release before adopting it, optionally verify the same hash from a second source.
- **Deno-pi trust model.** Resolve all transitive npm packages once under Deno (no postinstall scripts), pin everything via `deno.lock` with sha512s, optionally vendor the resolved tree. Risk is "any of ~200 transitive packages was compromised at vendor time." Mitigations: lockfile integrity, vendor under deno (not npm install), optionally bundle into one binary via `deno compile`.
- **The precompiled binary's trust surface is narrower** — one artefact from one vendor (earendil-works) vs ~200 packages from ~50 maintainers. Whether that's better depends on how much you trust earendil-works's CI vs the long tail of pi's transitive deps. Given high trust in Armin and the earendil team, the precompiled binary is the smaller-surface option.

The conda-forge asymmetry (current Deno on aarch64, abandoned on linux-64) means **conda-forge is not a portable Deno channel for end users**. For our own dev box it's fine; for distribution we'd pull Deno binaries from GitHub releases anyway. Same shape as the precompiled-pi-via-GitHub-releases path, so the distribution mechanism is roughly equivalent — what differs is which artefact we consume.

## Open questions / what we did NOT validate

- TUI rendering and interactive input under either deno-pi or the precompiled binary. We only ran pi in `--mode rpc` (no TUI). The interactive TUI may surface different compatibility issues, particularly under Deno.
- `deno compile` against pi's full module graph. We ran `deno run npm:...`, not compile-to-binary. The compile step has its own restrictions (dynamic imports, FFI, native modules) that may or may not bite.
- Long-running stability (memory, file descriptors, signal handling) over real workloads, not single-prompt smoke tests. Both paths.
- Whether `openai-codex` provider (we also have credentials for it) behaves identically; we only exercised OpenRouter + Mistral.
- The deno-pi 0.75.5 + Mistral SSE streaming gap. We confirmed it exists and is deno-specific (not pi-internal); we didn't root-cause it. If the deno path is ever pursued seriously, this needs investigation.
- Whether the precompiled binary's resource handling (the `pi/` directory layout with sibling `node_modules/`, `assets/`, `theme/`) is robust when invoked with arbitrary CWDs. Our test ran with CWD = the spike directory; other CWDs may behave differently.

## Recommendations

- **Do not change the current dev loop yet.** Today's `make install-pi` + node-pi setup works and is documented. Both the precompiled-binary and Deno paths are validated alternatives, not replacements for the dev loop. *(Update 2026-05-24: superseded — the dev loop now uses the precompiled binary as the default; `make install-pi` invokes `python -m libharness._pi_vendor install`, the legacy node path lives at `make install-pi-node-deprecated` until the binary path is thoroughly vetted. See `dev-notes/2026-05-24-pi-vendoring-design.md`.)*
- **Treat the precompiled pi binary as the leading distribution candidate.** The artefacts exist for all six platforms we'd care about, libharness compatibility is verified, and the trust surface is narrower than a vendored npm tree. A Python wheel shape (either per-platform wheels with embedded binary, or pure-Python wheel + first-run download with sha256 verification) maps naturally onto this.
- **Keep Deno as a documented alternative** in case (a) we decide we don't want to consume Bun-built artefacts at all, or (b) earendil-works ever stops publishing precompiled binaries. The deno-pi-0.75.5 SSE streaming bug needs root-causing before that path is production-ready, but for pi 0.74 it works as-is.
- **Before any pi version bump,** re-run the smokes against all three runtime paths (node-pi, deno-pi, precompiled pi) at the new version, with version pinning explicit, to catch any runtime-pi interaction regressions.
- **For the precompiled binary, adopt a release-quarantine policy.** A short window (a few days) between earendil-works publishing a release and libharness bumping to it gives the community time to flag a compromised release.

## Re-verification under v8 (this workspace)

The original spike was conducted against `PiPythonHarness` (async surface) in the sibling workspace. The handoff document called out the "precompiled binary is a drop-in" claim as the load-bearing one to re-verify under v8 in this workspace. Re-verification covers four corners — faux/live × `PiAgentHarness`/`Agent`-subclass — plus a regression test that locks the minimum bar into the suite.

Procedure executed in this workspace (2026-05-23):

1. **Binary fetch + integrity.** Downloaded `pi-linux-arm64.tar.gz` from the pi v0.75.5 GitHub release into `experiments/precompiled-pi-spike/`, verified sha256 `8b5c66fc3baee5fea4077f62307f792d6293660315b3ce4d0940c49353b45f50`, extracted to `experiments/precompiled-pi-spike/pi/`, confirmed `pi/pi --version` reports `0.75.5`.

1. **Smoke 1 — faux provider × `PiAgentHarness`.** `experiments/precompiled-pi-spike/smoke_faux_v8.py`. Sync facade: `with PiAgentHarness(registry, config=PiLaunchConfig(pi_command=[<bin>]), fake_provider=True, fake_tool_name="echo", fake_tool_args={"message": "..."}) as h: h.prompt_and_wait("trigger", timeout=60)`. Pass criterion: the Python `echo` tool was invoked with the expected argument.

1. **Smoke 2 — faux provider × `Agent` subclass.** `experiments/precompiled-pi-spike/smoke_faux_agent.py`. Subclass of `Agent` registering an `on_tool_call` notification hook plus the `echo` tool; sync `with MyAgent(...) as a:` lifecycle. Pass criterion as Smoke 1, additionally asserts the hook fired against a bridge event from pi.

1. **Smoke 3 — live LLM × `PiAgentHarness`.** `experiments/precompiled-pi-spike/smoke_live_v8.py`. Same shape as Smoke 1 but with `fake_provider=False` and OpenRouter + Mistral large per the original spike. Pass criterion: Mistral called the `echo` tool with a message containing the prompted string.

1. **Smoke 4 — live LLM × `Agent` subclass.** `experiments/precompiled-pi-spike/smoke_live_agent.py`. Smoke 2 shape with the live provider.

1. **Regression test.** `tests/pi/test_precompiled_pi_binary.py`, marked `live`, runs the faux-provider × `PiAgentHarness` flow and skips when `PI_PRECOMPILED` is unset. Validated under both `local.venv/` (3.11) and `local-ft.venv/` (3.14t).

### Results (2026-05-23)

All four smokes passed first try. The regression test passed on both venvs.

| Smoke / test                                             | Result                                                                |
| -------------------------------------------------------- | --------------------------------------------------------------------- |
| `smoke_faux_v8.py` (faux × `PiAgentHarness`)             | PASS — `echo` called with expected args                               |
| `smoke_faux_agent.py` (faux × `Agent` subclass)          | PASS — `echo` called, `on_tool_call` hook fired for `toolName='echo'` |
| `smoke_live_v8.py` (Mistral large × `PiAgentHarness`)    | PASS — Mistral called `echo` with the prompted token                  |
| `smoke_live_agent.py` (Mistral large × `Agent` subclass) | PASS — tool call + hook both fired                                    |
| `pytest tests/pi/test_precompiled_pi_binary.py` on 3.11  | PASS (0.39s)                                                          |
| `pytest tests/pi/test_precompiled_pi_binary.py` on 3.14t | PASS (0.44s)                                                          |

The load-bearing claim survives the port: the precompiled pi binary works as a drop-in for libharness under v8 — both the sync `PiAgentHarness` facade and the `Agent` subclass surface route bridge events and tool-call invocations correctly against bundled-into-the-binary module specifiers.

## Artefacts

In this workspace (`~/git/github/libharness--asyncio-in-thread/`):

- `experiments/precompiled-pi-spike/pi-linux-arm64.tar.gz` — the downloaded binary asset (sha256 `8b5c66fc3baee5fea4077f62307f792d6293660315b3ce4d0940c49353b45f50`).
- `experiments/precompiled-pi-spike/pi/pi` — the extracted ~113 MB bun-compiled standalone (pi 0.75.5).
- `experiments/precompiled-pi-spike/smoke_faux_v8.py` — faux-provider round-trip via sync `PiAgentHarness` against the compiled binary.
- `experiments/precompiled-pi-spike/smoke_faux_agent.py` — faux-provider round-trip via `Agent` subclass + hook.
- `experiments/precompiled-pi-spike/smoke_live_v8.py` — live-LLM round-trip via sync `PiAgentHarness`.
- `experiments/precompiled-pi-spike/smoke_live_agent.py` — live-LLM round-trip via `Agent` subclass + hook.
- `tests/pi/test_precompiled_pi_binary.py` — regression test (`live`-marked); runs when `PI_PRECOMPILED` is set to a binary path.

In the sibling workspace (`~/git/github/libharness/`, threads-rewrite branch; not duplicated here):

- `experiments/deno-pi-spike/smoke.py` — live-LLM round-trip via deno-launched pi @ 0.74.0.
- `experiments/deno-pi-spike/smoke_faux.py` — faux-provider round-trip via deno-launched pi.
- `experiments/deno-pi-spike/deno-node-compat-shim.mjs` — the 7-line `--import` preload for undici 8.x compat (only needed for pi ≥ 0.75 under deno).
- `experiments/precompiled-pi-spike/{smoke_faux,smoke_live}.py` — original spike smokes (async `PiPythonHarness` surface).

External references:

- yt-dlp issue [16766](https://github.com/yt-dlp/yt-dlp/issues/16766) — context for Bun-trajectory concerns (relevant if we ever consider building our own bun-compiled binary; less relevant when consuming earendil-works's prebuilt artefacts).
- Undici 7.x graceful fallback: `node_modules/undici/lib/web/webidl/index.js:161` (inside `.sandbox/pi-install/`).
- Undici 8.x regression: `~/.cache/deno/npm/registry.npmjs.org/undici/8.3.0/lib/web/cache/cachestorage.js:20`.
- Pi releases: <https://github.com/earendil-works/pi/releases> (six precompiled assets per release; v0.75.5 published 2026-05-23).
