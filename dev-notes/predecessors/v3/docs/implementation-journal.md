# Implementation journal

## 1. Source intake

I extracted the uploaded `pi-main(2).zip` archive and inspected the repository structure. The root identified itself as a Pi Agent Harness mono repo containing the coding agent CLI, agent core, AI package, TUI package, web UI package, examples, and documentation.

Notable package facts:

- `packages/coding-agent/package.json`: package name `@earendil-works/pi-coding-agent`, version `0.74.0`, CLI binary `pi`, license MIT, Node engine `>=20.6.0`.
- root `package.json`: monorepo with package workspaces and Node engine `>=20.0.0`.
- `LICENSE`: MIT.

## 2. Public documentation check

I checked Pi's public documentation and current repository context.

Findings:

- Pi is presented as a minimal terminal coding harness extended through TypeScript extensions, skills, prompt templates, themes, and packages.
- The current package scope is `@earendil-works`, and Pi moved under Earendil Works in May 2026.
- RPC mode is officially documented for headless embedding over JSON stdin/stdout.
- Extensions are officially documented as TypeScript modules that can register custom tools, intercept events, add commands, and provide UI.
- The SDK is recommended for Node/TypeScript applications, while RPC mode is the natural boundary for non-Node hosts.

## 3. Source-level design review

I focused on these areas:

- `packages/coding-agent/docs/rpc.md`
- `packages/coding-agent/src/modes/rpc/rpc-mode.ts`
- `packages/coding-agent/src/modes/rpc/rpc-client.ts`
- `packages/coding-agent/docs/extensions.md`
- `packages/coding-agent/src/core/extensions/types.ts`
- `packages/coding-agent/src/core/extensions/loader.ts`
- `packages/coding-agent/src/core/extensions/runner.ts`
- `packages/ai/src/utils/validation.ts`
- `packages/agent/docs/agent-harness.md`
- `packages/agent/src/harness/agent-harness.ts`

Important conclusions:

- RPC mode is real and includes command/response/event framing, extension UI mediation, and session event streaming.
- Extensions are the right way to register custom tools.
- Tool definitions have an `execute(...)` function that is easy to bridge.
- Tool parameters can be plain JSON Schema, not only TypeBox.
- Direct low-level `AgentHarness` use is less attractive because its own documentation marks some lifecycle/settlement semantics as provisional.

## 4. Attempt to run/build Pi

Environment observed:

```text
node v18.20.4
npm 9.2.0
python 3.11.8
```

Pi source requires Node 20+. Attempted dependency installation produced engine warnings for packages requiring Node 20+ and did not complete successfully within the environment. I also checked available apt Node versions; the available package line was Node 18.x. Therefore, I did not claim a live Pi run.

## 5. Prototype implementation

I created `/mnt/data/pi-python-harness` with a Python package.

### Implemented modules

- `jsonl.py`
  - Strict LF-only JSONL parser/writer.
  - Accepts optional CR before LF.
  - Avoids generic line-reader behavior that would violate Pi RPC framing.

- `tools.py`
  - `ToolRegistry` with decorator API.
  - `PythonTool` dataclass and manifest generation.
  - `ToolResult`, `ToolUpdate`, and `ToolContext` dataclasses.
  - Normalization helpers for string/dict/awaitable/generator tool returns.

- `bridge.py`
  - Async TCP JSONL server on loopback.
  - Run-scoped bearer token.
  - `list_tools` and `call_tool` request handling.
  - Streaming progress updates and final results.
  - Error reporting with traceback.

- `shim.py`
  - Fixed generated TypeScript source.
  - Reads manifest and environment variables.
  - Registers tools with `pi.registerTool(...)`.
  - Forwards `execute(...)` calls to the Python bridge.
  - Forwards Python `tool_update` messages to Pi's `onUpdate`.
  - Returns Python `tool_result` to Pi.

- `client.py`
  - Async subprocess client for `pi --mode rpc`.
  - Request/response correlation.
  - Event queue and event handlers.
  - Prompt, steer, follow-up, state, and messages helpers.
  - Basic extension UI response handling.

- `harness.py`
  - High-level composition:
    - writes manifest
    - writes shim
    - starts tool server
    - starts Pi RPC subprocess with `--extension <shim>`

- `cli.py`
  - Utility command for writing the generated shim.

### Tests implemented

- `tests/test_jsonl.py`
  - Validates strict JSONL splitting and CRLF handling.

- `tests/test_tools_and_bridge.py`
  - Validates registry manifest generation.
  - Starts the Python tool server and calls a Python tool over JSONL.

- `tests/test_shim.py`
  - Validates that the generated TypeScript shim includes key Pi extension and bridge calls.

- `tests/test_rpc_client.py`
  - Uses a fake Python subprocess as a stand-in for Pi RPC mode.
  - Validates command sending, response correlation, and event handling.

## 6. Test result

Command:

```bash
cd /mnt/data/pi-python-harness
PYTHONPATH=src pytest -q
```

Result:

```text
7 passed in 0.32s
```

## 7. Known gaps

- No live Pi smoke test in this environment due Node 18 vs required Node 20+.
- Cancellation is not fully propagated into Python tool tasks.
- Tool bridge is implemented; event bridge is only designed.
- Command bridge is not implemented.
- Session/state mutation bridge is not implemented.
- No Unix-domain socket transport yet.
- No packaging mode for persistent project-local generated shim/manifest yet.
- No production sandboxing; callers must provide process/container isolation if needed.

## 8. Recommended immediate validation on a suitable machine

On a machine with Node 20+:

1. Install Pi: `npm install -g @earendil-works/pi-coding-agent`.
2. Install this Python package: `python -m pip install -e .`.
3. Register a trivial Python tool.
4. Launch `PythonPiHarness(..., no_session=True)`.
5. Prompt Pi to call the Python tool.
6. Confirm the tool call appears in Pi's event stream and returns the Python result.
