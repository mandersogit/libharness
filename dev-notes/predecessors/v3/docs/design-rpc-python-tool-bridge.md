# Design: Python tool bridge for Pi RPC mode

## Goals

- Use Pi as the agentic coding harness.
- Author tools in Python.
- Author environment customization and launch policy in Python.
- Keep TypeScript to a generated adapter with no business logic.
- Preserve Pi's upstream behavior and avoid forking Pi internals.
- Provide a growth path for event hooks, commands, and session/state APIs.

## Non-goals for the initial implementation

- Reimplementing Pi in Python.
- Replacing Pi's model/provider stack.
- Replacing Pi's session manager.
- Supporting arbitrary Pi custom TUI component rendering from Python.
- Providing production-grade sandboxing by itself.

## High-level architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│ Python application / harness                                     │
│                                                                 │
│  ToolRegistry      policy/config      event handlers             │
│       │                 │                    │                   │
│       ▼                 ▼                    ▼                   │
│  manifest.json    PythonPiHarness      PiRpcClient               │
│       │                 │                    │                   │
│       │                 ├──── starts ────────┘                   │
│       │                 ▼                                        │
│  PythonToolServer on 127.0.0.1:<ephemeral>                       │
│       ▲                                                          │
└───────┼──────────────────────────────────────────────────────────┘
        │ JSONL TCP with run-scoped bearer token
        ▼
┌─────────────────────────────────────────────────────────────────┐
│ Pi process                                                       │
│                                                                 │
│  pi --mode rpc --extension /tmp/python-tool-bridge.ts            │
│       │                                                          │
│       ├── stdin/stdout JSONL RPC with Python PiRpcClient          │
│       │                                                          │
│       └── generated TypeScript extension                         │
│              ├── reads manifest.json                             │
│              ├── calls pi.registerTool(...)                      │
│              └── forwards execute(...) to PythonToolServer        │
└─────────────────────────────────────────────────────────────────┘
```

## Process lifecycle

1. Python code constructs a `ToolRegistry`.
2. Python decorators register tools with JSON Schema parameter definitions.
3. `PythonPiHarness.start()` creates a temporary directory.
4. It writes `python-tools.json` from `registry.manifest()`.
5. It writes `python-tool-bridge.ts` from a fixed template.
6. It starts `PythonToolServer` on `127.0.0.1` and an ephemeral port.
7. It generates a random bearer token.
8. It starts `pi --mode rpc --extension <shim>` with environment variables:
   - `PI_PY_TOOL_MANIFEST`
   - `PI_PY_BRIDGE_HOST`
   - `PI_PY_BRIDGE_PORT`
   - `PI_PY_BRIDGE_TOKEN`
9. Pi loads the shim.
10. The shim reads the manifest and registers tools.
11. Python sends prompts through `PiRpcClient`.
12. When the model calls a Python-backed tool, Pi invokes the shim's `execute()`.
13. The shim opens a loopback JSONL connection to Python.
14. Python executes the callable and streams updates/final result back.
15. The shim forwards updates via `onUpdate` and returns the final result to Pi.

## Python tool model

A Python tool is represented by:

```python
PythonTool(
    name="python_echo",
    label="Python Echo",
    description="Echo a string from Python.",
    parameters={... JSON Schema ...},
    handler=callable,
    prompt_snippet="...",
    prompt_guidelines=["..."],
    execution_mode="sequential" | "parallel" | None,
)
```

Handlers receive:

```python
def handler(args: dict, ctx: ToolContext) -> ToolResult:
    ...
```

Handlers may return:

- `ToolResult`
- `str`
- a JSON-like dict containing `content`
- an awaitable of any of the above
- a sync or async generator yielding `ToolUpdate`, `ToolResult`, strings, or JSON-like dicts

Generator semantics:

- `ToolUpdate` yields are progress updates.
- Strings in a generator are progress text updates.
- Dicts in a generator are progress updates if they contain `content` and/or `details`.
- The first yielded `ToolResult` is treated as the final result after generator completion.
- If no final result is yielded, the bridge returns `ToolResult.text("Done")`.

## Tool manifest

The manifest is intentionally plain JSON:

```json
{
  "protocolVersion": 1,
  "tools": [
    {
      "name": "python_echo",
      "label": "Python Echo",
      "description": "Echo a string from Python.",
      "parameters": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": false
      },
      "promptSnippet": "Echo text through the Python bridge",
      "promptGuidelines": ["Use python_echo when the user asks to test Python tool wiring."],
      "executionMode": "sequential"
    }
  ]
}
```

Pi's tool validation path in the uploaded source handles non-TypeBox JSON Schema objects, so the Python side does not need to generate TypeBox code.

## Bridge protocol

The Python tool server uses strict JSONL framing: one JSON object per LF-delimited line.

### Request: call a tool

```json
{
  "type": "call_tool",
  "id": "request-id",
  "token": "run-scoped-token",
  "toolName": "python_echo",
  "toolCallId": "pi-tool-call-id",
  "args": {"text": "hello"}
}
```

### Response: progress update

```json
{
  "type": "tool_update",
  "id": "request-id",
  "partialResult": {
    "content": [{"type": "text", "text": "working..."}],
    "details": {}
  }
}
```

### Response: final result

```json
{
  "type": "tool_result",
  "id": "request-id",
  "result": {
    "content": [{"type": "text", "text": "done"}],
    "details": {}
  }
}
```

### Response: error

```json
{
  "type": "tool_error",
  "id": "request-id",
  "error": "message",
  "traceback": "... optional Python traceback ..."
}
```

The generated shim throws when it receives `tool_error`. Pi then treats this like an extension/tool execution failure according to its normal behavior.

## Pi RPC client behavior

`PiRpcClient` starts a subprocess with arguments equivalent to:

```bash
pi --mode rpc [--provider ...] [--model ...] [--no-session] [--session-dir ...] --extension /tmp/python-tool-bridge.ts
```

It implements:

- request IDs for correlation
- strict LF JSONL parsing
- prompt, steer, follow-up, get-state, and get-messages commands
- asynchronous event queue
- event handlers
- extension UI request handling

Default UI behavior:

- `select`, `input`, and `editor`: cancelled
- `confirm`: false
- fire-and-forget methods: ignored

Applications can override UI handlers per method.

## Cancellation

Initial implementation:

- The shim listens for `AbortSignal`.
- On abort, it destroys the socket and rejects the Pi tool execution promise.
- Python sees connection closure but does not yet receive an explicit cancel request.

Recommended production improvement:

- Add request IDs stored by `PythonToolServer`.
- Add a `cancel_tool` bridge message.
- Execute each tool in a cancellable task.
- Propagate cancellation into Python via an `asyncio.Event` or cancellation token on `ToolContext`.

## Concurrency

Pi supports tool execution modes. For Python tools:

- Use `executionMode="sequential"` for tools that mutate files, external state, process-wide state, or shared resources.
- Use `executionMode="parallel"` or omit the mode only for safe read-only or isolated tools.
- For production, add per-resource locks in Python. File mutation tools should lock by normalized path or by project root.

## Security and isolation

The bridge is not a sandbox. It is an integration mechanism. Security boundaries should be explicit:

- Bind the bridge to loopback.
- Use a random run-scoped token.
- Do not expose the bridge port externally.
- Treat the generated shim as trusted code.
- Prefer deterministic generated shim contents.
- Load only the shim when operating under controlled policy.
- Consider disabling built-in tools or restricting Pi tool lists.
- Run Pi in a sandbox/container when untrusted prompts or repositories are involved.
- Keep API keys in the parent environment only as needed.

## Extension beyond tools

The same bridge can be expanded.

### Event bridge

Generated TS:

```ts
pi.on("tool_call", async (event, ctx) => {
  return await callPythonEventHandler("tool_call", { event, context: serializeContext(ctx) });
});
```

Python side:

```python
@events.on("tool_call")
async def guard_tool_call(event, ctx):
    if event["toolName"] == "bash" and "rm -rf" in event["input"].get("command", ""):
        return {"block": True, "reason": "blocked by Python policy"}
```

This would enable permission gates, path protection, context injection, logging, and pre/post-processing.

### Command bridge

Expose slash commands by having the shim register command names from a Python manifest and forward command invocations to Python.

### State bridge

Expose APIs such as:

- append entry
- send message
- send user message
- set active tools
- set model
- get commands
- reload

This requires careful serialization of Pi contexts and return-value contracts.

### UI bridge

RPC mode supports extension UI request/response for dialogs and basic widgets, but full TUI component factories are not available in RPC. Python-first integrations should use the RPC event stream to render their own UI rather than attempting to project rich Pi TUI components into Python.

## Failure modes

| Failure | Expected behavior | Mitigation |
|---|---|---|
| Pi process exits during startup | `PiRpcClient.start()` raises `RpcProcessError` with stderr | Validate Node/Pi installation before launch |
| Invalid manifest | Shim throws during extension load; Pi reports extension error | Validate manifest before launch |
| Python tool raises | Bridge sends `tool_error`; shim throws | Add app-level error policy and observability |
| Python bridge unavailable | Shim connection error; tool fails | Start bridge before Pi; health check with `list_tools` |
| RPC JSON parse problem | Client or Pi reports parse error | Use strict JSONL parser/writer |
| Tool cancellation | Socket destroyed; Python task may continue if not cancellation-aware | Add explicit cancel protocol |
| Concurrent mutation conflict | Race or file corruption | Use sequential execution and Python locks |

## Versioning

The manifest has `protocolVersion: 1`. Future versions should only add optional fields unless the generated shim and Python server are upgraded together.

Recommended version policy:

- Shim protocol version pinned to Python package version.
- Fail fast if manifest protocol is unsupported.
- Include generated shim hash in debug logs for reproducibility.

## Testing strategy

Current tests cover:

- strict JSONL parsing and writing
- Python tool registry manifest generation
- bridge tool execution
- generated shim contents
- Python RPC client behavior against a fake Pi process

Production tests to add:

- live Pi smoke test on Node 20+
- a real Python-backed tool call through Pi with a deterministic/faux model if available
- cancellation test
- concurrent tool call test
- extension UI round-trip test
- `--no-builtin-tools` / active tools policy test
- session persistence/resume smoke test
