# Source-code evidence from the uploaded Pi repository

The assessment is based primarily on the uploaded repository extracted at `/mnt/data/pi-main`.

## Pi RPC is a JSONL process protocol

- `packages/coding-agent/src/modes/rpc/rpc-types.ts:1-6` states that commands are JSON lines on stdin, and responses/events are JSON lines on stdout.
- `packages/coding-agent/src/modes/rpc/rpc-types.ts:19-69` enumerates the RPC command union. It includes prompt/state/model/session/bash commands, but no dynamic `register_tool` command.
- `packages/coding-agent/src/modes/rpc/jsonl.ts:4-19` implements strict LF-only JSONL framing and explicitly avoids Node `readline` because Unicode separators may appear inside JSON strings.
- `packages/coding-agent/src/modes/rpc/rpc-mode.ts:48` exposes `runRpcMode(runtimeHost)`.
- `packages/coding-agent/src/main.ts:673-675` enters `runRpcMode(runtime)` when `--mode rpc` is selected.

## Pi extensions are the native custom-tool registration surface

- `packages/coding-agent/src/core/extensions/types.ts:424-461` defines `ToolDefinition`: `name`, `label`, `description`, `parameters`, optional prompt metadata, `executionMode`, and `execute(...)` returning `AgentToolResult`.
- `packages/coding-agent/src/core/extensions/types.ts:1084-1135` defines `ExtensionAPI` and its `registerTool(...)` method.
- `packages/coding-agent/docs/extensions.md:1660-1763` documents custom tool registration, prompt snippets/guidelines, updates, returned `content`/`details`, and error signaling by throwing.
- `packages/coding-agent/src/core/agent-session.ts:2244-2301` merges extension-registered tools and SDK custom tools into the runtime tool registry.

## CLI extension loading supports an explicit shim path

- `packages/coding-agent/src/cli/args.ts:237-238` documents `--extension <path>` and `--no-extensions`.
- `packages/coding-agent/src/main.ts:517-545` resolves CLI extension paths and passes them to `createAgentSessionServices` as `additionalExtensionPaths`.
- `packages/coding-agent/src/core/resource-loader.ts:395-400` loads CLI-provided extension paths even when auto-discovery is disabled.

## SDK embedding is excellent, but TypeScript-first

- `packages/coding-agent/docs/sdk.md:1-27` presents the SDK for programmatic access to Pi.
- `packages/coding-agent/docs/sdk.md:520-540` shows direct custom tools via `customTools` and `defineTool`.
- `packages/coding-agent/docs/sdk.md:544-562` shows extension loading through `DefaultResourceLoader`.

For a Node/TypeScript application, the SDK is likely preferable. For a Python-first application, RPC avoids building the host in TypeScript.

## Result shape for tools

- `packages/agent/src/types.ts:336-347` defines `AgentToolResult<T>` with `content`, `details`, and optional `terminate`.
- `packages/agent/src/types.ts:349-367` defines `AgentToolUpdateCallback` and `AgentTool.execute(...)`.

The Python bridge returns this same shape from Python and lets the TS shim pass it through to Pi.

## JSON Schema compatibility

- `packages/ai/src/utils/validation.ts:292-319` validates tool-call arguments against `tool.parameters` and has explicit handling for non-TypeBox JSON Schema objects.
- `packages/ai/src/providers/openai-completions.ts:983` and `packages/ai/src/providers/openai-responses-shared.ts:274` treat tool parameters as JSON Schema.

The generated TS shim can therefore use Python-emitted JSON Schema directly instead of generating TypeBox code per tool.
