# Source findings from uploaded Pi codebase

The uploaded source archive was treated as the primary evidence. Paths below are relative to the extracted repository root.

## Package identity and license

- `README.md` describes the repository as the Pi Agent Harness mono repo and lists:
  - `@earendil-works/pi-coding-agent`
  - `@earendil-works/pi-agent-core`
  - `@earendil-works/pi-ai`
- `LICENSE` is MIT.
- `packages/coding-agent/package.json` is `@earendil-works/pi-coding-agent` version `0.74.0`, license MIT.
- `packages/coding-agent/package.json` requires Node `>=20.6.0`.
- root `package.json` requires Node `>=20.0.0`.

## RPC mode is a supported embedding surface

`packages/coding-agent/docs/rpc.md`:

- Lines 1-5: RPC mode enables headless operation over JSON stdin/stdout and is useful for embedding Pi in other applications, IDEs, or custom UIs. It also says Node/TypeScript users should consider `AgentSession` directly instead of spawning a subprocess.
- Lines 7-17: `pi --mode rpc [options]`, including `--provider`, `--model`, `--no-session`, and `--session-dir`.
- Lines 19-36: protocol overview, command/response/event split, request IDs, and strict LF-only JSONL framing.
- Lines 42-75: `prompt` semantics. A successful response means the prompt was accepted/queued/handled; later failures are reported through events/messages.
- Lines 79-119: `steer` and `follow_up` queueing semantics.

`packages/coding-agent/src/modes/rpc/rpc-mode.ts`:

- Lines 44-55: `runRpcMode()` takes over stdout and writes serialized JSON lines.
- Lines 72-120: extension UI dialog requests are tracked and resolved from RPC responses.
- Lines 188-200: RPC mode only supports string-array widgets; component factories are ignored.
- Lines 310-348: the runtime binds extensions with an RPC UI context and forwards session events to stdout.
- Lines 688-735: stdin JSON lines are parsed, extension UI responses are handled, commands are dispatched, and errors are emitted as RPC responses.

`packages/coding-agent/src/modes/rpc/rpc-client.ts`:

- Implements a TypeScript subprocess client that starts the agent in RPC mode. This validates that Pi itself treats subprocess RPC as a real integration path, not just a documentation example.

## Extensions are the native customization surface

`packages/coding-agent/docs/extensions.md`:

- Lines 3-7: extensions are TypeScript modules; they can register tools, subscribe to events, add commands, and more. They can be loaded from global/project locations or with `pi -e ./path.ts`.
- Lines 9-16: key capabilities include custom tools, event interception, user interaction, custom UI components, commands, session persistence, and custom rendering.
- Lines 138-151: extension imports include `@earendil-works/pi-coding-agent`, `typebox`, `@earendil-works/pi-ai`, `@earendil-works/pi-tui`, npm dependencies, and Node built-ins.
- Lines 153-180: an extension exports a default factory function; extensions are loaded via `jiti`, so TypeScript works without compilation; async factories are awaited before startup continues.
- Lines 1217-1229: `pi.registerTool()` works during extension load and after startup, refreshes tools immediately, and supports `promptSnippet` and `promptGuidelines`.
- Lines 1660-1675: custom tools are registered with `pi.registerTool()` and can include prompt snippets/guidelines. The docs warn to be careful with tools that mutate files and recommend queueing/locking semantics.
- Lines 1702-1763: tool definitions include schema, execute functions, updates, and result structures.
- Lines 1824-1839: built-in tools can be overridden; there is also a `--no-builtin-tools` option.

`packages/coding-agent/src/core/extensions/types.ts`:

- Tool definitions include `name`, `label`, `description`, `promptSnippet`, `promptGuidelines`, `parameters`, `executionMode`, and `execute(toolCallId, params, signal, onUpdate, ctx)`.
- The `ExtensionAPI` includes event subscription, tool registration, messaging, command registration, and active tool management methods.

`packages/coding-agent/src/core/extensions/loader.ts`:

- `registerTool` adds the tool to the extension's tool collection and refreshes tool state.

`packages/coding-agent/src/core/extensions/runner.ts`:

- First registration by tool name wins across extensions. This matters if the Python shim registers a name that collides with another extension.

## Plain JSON Schema can work for Python-authored tools

`packages/ai/src/utils/validation.ts`:

- Lines 292-323 validate tool call arguments.
- Lines 296-299 explicitly branch when the tool parameters do not have TypeBox metadata but are a JSON Schema object, then coerce using JSON Schema.

This is important because the Python manifest can emit JSON Schema directly. The generated TypeScript shim can pass `parameters: tool.parameters as any` rather than authoring TypeBox schemas.

## SDK and direct AgentSession are better for TypeScript, not for Python-first customization

`packages/coding-agent/docs/sdk.md`:

- The SDK provides programmatic access to Pi's agent capabilities and has `createAgentSession()` / `AgentSession` concepts.
- This is useful if the host application is TypeScript. For Python, using it directly would still require a Node sidecar. RPC mode is the cleaner Python boundary.

## Low-level AgentHarness is not yet the safest integration target

`packages/agent/docs/agent-harness.md`:

- Lines 1-19 describe lifecycle hardening goals and state that a final lifecycle hardening pass should prove guarantees with tests.
- Lines 77-103 describe explicit operation phases and say phase/settlement semantics are still provisional.
- Lines 213-233 list implementation TODOs before treating `AgentHarness` as migration-ready, including removing the internal `Agent` dependency.

`packages/agent/src/harness/agent-harness.ts`:

- The class still imports and owns an internal `Agent` instance.

Conclusion: for a stable Python integration, prefer the coding-agent RPC and extension surfaces over direct low-level harness binding.

## Runtime limitation observed here

The container had:

```text
node v18.20.4
npm 9.2.0
python 3.11.8
```

The uploaded source requires Node 20+. `npm install --ignore-scripts` emitted multiple `EBADENGINE` warnings and did not complete. A live Pi run could not be meaningfully performed in this environment.
