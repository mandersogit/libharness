# Sandbox scripts

These scripts manage the **legacy node-pi sandbox** — the npm install of pi-coding-agent (under a sandboxed Node) that was the original dev-loop runtime. They are the implementation behind the `make install-pi-node-deprecated`, `make login`, `make pi`, `make test`, and `make smoke-pi` targets. Prefer the `make` targets in normal use.

As of 2026-05-24, the primary `make install-pi` target fetches the
precompiled pi binary from earendil-works's GitHub release (via
`python -m libharness._pi_vendor install`) into
`src/libharness/_vendor/pi/`. The node-pi path is still here for
fallback / comparison; it will be removed once the binary path is
thoroughly vetted. See
`dev-notes/2026-05-24-pi-vendoring-design.md`.

The Python venv (`local.venv/`) is managed by `make install`, not by
these scripts.

## What lives where

```text
.sandbox/         created by these scripts
  nodeenv/        standalone Node LTS (via nodeenv)
  pi-install/     @earendil-works/pi-coding-agent npm package
  npm-cache/      project-local npm cache (keeps ~/.npm clean)
  pi-home/        fake HOME for pi (sessions, settings, auth.json)

local.venv/       created by `make install`
```

## OAuth login

Pi supports subscription-based OAuth for ChatGPT Plus/Pro (the "OpenAI
Codex" provider, officially endorsed by OpenAI), Claude Pro/Max, and
GitHub Copilot. For API-key providers, set the env var
(`OPENROUTER_API_KEY`, etc.) or use `/login` to store the key in
`auth.json`.

```bash
make login              # opens the sandboxed pi TUI; type /login
```

Inside the TUI: `/login` → pick provider → approve in browser → `/quit`.
The token persists at `.sandbox/pi-home/.pi/agent/auth.json` and never
touches your real `~/.pi/`. To clear, `rm` that file or use `/logout`.

## Individual scripts

| Script              | Make target                    | Purpose                           |
| ------------------- | ------------------------------ | --------------------------------- |
| `setup-node.sh`     | (`install-pi-node-deprecated`) | nodeenv install of Node LTS       |
| `setup-pi.sh`       | (`install-pi-node-deprecated`) | npm install pi-coding-agent       |
| `pi.sh [args...]`   | `pi ARGS="..."`                | Sandboxed pi CLI                  |
| `login.sh`          | `login`                        | OAuth login flow (interactive)    |
| `smoke-pi.sh`       | `smoke-pi`                     | RPC `get_state` sanity check      |
| `test.sh [args...]` | `test`/`test-live`             | pytest with `HOME`/`PI_CLI` wired |
| `clean.sh`          | `clean-sandbox`                | Remove `.sandbox/`                |

## Common overrides

| Env var          | Default  | Used by            |
| ---------------- | -------- | ------------------ |
| `PI_PKG_VERSION` | `0.74.0` | `setup-pi.sh`      |
| `NODE_CHANNEL`   | `lts`    | `setup-node.sh`    |
| `PI_OFFLINE`     | `1`      | `pi.sh`, `test.sh` |

## Sandboxing model

- All install state goes under `.sandbox/`.
- npm caches into `.sandbox/npm-cache/` (via `NPM_CONFIG_CACHE`),
  not `~/.npm/`.
- Pi runs with `HOME=.sandbox/pi-home/`, so every `homedir()` /
  `~/.pi/` lookup pi performs lands inside the sandbox.
- `test.sh` and `pi.sh` only override `HOME` and prepend the sandboxed
  Node bin to `PATH` — the rest of your shell env is left alone.
- `make clean-sandbox` removes the entire sandbox in one step.
