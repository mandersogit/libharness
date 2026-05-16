# Executive summary

## Recommendation

Use **Pi in RPC mode, launched and supervised from Python**, plus a **single generic TypeScript extension shim** that registers Python-authored tools with Pi and forwards tool execution to a Python tool broker.

This is the best fit for a Python-first harness because:

1. Pi already exposes a cross-language integration surface: JSONL RPC over stdin/stdout.
2. Pi's tool registration surface is the extension API, and extensions are TypeScript modules. A thin TS shim is therefore unavoidable for native Pi tool registration unless Pi itself is patched.
3. The shim can be static and generic. Tool definitions, schemas, execution, state, and environment customization can all live in Python.
4. Python remains the process owner: it launches Pi, starts/stops the tool broker, controls environment variables, handles RPC events, and can own higher-level orchestration.

## What I built

The artifact contains an initial Python package, `pi-python-harness`, with:

- `PiRpcClient`: async Python client for Pi's JSONL RPC protocol.
- `ToolRegistry`: Python decorator-based tool definition and JSON Schema inference.
- `PythonToolBroker`: token-protected localhost HTTP/NDJSON bridge for tool execution and progress updates.
- `write_generated_shim`: generator for the generic TypeScript Pi extension shim.
- `PiPythonHarness`: high-level lifecycle owner for broker, generated shim, and Pi RPC subprocess.
- Tests for JSONL framing, tool schema inference, broker execution, RPC behavior against a fake Pi process, and generated shim creation.

## What was actually tested here

The Python implementation was tested locally with:

```bash
cd /mnt/data/pi_python_harness
PYTHONPATH=src pytest -q
```

Result: `5 passed in 1.49s`.

The real Pi software was not runnable in this container. The uploaded Pi repository requires Node 20+ (`packages/coding-agent/package.json` declares `node >=20.6.0`, and the repo root declares `node >=20.0.0`), while this environment has Node 18.20.4. An offline `npm ci` attempt also failed because dependencies were not cached. The implementation was therefore validated against the protocol and source code, plus a fake Pi RPC process.

## Main caveat

This approach gives Python ownership of tools and orchestration, but it does not eliminate TypeScript entirely. Pi's current native extension system is TypeScript, so a generic generated TS extension is the narrowest stable bridge into Pi's tool registry.
