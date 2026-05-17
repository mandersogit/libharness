# Source Findings

## Uploaded Pi source

Key files inspected:

- `packages/coding-agent/src/modes/rpc/rpc-types.ts`
- `packages/coding-agent/src/core/extensions/types.ts`
- `packages/coding-agent/src/cli/args.ts`
- `packages/ai/src/utils/validation.ts`
- `packages/agent/README.md`
- `packages/coding-agent/package.json`

Findings:

- Pi RPC supports session control, prompting, steering, messages, commands, bash, model selection, and extension UI responses.
- Pi RPC does not define dynamic tool registration.
- Tool registration is available through `ExtensionAPI.registerTool()`.
- Tool definitions use TypeBox schemas, but validation has a JSON-Schema-compatible path.
- CLI flags support deterministic disabling of ambient resources while explicitly loading a bridge extension.
- Package metadata identifies the coding agent package as `@earendil-works/pi-coding-agent` with version `0.74.0` in the uploaded source.

## Public documentation checked

The current public docs align with the source-level findings:

- RPC mode is a JSON stdin/stdout embedding interface.
- Extensions are TypeScript modules and can register tools, events, commands, providers, UI, and persistence.
- The SDK is the native Node/TypeScript embedding path.
- CLI usage documents modes and flags, including RPC, extension loading, and tool disabling options.
- Current package naming is under the `@earendil-works` scope.

## Consequence for design

A Python-only customization layer is possible for tool authoring and harness orchestration, but not with literally zero TypeScript. The smallest robust TypeScript surface is a generic extension bridge that registers tools and delegates execution to Python.
