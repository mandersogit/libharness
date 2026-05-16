# Runbook

## Prerequisites

- Python 3.10+
- Node 20+
- Pi CLI installed:

```bash
npm install -g @earendil-works/pi-coding-agent
```

- An LLM provider configured for Pi, either through Pi login or environment variables.

## Install the Python package

From the artifact root:

```bash
python -m pip install -e .
pytest -q
```

## Write a Python-backed tool

Create `example_echo.py`:

```python
import asyncio
from pi_python_harness import PythonPiHarness, ToolContext, ToolRegistry, ToolResult

registry = ToolRegistry()

@registry.register(
    name="python_echo",
    label="Python Echo",
    description="Echo a string from Python.",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
    prompt_snippet="Echo text through the Python bridge",
    prompt_guidelines=["Use python_echo when the user asks to test Python tool wiring."],
    execution_mode="sequential",
)
def python_echo(args: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult.text(
        f"Python tool received: {args['text']}",
        details={"tool_call_id": ctx.tool_call_id},
    )

async def main() -> None:
    async with PythonPiHarness(
        registry,
        provider="anthropic",       # adjust for your Pi config
        model="sonnet",             # adjust for your Pi config
        no_session=True,
        keep_temp=True,              # helpful while inspecting generated shim/manifest
    ) as pi:
        pi.add_event_handler(lambda event: print("EVENT", event.get("type")))
        await pi.prompt("Use python_echo with the text 'hello from Pi'.")
        await pi.wait_for_event("turn_end", timeout=120)

asyncio.run(main())
```

Run:

```bash
python example_echo.py
```

## Inspect generated files

When `keep_temp=True`, `PythonPiHarness` does not delete its temporary directory during cleanup. You can inspect:

- `python-tools.json`
- `python-tool-bridge.ts`

The TypeScript file should be small and should not contain application-specific business logic. The manifest is the source of truth for tool names, schemas, and descriptions.

## Disable built-ins for a Python-owned environment

For a tightly controlled environment, consider:

```python
PythonPiHarness(
    registry,
    no_session=True,
    extra_args=["--no-builtin-tools"],
)
```

or selectively allow Pi built-ins depending on your workflow:

```python
PythonPiHarness(
    registry,
    extra_args=["--tools", "read,grep,find,ls"],
)
```

The right choice depends on whether Python tools or Pi's built-in file/bash tools should own the execution boundary.

## Add a streaming Python tool

```python
from collections.abc import Generator
from pi_python_harness import ToolContext, ToolRegistry, ToolResult, ToolUpdate

registry = ToolRegistry()

@registry.register(
    name="count_to_three",
    description="Stream counting updates from Python.",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
)
def count_to_three(args: dict, ctx: ToolContext) -> Generator[ToolUpdate | ToolResult, None, None]:
    yield ToolUpdate.text("one")
    yield ToolUpdate.text("two")
    yield ToolUpdate.text("three")
    yield ToolResult.text("done")
```

## Common failures

### `failed to start Pi RPC command: 'pi'`

The Pi CLI is not on `PATH`. Install it or pass an explicit command:

```python
PythonPiHarness(registry, pi_command=["node", "/path/to/pi/dist/cli.js"])
```

### Pi exits immediately with Node engine errors

Install Node 20+ and reinstall Pi.

### Tool not visible to Pi

Check:

- Was the generated shim passed with `--extension`?
- Does the manifest include the tool?
- Is there a name collision with another extension or built-in tool?
- Did Pi emit an `extension_error` event?

### Tool execution cannot connect to Python

Check:

- `PythonToolServer` started before Pi.
- `PI_PY_BRIDGE_HOST`, `PI_PY_BRIDGE_PORT`, and `PI_PY_BRIDGE_TOKEN` were passed to Pi.
- No local firewall blocks loopback.
- The Python process is still alive.

### Extension UI requests block

Register UI handlers on `PiRpcClient` if your extensions use dialogs:

```python
async def confirm_handler(request: dict) -> dict:
    return {"type": "extension_ui_response", "id": request["id"], "confirmed": True}

client.set_ui_handler("confirm", confirm_handler)
```

Default behavior cancels selection/input/editor requests and returns false for confirmation requests.

## Suggested production hardening

- Add explicit cancellation protocol.
- Add structured logging for every bridge request and Pi RPC command.
- Add per-tool timeout configuration.
- Add per-resource locks for mutating tools.
- Add a persistent generated-shim mode with content hashing.
- Run Pi in a sandbox/container for untrusted repositories.
- Add a live Pi integration test on CI with Node 20+ and a faux/deterministic model path if available.
