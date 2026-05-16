# Design: Python-first Pi harness using RPC and a TypeScript shim

## Goals

- Python is the only language used for user-authored tools and harness customization.
- TypeScript is limited to a static adapter that speaks Pi's native extension API.
- Pi remains unmodified and can be updated independently.
- The bridge supports structured tool schemas, streaming tool updates, tool errors, and local no-LLM testing.
- The Python library exposes a clean API over Pi's RPC mode.

## Non-goals

- Recreating Pi's SDK API in Python.
- Replacing Pi's provider/model registry.
- Supporting arbitrary custom TUI components in Python in the first version.
- Supporting distributed/remote tool execution before local process security is solved.

## Runtime topology

```text
Python application
  ├─ ToolRegistry
  ├─ PythonToolServer (localhost JSONL, token-authenticated)
  └─ PiRpcClient
       │ stdin/stdout JSONL
       ▼
Pi CLI --mode rpc
  └─ TypeScript extension shim
       ├─ bridgeCall("manifest") ───────────────▶ PythonToolServer
       ├─ pi.registerTool(...) for each manifest tool
       └─ bridgeCall("execute", ...) ───────────▶ PythonToolServer
```

## Startup sequence

1. Python creates a `ToolRegistry`.
2. Python starts `PythonToolServer` on `127.0.0.1` with an ephemeral port and random token.
3. Python launches Pi:

   ```bash
   pi --mode rpc --extension python_tools_extension.ts --no-extensions --no-session ...
   ```

   `--no-extensions` disables discovery, while the explicit `--extension` still loads the shim.

4. Environment variables convey bridge coordinates:

   - `PY_PI_TOOLS_HOST`
   - `PY_PI_TOOLS_PORT`
   - `PY_PI_TOOLS_TOKEN`

5. Pi loads the extension with `jiti`.
6. The extension factory calls `bridgeCall("manifest")`.
7. Python returns an array of tool specs.
8. The shim calls `pi.registerTool()` for each tool.
9. The Python caller uses `PiRpcClient` to prompt, steer, switch model, inspect state, and consume events.

## Tool definition flow

Python tool:

```python
@registry.tool(description="Add two integers")
def add(a: int, b: int) -> int:
    return a + b
```

Generated manifest entry:

```json
{
  "name": "add",
  "label": "add",
  "description": "Add two integers",
  "parameters": {
    "type": "object",
    "properties": {
      "a": { "type": "integer" },
      "b": { "type": "integer" }
    },
    "required": ["a", "b"],
    "additionalProperties": false
  }
}
```

Shim registration:

```typescript
pi.registerTool({
  name: spec.name,
  label: spec.label ?? spec.name,
  description: spec.description,
  parameters: Type.Unsafe(spec.parameters),
  async execute(toolCallId, params, signal, onUpdate) {
    const result = await bridgeCall("execute", { tool: spec.name, toolCallId, params }, onUpdate, signal);
    return normalizeAgentToolResult(result);
  }
});
```

## Bridge protocol

All frames are JSONL with LF delimiters.

### Manifest request

```json
{"id":"...","type":"manifest","token":"..."}
```

### Manifest response

```json
{"id":"...","type":"response","success":true,"data":{"tools":[...]}}
```

### Execute request

```json
{
  "id": "...",
  "type": "execute",
  "token": "...",
  "tool": "add",
  "toolCallId": "toolu_123",
  "params": { "a": 2, "b": 3 }
}
```

### Streaming update

```json
{"id":"...","type":"update","data":{"content":[{"type":"text","text":"Working..."}],"details":{}}}
```

### Execute response

```json
{"id":"...","type":"response","success":true,"data":{"content":[{"type":"text","text":"5"}],"details":5}}
```

### Error response

```json
{"id":"...","type":"response","success":false,"error":"...","data":{"traceback":"..."}}
```

The shim turns bridge errors into thrown tool errors so Pi records them as tool failures.

## Python API surface

### `ToolRegistry`

- `@registry.tool(...)` registers a callable.
- JSON Schema is inferred from type hints.
- Explicit schemas can override inference.
- Parameters named `ctx` or annotated as `ToolContext` are injected and excluded from the schema.

### `ToolContext`

- `ctx.tool_call_id`
- `await ctx.update(...)` for streaming partial tool output.

### `ToolResult`

- `ToolResult.text(...)`
- `ToolResult.image(...)`
- arbitrary structured `details`
- optional `terminate` hint.

### `PiRpcClient`

- `get_state()`
- `get_commands()`
- `get_available_models()`
- `set_model(provider, model_id)`
- `prompt(message)`
- `wait_event(predicate)`
- `drain_events()`
- `drain_stderr()`

### `PiPythonHarness`

Owns the Python tool server and Pi subprocess.

```python
harness = PiPythonHarness(
    registry,
    pi_command=["node", "/path/to/pi/packages/coding-agent/dist/cli.js"],
    cwd="/path/to/pi",
)
harness.start()
```

## No-LLM testing design

The shim optionally registers a fake provider named `py-pi-fake` with model `toolcaller`. When enabled, the fake provider:

1. emits a tool call for the configured Python tool on the first model turn;
2. receives the Pi-generated tool result in the next model context;
3. emits a final assistant message summarizing the result.

This exercises Pi's real agent loop, tool validation, extension-tool execution, tool-result insertion, and final answer path without calling an external model.

## Production hardening roadmap

1. Replace localhost TCP with Unix-domain sockets on POSIX where possible.
2. Add schema normalization for each target provider.
3. Add structured cancellation propagation into Python tool code.
4. Add event adapters that translate Pi agent events into Python dataclasses.
5. Add a persistent session manager wrapper with explicit session IDs and artifact export helpers.
6. Add a sidecar mode that uses Pi's SDK rather than CLI RPC when deeper lifecycle control is required.
7. Add upstream proposal for a generic extension bridge protocol.
