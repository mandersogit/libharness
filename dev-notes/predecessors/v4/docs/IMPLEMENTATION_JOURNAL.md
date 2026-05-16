# Implementation journal

## 1. Repository inspection

I extracted the uploaded archive to `/mnt/data/pi-main` and inspected:

- root `README.md`, `package.json`;
- `packages/coding-agent/docs/rpc.md`;
- `packages/coding-agent/docs/extensions.md`;
- `packages/coding-agent/docs/sdk.md`;
- RPC implementation files under `packages/coding-agent/src/modes/rpc/`;
- extension API files under `packages/coding-agent/src/core/extensions/`;
- session/tool registry code in `packages/coding-agent/src/core/agent-session.ts`;
- JSON schema validation in `packages/ai/src/utils/validation.ts`.

Key finding: RPC mode is suitable for Python process control, but the RPC command set does not include dynamic tool registration. Tool registration is through extensions or the TS SDK.

## 2. Web research

I checked current public Pi documentation and ecosystem references. The official docs confirm:

- RPC mode is intended for embedding Pi into other applications and custom UIs.
- Extensions are TypeScript modules and can register custom tools.
- The SDK supports direct custom tools, but it is a Node/TypeScript integration surface.
- OpenClaw embeds Pi through the SDK rather than RPC because its integration is TypeScript-side.

These sources support the architectural split: RPC for Python host control, TS extension shim for custom tool registration.

## 3. Environment check

I attempted to prepare the uploaded Pi repository for execution:

```bash
cd /mnt/data/pi-main
node --version
npm --version
npm ci --ignore-scripts --prefer-offline --offline
```

Observed:

- Node version: `v18.20.4`.
- npm version: `9.2.0`.
- Pi packages require Node 20+; `@earendil-works/pi-coding-agent` requires `>=20.6.0`.
- Offline `npm ci` failed because required packages were not cached, ending with `ENOTCACHED`.

Conclusion: I could not meaningfully run the actual Pi software in this container.

## 4. Prototype implementation

I created `/mnt/data/pi_python_harness`.

Implemented files:

- `src/pi_python_harness/types.py`: result/context/launch dataclasses.
- `src/pi_python_harness/jsonl.py`: strict LF-only JSONL encoder/decoder.
- `src/pi_python_harness/tools.py`: Python tool registry, decorator, schema inference, invocation.
- `src/pi_python_harness/broker.py`: localhost token-protected HTTP/NDJSON broker.
- `src/pi_python_harness/shim.py`: generated generic TypeScript extension.
- `src/pi_python_harness/rpc.py`: async Pi RPC client.
- `src/pi_python_harness/harness.py`: high-level lifecycle owner.
- `examples/minimal_usage.py`: example using a Python-authored `add` tool.

## 5. Tests

Implemented tests:

- `tests/test_jsonl.py`: verifies LF-only JSONL decoding with U+2028 inside a JSON string.
- `tests/test_tools_and_broker.py`: verifies schema inference and broker execute/update/result flow.
- `tests/test_rpc_client.py`: verifies `PiRpcClient` against a fake Pi RPC subprocess.
- `tests/test_shim.py`: verifies manifest and shim generation.

Run:

```bash
cd /mnt/data/pi_python_harness
PYTHONPATH=src pytest -q
```

Result:

```text
5 passed in 1.49s
```

## 6. Known gaps

- Actual Pi execution was not tested here due environment constraints.
- Python tool cancellation is cooperative/design-level only in this prototype.
- Extension event forwarding into Python is not implemented, only tool execution.
- Python tool calls cannot directly use Pi `ctx.ui`; the current shim only forwards lightweight context fields.
- No packaging/publishing metadata beyond a minimal `pyproject.toml`.

## 7. Next implementation steps

1. Run the same tests in an environment with Node 20+ and an installed Pi binary.
2. Add an integration test that launches real `pi --mode rpc --extension generated.ts --no-session --no-extensions` with a faux/no-op provider if Pi supports it in the chosen version.
3. Add event forwarding for `session_start`, `tool_call`, and `tool_result`.
4. Add explicit cancellation support for long-running Python tools.
5. Add a policy/sandbox layer for destructive tools.
6. Convert this prototype into a real package if you want to iterate on it.
