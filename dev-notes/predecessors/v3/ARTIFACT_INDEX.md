# Artifact index

## Core package

- `src/pi_python_harness/client.py` — async Python RPC client for `pi --mode rpc`.
- `src/pi_python_harness/bridge.py` — Python JSONL tool server consumed by the generated shim.
- `src/pi_python_harness/tools.py` — Python tool registry and result/update dataclasses.
- `src/pi_python_harness/shim.py` — generated TypeScript extension template.
- `src/pi_python_harness/harness.py` — high-level Python launcher tying Pi RPC and Python tools together.

## Documents

- `docs/executive-summary.md` — top-level recommendation and prototype status.
- `docs/assessment.md` — comparison of architectural options.
- `docs/design-rpc-python-tool-bridge.md` — detailed system design.
- `docs/source-findings.md` — evidence from the uploaded Pi source code.
- `docs/implementation-journal.md` — work log and validation notes.
- `docs/runbook.md` — how to run the prototype on a Node 20+ machine.

## Tests

- `tests/test_jsonl.py`
- `tests/test_rpc_client.py`
- `tests/test_shim.py`
- `tests/test_tools_and_bridge.py`

Local result: `7 passed in 0.32s`.
