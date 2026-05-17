# pi-python-harness

A Python-first harness around the Pi agent harness, reshaped for application embedding with explicit thread ownership.

The current architecture is:

1. The application keeps `MainThread` for application code.
2. A shared `HarnessRuntime` owns one asyncio loop thread, named `PiAsyncioLoop-Thread-2` by default. It is started before normal harness owner threads.
3. Each normal `PiAgentHarness` owns its mutable harness state on its own owner thread.
4. Each harness has its own `ToolRegistry` and local bridge server.
5. Python tool calls from every harness are executed by one shared thread pool.
6. Pi still runs as an RPC subprocess and receives tools through a minimal generic TypeScript bridge extension.

The older async-first `PiPythonHarness` remains available for compatibility, but `PiAgentHarness` is the preferred application shape.

## Minimal threaded example

```python
from pi_python_harness import HarnessRuntime, PiAgentHarness, ToolRegistry, ToolResult

runtime = HarnessRuntime(max_tool_workers=8)
registry = ToolRegistry()

@registry.register(description="Add two integers")
def add(a: int, b: int) -> ToolResult:
    return ToolResult.text(str(a + b))

harness = PiAgentHarness(registry, runtime=runtime)
try:
    harness.start()              # runs harness state on its owner thread
    print(harness.snapshot())    # shows owner thread and asyncio loop thread
    state = harness.get_state()  # marshalled through the owner thread
    print(state)
finally:
    harness.close()
    runtime.close()
```


## Subclassable `Agent` hooks

`Agent` inherits from `PiAgentHarness` and adds named hooks while keeping the
synchronous caller surface. Async hooks run on the runtime asyncio loop thread; sync
hooks run on a dedicated single-worker hook executor owned by `HarnessRuntime`.

There are two hook families:

- `on_X` / `async_on_X`: observe events; return values are ignored.
- `decide_X` / `async_decide_X`: participate in Pi's in-process extension event
  surface; return values are handed back to Pi.

```python
from pi_python_harness import Agent, AgentEvent, HookContext, ToolRegistry, ToolResult

registry = ToolRegistry()

@registry.register(description="Add two integers")
def add(a: int, b: int) -> ToolResult:
    return ToolResult.text(str(a + b))

class MyAgent(Agent):
    def on_agent_end(self, event: AgentEvent) -> None:
        print(f"run ended: {event.type}")

    def on_tool_call(self, event: AgentEvent) -> None:
        print(f"about to call: {event.get('toolName')}")

    def decide_tool_call(self, event: AgentEvent, ctx: HookContext) -> dict[str, object] | None:
        command = event.get("input", {}).get("command", "")
        if event.get("toolName") == "bash" and "rm -rf" in command:
            return {"block": True, "reason": "destructive bash command blocked"}
        if ctx.cancelled:
            return None
        return None

agent = MyAgent(registry)
try:
    agent.start()
    agent.prompt_and_wait("use the add tool", timeout=120)
finally:
    agent.close()
```

See `docs/AGENT_HOOKS.md` for the notification event table, decision return shapes,
strict-mode behavior, channel separation, and the hook executor rationale.

## Multiple harnesses

```python
from pi_python_harness import HarnessRuntime, PiAgentHarness, ToolRegistry

runtime = HarnessRuntime(max_tool_workers=16)

h1 = PiAgentHarness(ToolRegistry(), runtime=runtime, owner_thread_name="PiAgentHarness-one")
h2 = PiAgentHarness(ToolRegistry(), runtime=runtime, owner_thread_name="PiAgentHarness-two")

try:
    h1.start()
    h2.start()
    assert h1.snapshot().async_loop_thread_id == h2.snapshot().async_loop_thread_id
    assert h1.snapshot().owner_thread_id != h2.snapshot().owner_thread_id
finally:
    h1.close()
    h2.close()
    runtime.close()
```

## Test-mode main-thread harness

For tests, create one harness with `threaded=False`. Its core is created on the current thread, including `MainThread`, while Pi RPC and bridge I/O still use the runtime asyncio loop thread.

```python
harness = PiAgentHarness(registry, runtime=runtime, threaded=False)
harness.start()
assert harness.snapshot().owner_thread_name == "MainThread"
```

## Launch defaults

By default the harness uses deterministic flags (`--offline`, `--no-session`, `--no-extensions`, `--no-skills`, `--no-prompt-templates`, `--no-context-files`) and then loads only the generated bridge extension. Pass a custom `PiLaunchConfig` for other modes.

Use `--no-builtin-tools` when you want only Python tools. Do not use `--no-tools` for this mode because it can disable extension/custom tools as well.

## Running tests

```bash
python -m pytest -q
```

Optional real-Pi integration remains available when `PI_CLI` points to a Pi CLI executable or JS entrypoint:

```bash
PI_CLI=/path/to/node_modules/@earendil-works/pi-coding-agent/dist/cli.js python -m pytest -q tests/test_real_pi_integration.py
```
