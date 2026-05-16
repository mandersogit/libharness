# py-pi-harness

A Python-first prototype for using Pi as an agentic harness while keeping customization and tool implementation in Python.

The package starts:

1. a local Python JSONL tool server,
2. Pi in `--mode rpc`, and
3. a minimal TypeScript extension shim loaded with `--extension`.

The shim registers Pi tools from a Python manifest. Tool calls cross a localhost JSONL bridge back into Python.

This repository is an initial implementation generated against the uploaded Pi source tree. It is intentionally small and conservative: no Pi fork, no generated TypeScript per tool, and no external Python runtime dependencies.

## Minimal use

```python
from py_pi_harness import PiPythonHarness, ToolRegistry

registry = ToolRegistry()

@registry.tool(description="Echo text back to the model")
def echo(text: str) -> str:
    return text

harness = PiPythonHarness(registry, pi_command=["pi"])
harness.start()
try:
    print(harness.client.get_state())
finally:
    harness.close()
```

For local testing against a built source checkout:

```python
harness = PiPythonHarness(
    registry,
    pi_command=["node", "/path/to/pi/packages/coding-agent/dist/cli.js"],
    cwd="/path/to/pi",
)
```

See `docs/` for architecture, protocol, and the implementation journal.
