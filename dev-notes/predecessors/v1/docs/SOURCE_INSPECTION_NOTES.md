# Source Inspection Notes

These notes are based on the uploaded Pi source tree, not only public documentation.

## Relevant source surfaces

### RPC mode

Files:

- `packages/coding-agent/src/modes/rpc/rpc-mode.ts`
- `packages/coding-agent/src/modes/rpc/rpc-types.ts`
- `packages/coding-agent/src/modes/rpc/jsonl.ts`
- `packages/coding-agent/src/modes/rpc/rpc-client.ts`

Findings:

- RPC mode is line-oriented JSON over stdin/stdout.
- It supports command/response correlation via `id`.
- It emits agent lifecycle events and extension UI requests.
- It exposes state/model/session/message/bash/command operations.
- The JSONL framing is deliberately strict: split only on LF and strip a trailing CR.

### Extension API

Files:

- `packages/coding-agent/src/core/extensions/types.ts`
- `packages/coding-agent/src/core/extensions/loader.ts`
- `packages/coding-agent/src/core/extensions/runner.ts`
- `packages/coding-agent/src/core/tools/tool-definition-wrapper.ts`

Findings:

- Extensions are TypeScript modules loaded with `jiti`.
- Extensions can register tools, commands, providers, flags, shortcuts, message renderers, and event handlers.
- `registerTool()` is the native first-class path for LLM-callable tools.
- Tool execution receives `toolCallId`, validated params, abort signal, update callback, and extension context.
- The loader aliases Pi packages for extensions, which makes a standalone shim feasible.

### Agent session / tool registry

Files:

- `packages/coding-agent/src/core/agent-session.ts`
- `packages/agent/src/agent-loop.ts`
- `packages/agent/src/types.ts`

Findings:

- `AgentSession` builds the runtime tool registry from built-ins, extension tools, and SDK `customTools`.
- Extension tools are included by default unless the active tool allowlist excludes them.
- Tool execution supports partial updates and sequential/parallel execution modes.
- Tool errors are represented by throwing during execution; Pi then emits error tool results.

### Provider registration

Files:

- `packages/coding-agent/src/core/model-registry.ts`
- `packages/coding-agent/src/core/extensions/types.ts`

Findings:

- Extensions can register providers and models.
- Provider registration can include a `streamSimple` function.
- Custom provider models require a base URL, API key or OAuth, API identifier, and model definitions.
- A no-LLM fake provider is practical for integration tests.

### Lower-level harness

Files:

- `packages/agent/src/harness/agent-harness.ts`

Findings:

- Pi also contains a lower-level `AgentHarness` with hooks, queued steering/follow-up, tool-call callbacks, and resource composition.
- This is attractive for a future SDK sidecar, but not for the minimal Python-first path because it requires substantial TypeScript ownership.
