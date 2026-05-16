# pi-python-harness

Python-first control plane and tool bridge for the Pi agent harness.

This is an initial implementation generated from an inspection of the uploaded Pi source tree and exercised against a real Pi RPC subprocess. It is intentionally small: Python owns orchestration and tool implementation; the only TypeScript is a generated adapter extension that registers Python tools with Pi.

## Contents

- `pi_python_harness.rpc.PiRpcClient`: async Python client for Pi's JSONL RPC mode.
- `pi_python_harness.tools.ToolRegistry`: decorator-based Python tool registry with JSON Schema inference from Python type hints.
- `pi_python_harness.shim.write_python_tool_shim`: emits a generic TypeScript extension that exposes Python tools to Pi through a persistent JSONL subprocess.
- `pi_python_harness.shim.write_faux_toolcall_provider_extension`: test-only provider extension for tool execution without an external LLM.
- `examples/echo_tools.py`: two Python tools served over JSONL.
- `examples/launch_with_tools.py`: minimal launch example.
- `tests/`: unit tests plus real Pi integration tests gated by `PI_CLI` or `PI_COMMAND`.
- `docs/`: assessment, architecture, journal, run results, and source notes.

## Minimal usage

Install Pi separately, then install this package in editable mode:

```bash
pip install -e .
```

Set `PI_CLI` when using Pi from a local npm install or source build:

```bash
export PI_CLI=/absolute/path/to/node_modules/@earendil-works/pi-coding-agent/dist/cli.js
```

Run the Python tool server directly:

```bash
python examples/echo_tools.py
```

Launch Pi in RPC mode with generated Python-tool extension:

```bash
python examples/launch_with_tools.py
```

Run unit tests:

```bash
python -m unittest discover -s tests -v
```

Run integration tests against a real Pi CLI:

```bash
PI_CLI=/absolute/path/to/dist/cli.js python -m unittest tests.test_rpc_real_pi -v
```

## Current limitations

- The bridge is an initial implementation, not a finished product.
- Cancellation is accepted by the generated TypeScript shim but not yet propagated into Python tool functions.
- Tool schemas cover common Python type hints; complex schemas should be supplied explicitly.
- Rich TUI renderers remain TypeScript-side Pi extension features.
- Tests use Pi's faux provider extension for deterministic tool execution; no external model was used.
