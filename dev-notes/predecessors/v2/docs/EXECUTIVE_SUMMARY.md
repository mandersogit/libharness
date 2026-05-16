# Executive summary

## Recommendation

Use the proposed **Python RPC host + generated TypeScript tool shim + persistent Python tool server** architecture, with one adjustment: treat it as a two-boundary system rather than only a tool shim.

1. **Python -> Pi:** Python starts Pi in RPC mode, sends prompts/control commands, receives events, and owns higher-level orchestration.
2. **Pi -> Python:** a generated, generic TypeScript extension registers tools with Pi and forwards each tool execution to a Python JSONL subprocess.

This is the best fit for a Python-first agentic harness because Pi's non-Node integration surface is RPC, while Pi's tool registration surface is the TypeScript extension API. The source tree does not expose a native RPC command for registering tools. Therefore, without forking Pi, a minimal TS adapter is the cleanest way to make Python-authored tools callable by the Pi agent loop.

## Why this is preferable

- Keeps business logic, tools, workflow policy, environment customization, and tests in Python.
- Avoids forking Pi or maintaining a parallel agent loop.
- Uses Pi's actual core: model/provider selection, session state, prompt expansion, tool registry, validation, event stream, and execution loop.
- Keeps TypeScript small, generated, and stable enough to vendor or regenerate.
- Provides deterministic integration tests without an LLM by using Pi's faux provider support.

## What was built

A small Python package under `pi_python_harness/`:

- `PiRpcClient`: async subprocess RPC client using LF-only JSONL framing.
- `ToolRegistry`: Python decorator API for tools.
- `ToolResult`: normalized Pi-compatible tool result wrapper.
- `serve_jsonl`: Python tool server protocol.
- `write_python_tool_shim`: emits the TypeScript extension that connects Pi to the Python tool server.
- `write_faux_toolcall_provider_extension`: emits a test provider that asks Pi to call a tool, then returns a final assistant message.

## What was tested

- Python unit tests for schema inference, tool execution, and JSONL protocol behavior.
- Real Pi RPC startup using `--mode rpc --no-session --offline --no-extensions`.
- Real Pi extension loading with a generated TypeScript shim.
- Real Pi tool execution through the generated shim into Python, using a faux model provider instead of an external LLM.

All shipped tests passed in the working environment when `PI_CLI` pointed at the installed Pi CLI package.

## Key caveats

- The uploaded source tree could not be built from the root because dependency installation attempted to fetch a package from an external CDN and failed with DNS resolution in the sandbox. I installed and ran the published `@earendil-works/pi-coding-agent@0.74.0` package separately, which matches the uploaded package version.
- The bridge is an initial implementation. It needs cancellation propagation, hardened subprocess lifecycle management, richer schema generation, and a packaging policy before production use.
- RPC mode intentionally does not provide every TUI feature. Rich custom rendering and complex editor UI still belong in TypeScript extensions unless Pi is modified.
