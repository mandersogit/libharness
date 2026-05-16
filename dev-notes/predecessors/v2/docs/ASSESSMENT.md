# Assessment: Python-first use of Pi as an agentic harness

## Method

This assessment used two inputs:

1. The uploaded Pi source tree at `/mnt/data/pi_src/pi-main`.
2. Current public documentation and release information for Pi, checked on 2026-05-14.

The source tree was weighted more heavily than public documentation because it exposes the actual protocol, extension API, loader behavior, schema validation, and session runtime.

## Source-grounded findings

### 1. RPC mode is the correct non-Node control plane

Relevant source files:

- `packages/coding-agent/docs/rpc.md`
- `packages/coding-agent/src/modes/rpc/rpc-types.ts`
- `packages/coding-agent/src/modes/rpc/rpc-mode.ts`
- `packages/coding-agent/src/modes/rpc/jsonl.ts`
- `packages/coding-agent/src/modes/rpc/rpc-client.ts`

The RPC protocol is JSONL over stdin/stdout. Commands include prompt/control/state/model/session/message operations. Events stream asynchronously. The protocol explicitly uses LF-only JSONL framing and warns not to use generic line readers that split on Unicode line separators.

The command union in `rpc-types.ts` does **not** include a command such as `register_tool`, `load_tool`, or `execute_tool`. That is the central design constraint: Python can drive Pi through RPC, but Python cannot register tools with Pi through RPC alone.

### 2. Tool registration is in the extension API

Relevant source files:

- `packages/coding-agent/src/core/extensions/types.ts`
- `packages/coding-agent/src/core/extensions/loader.ts`
- `packages/coding-agent/docs/extensions.md`

`ExtensionAPI.registerTool()` is the native path for adding LLM-callable tools. A tool definition includes `name`, `label`, `description`, optional prompt additions, `parameters`, `executionMode`, and an async `execute()` implementation. Extensions can also register commands, providers, UI interactions, and event hooks.

`loader.ts` uses `jiti` to load TypeScript/JavaScript extension modules and provides aliases for Pi packages. This makes a generated TS shim viable because it can import `@earendil-works/pi-coding-agent` types from the Pi runtime environment rather than requiring a separately bundled extension package.

### 3. Plain JSON Schema is acceptable enough for a Python bridge

Relevant source file:

- `packages/ai/src/utils/validation.ts`

Pi's validation path uses TypeBox, but the implementation has explicit accommodation for plain JSON Schema objects: it detects lack of TypeBox metadata and applies JSON-schema-oriented coercion. This is important because Python tools can emit JSON Schema without constructing TypeBox objects.

### 4. The TypeScript SDK is strong but not Python-first

Relevant source files:

- `packages/coding-agent/docs/sdk.md`
- `packages/coding-agent/src/core/agent-session.ts`

The SDK path allows programmatic `createAgentSession()` use, direct `customTools`, extension loading, model/resource configuration, and session control. This is likely the cleanest path for a TypeScript application. It is not the cleanest path for a Python-first application because the host process and custom tools become TypeScript-first.

### 5. Tool execution is handled by the real agent loop

Relevant source files:

- `packages/agent/src/agent-loop.ts`
- `packages/coding-agent/src/core/agent-session.ts`

Once a tool is registered, Pi's existing agent runtime handles schema validation, tool execution events, result messages, and active-tool filtering. A TS shim that forwards execution to Python lets Python use these existing mechanics rather than reimplementing the agent loop.

## Alternative approaches

| Approach | Python ownership | Pi-core reuse | TypeScript surface | Main advantage | Main drawback | Verdict |
|---|---:|---:|---:|---|---|---|
| Python RPC host + generated TS shim + persistent Python tool server | High | High | Low | Aligns with Pi's actual boundaries | Bridge protocol/lifecycle must be maintained | Recommended |
| TypeScript SDK host with Python tools called as subprocesses | Medium | Very high | High | Most direct Pi API access | Host logic moves to TS | Good only if TS is acceptable |
| Pure TypeScript extensions that shell out to Python per call | Medium | High | Medium | Simple initial path | Process-per-tool-call overhead and ad hoc plumbing | Useful prototype, not ideal architecture |
| Fork Pi to add Python extension loader or RPC `register_tool` | Very high | High | Low after fork | Best Python ergonomics if accepted upstream | High maintenance and version drift | Not first move |
| MCP-style adapter | Medium | Medium | Medium | Standardized external tool interface | Pi does not ship built-in MCP; extra layer | Use only if MCP compatibility is a requirement |
| Wrap Pi CLI/print mode from Python, no tool integration | High | Low/medium | None | Very simple | Loses agentic tool integration | Insufficient for this goal |

## Recommendation

Adopt the Opus approach, but implement it as a disciplined product architecture:

- Python is the application and orchestration layer.
- Pi runs as a managed RPC subprocess.
- The TypeScript shim is generated, generic, and carries no domain logic.
- Python tool metadata and execution are served by a persistent JSONL process.
- Tests use a deterministic faux provider so the bridge can be exercised without spending LLM tokens.

This approach matches the source code's separation of concerns. RPC controls Pi sessions, and extensions register tools. The TS shim is therefore not optional if you want to avoid modifying Pi while keeping tools callable by the model.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Pi extension API changes | Shim may break | Keep shim generated from one Python function; integration-test against each Pi version |
| Python tool subprocess dies | Tool calls fail | Restart policy, health checks, stderr capture, clear errors |
| Cancellation not propagated | Long Python tools keep running after abort | Add request-level cancellation messages and cooperative cancellation tokens |
| JSON Schema mismatch | Tool calls fail validation | Allow explicit schemas; add contract tests against Pi validation |
| Security exposure | Extensions and Python tools have process-level permissions | Run in controlled working dirs, restrict active tools, document trust boundary |
| Rich UI gap | Python tools cannot fully render custom TUI components | Use RPC UI primitives or small TS renderers only when essential |

## Decision rule

Use the recommended architecture unless one of these becomes true:

- You accept TypeScript as the primary implementation language. Then use Pi's SDK directly.
- You need first-class Python extension loading with no TS adapter. Then propose or maintain a Pi fork/upstream patch.
- You primarily need standardized cross-agent tool interoperability. Then build an MCP adapter, possibly using the same generated extension pattern.
