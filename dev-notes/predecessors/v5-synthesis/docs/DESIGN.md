# Synthesized Design

## Architecture

```text
Python application
  ├─ ToolRegistry / Python tools
  ├─ PythonToolServer on 127.0.0.1:<ephemeral> with random token
  └─ PiRpcClient
        └─ pi --mode rpc --offline --no-session --no-extensions ... --extension python_tools_extension.ts
                └─ TypeScript bridge extension
                       ├─ fetches manifest from PythonToolServer
                       ├─ registers each Python tool with pi.registerTool()
                       └─ dispatches tool execution back to PythonToolServer
```

The TypeScript bridge is generic. It does not contain user tool logic.

## Runtime sequence

1. Python starts `PythonToolServer`.
2. Python writes the bridge TypeScript extension into a temporary directory or a caller-specified path.
3. Python starts Pi in RPC mode with deterministic flags and explicit `--extension` paths.
4. Pi loads the bridge extension.
5. The bridge calls `manifest` on the Python server.
6. The bridge calls `pi.registerTool()` for each Python tool.
7. Python sends prompts or commands through RPC.
8. When Pi decides to call a tool, the bridge sends an `execute` frame to Python.
9. Python executes the tool, optionally streams updates, and returns a normalized result.
10. Pi incorporates the tool result into the agent loop.

## Bridge protocol

Transport: loopback TCP, one JSONL request per connection.

Common fields:

```json
{
  "id": "uuid-or-request-id",
  "type": "manifest|execute",
  "token": "random secret"
}
```

Manifest request:

```json
{"id":"m1","type":"manifest","token":"..."}
```

Manifest response:

```json
{
  "id": "m1",
  "type": "response",
  "success": true,
  "data": {"tools": [{"name":"add","description":"...","parameters":{}}]}
}
```

Execute request:

```json
{
  "id":"e1",
  "type":"execute",
  "token":"...",
  "tool":"add",
  "toolCallId":"pi-tool-call-id",
  "params":{"a":1,"b":2},
  "cwd":"/workspace",
  "context":{}
}
```

Execution may emit update frames before the final response:

```json
{"id":"e1","type":"update","data":{"content":[{"type":"text","text":"working"}],"details":{}}}
```

Final response:

```json
{"id":"e1","type":"response","success":true,"data":{"content":[{"type":"text","text":"3"}],"details":{}}}
```

## Headless extension UI policy

Pi extensions may request UI interactions. In RPC/headless mode the client defaults are:

- `confirm`: return `confirmed: false`.
- `input`, `select`, `editor`: return `cancelled: true`.
- fire-and-forget notifications/status updates: queue as events and do not respond.

Applications can install method-specific or fallback handlers.

## Test strategy

1. Unit-test JSONL framing and registry/schema inference.
2. Unit-test the Python tool server with real TCP sockets.
3. Test `PiRpcClient` with a fake RPC subprocess that emits extension UI requests.
4. Test against real Pi with a separate faux-provider extension that emits a tool call and then a final message.

## Security model

This harness assumes trusted Python code, trusted Pi extensions, and a trusted local machine. The random bridge token prevents accidental local clients from using the tool bridge but is not a sandbox. For untrusted workloads, add process isolation, filesystem controls, network egress controls, and resource limits.
