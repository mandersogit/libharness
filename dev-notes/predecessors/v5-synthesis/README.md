# pi-python-harness

A Python-first harness around the Pi agent harness.

The architecture is intentionally narrow:

1. Python starts Pi in RPC mode.
2. Python starts a local, token-protected tool bridge.
3. A small TypeScript Pi extension registers Python tools with Pi.
4. Tool definitions, orchestration, process lifecycle, testing, and user extensions stay in Python.

This package is a synthesis of four independently generated implementations plus inspection of the Pi source and current public Pi documentation. It is an alpha reference implementation, not a published package.

## Minimal example

```python
import asyncio
from pi_python_harness import PiPythonHarness, ToolRegistry, ToolResult

registry = ToolRegistry()

@registry.register(description="Add two integers")
def add(a: int, b: int) -> ToolResult:
    return ToolResult.text(str(a + b))

async def main() -> None:
    async with PiPythonHarness(registry) as harness:
        state = await harness.pi.get_state()
        print(state)

asyncio.run(main())
```

By default the harness uses deterministic flags (`--offline`, `--no-session`, `--no-extensions`, `--no-skills`, `--no-prompt-templates`, `--no-context-files`) and then loads only the generated bridge extension. Pass a custom `PiLaunchConfig` for other modes.

## Real Pi integration test

Install Pi somewhere, then set `PI_CLI` to the CLI JS file or executable:

```bash
python -m pytest -q
PI_CLI=/path/to/node_modules/@earendil-works/pi-coding-agent/dist/cli.js python -m pytest -q tests/test_real_pi_integration.py
```

The real-Pi test uses a separate faux-provider extension so no LLM key is required.
