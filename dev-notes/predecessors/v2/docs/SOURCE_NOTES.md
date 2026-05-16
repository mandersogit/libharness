# Source notes

These notes summarize local source-code evidence from the uploaded Pi tree.

## Package and repo structure

- `package.json`: monorepo with package-manager/workspace setup.
- `packages/coding-agent/package.json`: coding-agent package version `0.74.0`.
- `README.md`: identifies packages such as `@earendil-works/pi-coding-agent`, `@earendil-works/pi-agent-core`, and `@earendil-works/pi-ai`.

## RPC mode

Files:

- `packages/coding-agent/docs/rpc.md`
- `packages/coding-agent/src/modes/rpc/rpc-types.ts`
- `packages/coding-agent/src/modes/rpc/rpc-mode.ts`
- `packages/coding-agent/src/modes/rpc/jsonl.ts`
- `packages/coding-agent/src/modes/rpc/rpc-client.ts`

Important findings:

- RPC mode is explicitly documented for headless operation through stdin/stdout JSONL.
- RPC commands are typed as a closed union in `rpc-types.ts`.
- Commands include prompt, steer, follow_up, abort, new_session, get_state, set_model, get_available_models, thinking level controls, queue controls, compaction, retry, bash, sessions, messages, and get_commands.
- No RPC command registers or mutates tools directly.
- Extension UI requests can be emitted over RPC and answered through `extension_ui_response` records.
- JSONL framing is LF-only; Node `readline` is explicitly avoided in Pi's own RPC code.

## Extension API and tool registration

Files:

- `packages/coding-agent/src/core/extensions/types.ts`
- `packages/coding-agent/src/core/extensions/loader.ts`
- `packages/coding-agent/docs/extensions.md`

Important findings:

- `ExtensionAPI.registerTool()` registers tools callable by the LLM.
- `ToolDefinition` includes `name`, `label`, `description`, optional prompt additions, `parameters`, `executionMode`, `execute()`, and optional custom renderers.
- The extension loader supports TypeScript/JavaScript modules and aliases Pi packages.
- Documentation states `pi.registerTool()` works during extension load and after startup.

## Session runtime

Files:

- `packages/coding-agent/src/core/agent-session.ts`
- `packages/agent/src/agent-loop.ts`

Important findings:

- `AgentSession` combines built-in tools, custom tools, and extension-registered tools.
- Active tool filtering is enforced by name.
- The agent loop validates tool calls, emits execution events, executes tool functions, catches tool errors, and emits tool result messages.

## Schema validation

File:

- `packages/ai/src/utils/validation.ts`

Important findings:

- Pi uses TypeBox validation but detects schemas without TypeBox metadata and applies JSON Schema-like coercion.
- This supports a Python bridge that emits JSON Schema parameter objects.

## SDK route

Files:

- `packages/coding-agent/docs/sdk.md`
- `packages/coding-agent/src/core/agent-session.ts`

Important findings:

- The SDK can create an agent session programmatically.
- It supports `customTools` and extension-loaded tools.
- This is powerful for TypeScript applications, but less aligned with a Python-exclusive customization goal.
