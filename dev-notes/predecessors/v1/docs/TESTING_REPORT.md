# Testing Report

## Environment

- Uploaded Pi source: `/mnt/data/pi-main.zip`
- Extracted checkout: `/mnt/data/pi_src/pi-main`
- Prototype package: `/mnt/data/pi_python_harness`
- External LLM: not used

## Test 1: Python unit tests

Command:

```bash
cd /mnt/data/pi_python_harness
python3 -m pytest -q
```

Result:

```text
3 passed in 0.17s
```

Coverage:

- Python tool registration.
- Type-hint-based JSON Schema inference.
- Context-injected streaming updates.
- Python JSONL tool server manifest response.
- Python JSONL tool server execution response.

## Test 2: Pi RPC smoke test

Result: successful `get_state` response from Pi in `--mode rpc` with `--offline` and no session persistence.

Significance: confirms the uploaded Pi code can run meaningfully in this environment without an LLM call.

## Test 3: TypeScript shim + Python tools

Python registered tools:

```python
@registry.tool(description='Echo text')
def echo(text: str) -> str:
    return 'echo:' + text

@registry.tool(description='Add two integers')
def add(a: int, b: int) -> int:
    return a + b
```

Observed through Pi RPC:

```text
state gemini-2.5-pro
commands ['py-tools', 'py-tool']
notify Python tools: echo, add
notify2 echo:hi
stderr []
```

Significance: confirms the shim loaded successfully, fetched the Python manifest, registered commands/tools, and executed a Python tool from a Pi command handler.

## Test 4: No-LLM fake provider through Pi's actual tool loop

Python registered tool:

```python
@registry.tool(description='Echo text')
def echo(text: str) -> str:
    return 'echo:' + text
```

Fake provider configuration:

```python
PiPythonHarness(
    registry,
    fake_provider=True,
    fake_tool_name='echo',
    fake_tool_args={'text': 'sample'},
)
```

Observed:

```text
fake models [{'id': 'toolcaller', 'name': 'Python Tool Caller (Fake)', ...}]
set toolcaller
state model py-pi-fake toolcaller
...
agent_end
```

Final assistant text:

```text
Fake provider observed Python tool result for echo:
echo:sample
```

Significance: confirms Pi's agent loop actually invoked a Python-authored tool through the shim and consumed the result in a subsequent assistant turn.

## Remaining tests for production

- Run against actual providers: Anthropic, OpenAI, Google/Gemini, and any intended local providers.
- Validate schemas with provider-specific edge cases: enums, nullable fields, arrays of objects, unions, and deeply nested structures.
- Stress-test concurrent tool calls in Pi's parallel tool execution mode.
- Test cancellation while Python tools are running.
- Test long-running streaming updates.
- Test persistent sessions and session resume/fork behavior.
- Test packaging paths when Pi is installed globally rather than run from source.

## Test 5: Optional integration tests as pytest

Command:

```bash
cd /mnt/data/pi_python_harness
PI_CLI='node /mnt/data/pi_src/pi-main/packages/coding-agent/dist/cli.js' \
PI_CWD='/mnt/data/pi_src/pi-main' \
PYTHONPATH=src python3 -m pytest tests/test_pi_integration_manual.py -q
```

Result:

```text
2 passed in 2.95s
```

Coverage:

- Pi loads the packaged TypeScript shim.
- Python tools are registered as Pi extension tools.
- `/py-tool` executes a Python tool through Pi RPC.
- The fake provider drives Pi's agent loop through Python tool execution and final assistant response.
