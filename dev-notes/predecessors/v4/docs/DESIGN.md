# Design: Python-first Pi harness with RPC and a generic TypeScript bridge

## Goals

1. Python is the authoring language for tools, environment setup, orchestration, state, and tests.
2. Pi remains the agent harness: model/provider selection, session persistence, tool-call loop, compaction, streaming, and RPC events.
3. TypeScript is limited to a generic shim that can be generated once and reused for every Python tool.
4. The bridge must not require additional Node packages beyond what Pi already loads.
5. The bridge should support progress updates and Pi's native `AgentToolResult` shape.

## Non-goals for the initial prototype

- Full forwarding of every Pi extension event into Python.
- Full forwarding of `ctx.ui` dialog APIs from Python tools.
- Hard cancellation of running Python code when Pi aborts a fetch request.
- Production-grade sandboxing.
- MCP compatibility.

These are compatible with the architecture, but they are not required for the first working library.

## Components

### 1. Python host

The Python host owns lifecycle:

1. build a `ToolRegistry`;
2. start `PythonToolBroker`;
3. generate `python-tools.manifest.json` and `python-tools.pi-extension.ts`;
4. launch Pi in RPC mode with the generated extension;
5. send prompts and receive events through `PiRpcClient`;
6. stop Pi and the broker.

### 2. Tool registry

Python tools are registered with decorators:

```python
registry = ToolRegistry()

@registry.tool(prompt_snippet="Add two integers")
def add(a: int, b: int) -> str:
    return str(a + b)
```

The registry infers a JSON Schema from type annotations or accepts an explicit schema:

```python
@registry.tool(
    name="search_docs",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
)
def search_docs(query: str) -> ToolResult:
    ...
```

### 3. Python tool broker

The broker is a token-protected HTTP server bound to `127.0.0.1`.

Endpoints:

- `GET /healthz` returns liveness.
- `GET /manifest` returns registered tool metadata when authorized.
- `POST /execute` invokes one Python tool and returns `application/x-ndjson`.
- `POST /session_shutdown` accepts a best-effort shutdown notification from the shim.

`POST /execute` request:

```json
{
  "toolName": "add",
  "toolCallId": "toolu_123",
  "arguments": {"a": 1, "b": 2},
  "cwd": "/repo",
  "context": {
    "hasUI": true,
    "model": {"provider": "anthropic", "id": "claude-sonnet-4-5", "name": "..."}
  }
}
```

Response stream:

```jsonl
{"type":"update","result":{"content":[{"type":"text","text":"Working..."}],"details":{"progress":50}}}
{"type":"result","result":{"content":[{"type":"text","text":"3"}],"details":{"a":1,"b":2,"sum":3}}}
```

Errors:

```jsonl
{"type":"error","message":"Invalid input"}
```

The TS shim converts an error record into a thrown exception. Pi then treats the tool execution as an error.

### 4. Generic TypeScript extension shim

The shim is generated but tool-agnostic. It:

1. reads `PI_PY_TOOL_MANIFEST`;
2. reads broker URL/token from `PI_PY_TOOL_BRIDGE_URL` and `PI_PY_TOOL_BRIDGE_TOKEN`;
3. calls `pi.registerTool(...)` for each manifest entry;
4. implements `execute(...)` by POSTing to Python;
5. parses the NDJSON response;
6. forwards update records to `onUpdate`;
7. returns the final Pi `AgentToolResult`.

No per-tool TypeScript is required.

### 5. Pi RPC client

`PiRpcClient` handles:

- subprocess startup;
- strict LF-only JSONL framing;
- request/response correlation by `id`;
- streamed event dispatch;
- extension UI responses;
- graceful termination.

It exposes convenience calls such as:

```python
await pi.prompt("Use add to compute 41 + 1")
state = await pi.get_state()
messages = await pi.get_messages()
```

## Startup sequence

```text
Python host
  ├─ registers Python tools
  ├─ starts PythonToolBroker on 127.0.0.1:<random>
  ├─ writes manifest + TS shim to temp/private directory
  └─ launches:
       pi --mode rpc --no-session --no-extensions --extension <shim.ts>

Pi
  ├─ loads only the CLI-specified shim
  ├─ shim reads manifest/env
  ├─ shim registers every Python tool with pi.registerTool
  └─ Pi enters RPC mode over stdin/stdout
```

## Tool-call sequence

```text
LLM emits tool call
  ↓
Pi validates arguments against JSON Schema
  ↓
Pi calls shim execute(toolCallId, params, signal, onUpdate, ctx)
  ↓
Shim POSTs to Python /execute with token
  ↓
Python broker invokes registry tool
  ↓
Python tool optionally emits update records
  ↓
Shim calls onUpdate(update)
  ↓
Python tool returns final ToolResult
  ↓
Shim returns final result to Pi
  ↓
Pi records toolResult and continues agent loop
```

## Security model

Minimum recommended defaults:

- Bind the broker to `127.0.0.1`, not `0.0.0.0`.
- Use a random per-run token in `X-Pi-Python-Token`.
- Generate the shim and manifest in a private temp directory.
- Launch Pi with `--no-extensions` so only the generated shim is loaded.
- Use `--tools` to explicitly allow only the tools needed for a task when running high-risk automations.
- Keep destructive operations in explicit Python tools with their own policy checks rather than exposing broad shell wrappers by default.

## Concurrency

Pi can execute tools concurrently. The Python broker uses `ThreadingHTTPServer`, so multiple tool calls can arrive at once.

Recommended policy:

- Pure read-only/stateless tools: allow parallel execution.
- Mutating tools: set `executionMode="sequential"` in the Python tool registration or implement resource-specific locks.
- File-mutating tools: use a per-path lock keyed by the resolved absolute path.

## Cancellations

The shim passes Pi's `AbortSignal` into `fetch`. If Pi aborts, the HTTP request can be aborted from the Node side. The initial Python broker does not forcibly stop a running Python function. Production options:

1. cooperative cancellation endpoint keyed by `toolCallId`;
2. per-tool subprocesses for tools that must be killable;
3. worker pool with cancellable tasks;
4. WebSocket transport that carries explicit cancel messages.

## Extension events beyond tools

The same shim pattern can be extended to forward Pi extension events into Python. A future manifest could include:

```json
{
  "eventSubscriptions": ["session_start", "tool_call", "tool_result", "before_agent_start"]
}
```

The shim would register `pi.on(event, ...)` and POST events to Python. Python could return event-specific responses for intercepting tool calls, injecting context, or custom compaction. This should be added deliberately because event semantics differ by event type.

## When to switch away from this architecture

Use the TypeScript SDK directly if the surrounding product is Node/TypeScript or if you need deep in-process access to `AgentSession` on every operation.

Patch/fork Pi only if you need native Python plugin registration with no TS shim and are willing to maintain upstream compatibility.
