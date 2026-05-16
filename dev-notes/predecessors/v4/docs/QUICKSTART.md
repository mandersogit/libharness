# Quickstart

## Install locally for development

```bash
cd pi_python_harness
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

## Define Python tools

```python
from pi_python_harness import ToolRegistry, ToolResult

registry = ToolRegistry()

@registry.tool(prompt_snippet="Add two integers")
def add(a: int, b: int) -> ToolResult:
    return ToolResult.text(str(a + b), details={"sum": a + b})
```

## Launch Pi from Python

```python
import asyncio
from pi_python_harness import PiLaunchOptions, PiPythonHarness

async def main():
    launch = PiLaunchOptions(
        command=("pi",),
        model="anthropic/claude-sonnet-4-5",
        no_session=True,
        no_extensions=True,
    )

    async with PiPythonHarness(registry, launch_options=launch) as pi:
        pi.on_event(lambda event: print(event.get("type")))
        await pi.prompt("Use the add tool to compute 41 + 1")

asyncio.run(main())
```

## What the harness launches

The generated command is equivalent to:

```bash
pi --mode rpc --no-session --no-extensions --extension /tmp/pi-python-harness-*/python-tools.pi-extension.ts
```

The Python process also supplies:

```text
PI_PY_TOOL_MANIFEST=/tmp/.../python-tools.manifest.json
PI_PY_TOOL_BRIDGE_URL=http://127.0.0.1:<port>
PI_PY_TOOL_BRIDGE_TOKEN=<random per-run token>
```

## Run tests

```bash
cd pi_python_harness
PYTHONPATH=src pytest -q
```
