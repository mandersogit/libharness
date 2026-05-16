# Assessment: Python-first use of Pi as an agentic harness

## Decision

I recommend the **Python parent + Pi RPC mode + generated TypeScript shim** approach.

This should be treated as the default architecture if your hard constraint is: "customization, tools, and environment orchestration should be authored in Python; TypeScript should be limited to what Pi strictly requires."

## Evaluation criteria

The assessment used these criteria:

1. Python authoring surface.
2. Fidelity to Pi's upstream behavior.
3. Stability of the integration surface.
4. Amount of TypeScript required.
5. Ability to implement custom tools.
6. Ability to implement richer extension semantics later.
7. Operational simplicity.
8. Security and policy isolation.
9. Maintainability under Pi upgrades.

## Approach comparison

| Approach | Summary | Strengths | Weaknesses | Recommendation |
|---|---|---|---|---|
| Python launches `pi --mode rpc`; generated TS shim forwards tool calls to Python | Python is parent/control plane. Pi is subprocess. TS shim only registers/forwards tools. | Strong Python authoring; uses Pi-supported RPC and extension surfaces; preserves Pi internals; minimal TS; easy to test the Python side. | Requires Node/Pi runtime; needs bridge protocol; full extension semantics need more shim work; UI custom components do not translate cleanly to RPC. | **Recommended** |
| Python launches `pi --mode rpc`; no extension shim | Python can prompt, steer, inspect state/events. | Simplest embedding; zero TS. | Cannot define Python tools callable by the LLM except by asking Pi to call built-ins or external shell commands; poor fit for custom environments. | Not enough for your stated goal |
| Use Pi SDK / `AgentSession` directly from TypeScript | Node process embeds Pi without subprocess. | First-class programmatic API; lower IPC overhead; likely best for TS applications. | Moves core application authoring into TS or requires a Node sidecar anyway. | Good for TS, not for your Python-first constraint |
| Use `@earendil-works/pi-agent-core` / low-level harness directly | Build a custom host around Pi core internals. | Potentially deepest control. | Uploaded source marks `AgentHarness` semantics as still provisional; more coupling to internals; Python still needs Node bridge; greater upgrade risk. | Avoid as primary path for now |
| Write all tools as TypeScript extensions | Native to Pi. | Maximum Pi extension fidelity; easiest custom rendering and event hooks. | Violates the Python-first constraint. | Use only for tiny generated adapters |
| Python-only agent framework instead of Pi | Use LangGraph-like or custom Python runtime. | Fully Python-native. | Loses Pi's specific harness, sessions, CLI/TUI/RPC behavior, provider integration, and extension ecosystem. | Consider only if Pi itself is not required |
| Fork/port Pi to Python | Reimplement Pi harness in Python. | Theoretical full control. | Very high cost; loses upstream velocity; duplicates difficult agent-loop/session/tooling behavior. | Not recommended |
| External HTTP/MCP-like tool server called by a TS extension | TS extension exposes remote tools to Pi. | Good for distributed tools; language-agnostic. | Heavier than needed for local Python harness; auth and lifecycle complexity. | A variant to consider for remote/multi-process deployments |

## Why the recommended approach is coherent with Pi

The uploaded source and public docs both point to the same split:

- RPC mode is explicitly intended for embedding Pi headlessly in other applications.
- Extensions are explicitly the route to registering tools callable by the LLM.
- The extension loader can load TypeScript without compilation.
- The RPC mode already has an extension UI request/response path, so extension operations can be mediated from a non-TUI client where supported.
- Pi's validation code accepts plain JSON Schema objects in addition to TypeBox-style schemas, so Python can author tool schemas as JSON data rather than TypeScript TypeBox code.

That means the TypeScript shim does not need to encode business logic. It can be a static adapter:

1. Load JSON manifest.
2. For each manifest tool, call `pi.registerTool(...)`.
3. In `execute(...)`, send a JSONL request to Python.
4. Forward Python progress messages to Pi's `onUpdate`.
5. Return Python's final result.

## Why not direct `AgentHarness` as the core building block

The package name `pi-agent-core` is attractive, but the uploaded source suggests caution. `packages/agent/docs/agent-harness.md` says the lifecycle/settlement semantics are still provisional and lists remaining work before treating `AgentHarness` as migration-ready. It also says the harness should likely remove its internal `Agent` dependency.

For a Python integration, that argues against binding to `AgentHarness` internals first. The safer public layer is `pi --mode rpc` plus the coding-agent extension API.

## Security posture

The generated shim still executes as a Pi extension, and Pi extensions run in the Node process. That means the shim should be treated as trusted code. To keep the attack surface small:

- Generate the shim from a fixed template.
- Store it in a temp directory with normal user-only permissions where possible.
- Bind the Python tool server to loopback.
- Use a random bearer token for every run.
- Prefer explicit `--extension <generated-shim>` over auto-discovery when running controlled harnesses.
- Consider `--no-extensions` and an explicit extension allowlist if you need to prevent unrelated project/global extensions from loading.
- Consider `--no-builtin-tools` or `--tools ...` when Python should own the tool environment boundary.
- Run Pi inside a project sandbox/container when tools may modify files.

## Recommended phasing

### Phase 1: Tool bridge

Implement Python-authored tools and a Python RPC client. This is what the included prototype does.

### Phase 2: Event bridge

Add Python handlers for Pi extension events such as `tool_call`, `tool_result`, `session_start`, and `message_end`. The shim would register event handlers and call into Python synchronously where the Pi event contract permits blocking or mutation.

### Phase 3: Command and state bridge

Expose `pi.registerCommand`, `pi.sendMessage`, `pi.appendEntry`, `pi.setActiveTools`, and related APIs through the bridge. This lets Python define slash commands and mutate Pi session state.

### Phase 4: Packaging and policy

Add a reproducible packaging mode that pins the generated shim, manifests, and Python package version. Add policy controls for filesystem writes, command execution, and tool concurrency.

## Bottom line

The Opus-suggested approach is sound. It is not merely a workaround; it maps cleanly onto Pi's actual architecture. The main correction is to keep the TypeScript shim deliberately boring and generated, and to make Python the source of truth for tools, schemas, launch policy, and orchestration.
