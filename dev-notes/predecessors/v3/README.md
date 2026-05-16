# pi-python-harness

A Python-first integration prototype for using Pi as an agentic harness while keeping tool and environment customization in Python.

This package is an initial implementation of the recommended approach:

1. Python owns the control plane.
2. Python launches `pi --mode rpc`.
3. Python writes a tiny generated TypeScript extension shim.
4. Pi loads the shim with `--extension`.
5. The shim registers Pi tools from a JSON manifest.
6. Tool execution is forwarded over loopback JSONL to Python callables.

The TypeScript surface is deliberately minimized and generated. Python authors write tools, schemas, launch configuration, event handling, and higher-level orchestration.

## Status

Tested in this environment:

```text
7 passed in 0.32s
```

Not fully exercised against a real Pi process in this environment because the uploaded Pi source requires Node 20+ while the container provides Node 18.20.4. The package includes a fake-RPC subprocess test to validate the Python RPC client framing, request/response correlation, and event handling.

## Repository layout

```text
src/pi_python_harness/
  client.py       Async Python subprocess client for `pi --mode rpc`.
  bridge.py       Loopback JSONL server exposing Python tools to the TS shim.
  tools.py        Python tool registry, result/update dataclasses, manifest generation.
  shim.py         Generated minimal TypeScript extension source.
  harness.py      High-level composition of tool server, manifest, shim, and Pi RPC client.
  jsonl.py        Strict LF-only JSONL parser/writer matching Pi RPC framing.
  cli.py          Utility CLI for writing the shim.

docs/
  executive-summary.md
  assessment.md
  design-rpc-python-tool-bridge.md
  source-findings.md
  implementation-journal.md
  runbook.md

tests/
  test_jsonl.py
  test_rpc_client.py
  test_shim.py
  test_tools_and_bridge.py
```

## Minimal example

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
def echo(args: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult.text(f"Python received: {args['text']}", details={"tool_call_id": ctx.tool_call_id})

async def main() -> None:
    async with PythonPiHarness(
        registry,
        provider="anthropic",
        model="sonnet",
        no_session=True,
        extra_args=["--no-builtin-tools"],  # optional; depends on your desired environment boundary
    ) as pi:
        await pi.prompt("Call python_echo with text='hello'.")

asyncio.run(main())
```

## Installing for local development

```bash
python -m pip install -e .
pytest -q
```

A real Pi run also requires Node 20+ and an installed Pi CLI:

```bash
npm install -g @earendil-works/pi-coding-agent
```

## Design stance

This is not a Python port of Pi. It is a Python integration layer around Pi's existing RPC and extension surfaces. That preserves Pi's upstream harness behavior, provider support, session semantics, and tool-calling runtime while letting Python own most customization.

See `docs/assessment.md` and `docs/design-rpc-python-tool-bridge.md` for the full rationale.
