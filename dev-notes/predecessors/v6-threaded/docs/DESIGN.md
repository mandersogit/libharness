# Synthesized Thread-Owned Design

## Architecture

```text
Python application / MainThread
  ├─ creates HarnessRuntime
  ├─ creates PiAgentHarness proxies
  └─ remains available for application logic

HarnessRuntime
  ├─ AsyncioLoopThread: PiAsyncioLoop-Thread-2
  │    ├─ Pi RPC subprocess I/O
  │    └─ Python bridge server I/O
  └─ shared ThreadPoolExecutor: pi-tool_*
       └─ executes tool calls from all harnesses

PiAgentHarness owner thread, one per normal harness
  ├─ owns _PiAgentHarnessCore state
  ├─ owns that harness's ToolRegistry reference
  ├─ owns PythonToolServer and PiRpcClient references
  └─ submits async work to PiAsyncioLoop-Thread-2

Pi RPC subprocess
  └─ launched with --mode rpc and explicit --extension python_tools_extension.ts

TypeScript bridge extension
  ├─ fetches manifest from that harness's PythonToolServer
  ├─ registers each Python tool with pi.registerTool()
  └─ dispatches tool execution back to PythonToolServer
```

The TypeScript bridge is generic. It contains no user tool logic.

## Runtime sequence

1. The application creates a `HarnessRuntime`.
2. The runtime starts a dedicated asyncio loop thread lazily, normally before the first harness owner thread.
3. The application creates one or more `PiAgentHarness` instances.
4. In normal mode, each harness creates a dedicated owner thread after the loop thread exists.
5. `harness.start()` is marshalled onto the owner thread.
6. The owner thread freezes the harness's registry.
7. The owner thread starts the local `PythonToolServer` on the runtime loop thread.
8. The owner thread writes the generic TypeScript bridge extension.
9. The owner thread starts Pi in RPC mode on the runtime loop thread.
10. Pi loads the bridge extension.
11. The bridge calls `manifest` on that harness's Python server.
12. The bridge calls `pi.registerTool()` for each Python tool.
13. Prompts and commands are marshalled through the owner thread into `PiRpcClient`.
14. Tool execution requests enter the relevant harness's bridge server.
15. Tool calls run in the shared runtime thread pool.
16. Tool updates and final results are written back through the loop-thread bridge connection.

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

Manifest response:

```json
{
  "id": "m1",
  "type": "response",
  "success": true,
  "data": {"tools": [{"name":"add","description":"...","parameters":{}}]}
}
```

Execute requests may emit update frames before the final response:

```json
{"id":"e1","type":"update","data":{"content":[{"type":"text","text":"working"}],"details":{}}}
{"id":"e1","type":"response","success":true,"data":{"content":[{"type":"text","text":"done"}],"details":{}}}
```

## Thread-affinity contract

- Public `PiAgentHarness` methods are synchronous and safe to call from the application thread.
- The actual harness core is not touched directly from the application thread in normal mode.
- The core raises if a stateful method is invoked from the wrong thread.
- `PiRpcClient` and `PythonToolServer` async methods run only on the runtime loop thread.
- Tool functions run in `HarnessRuntime.tool_executor` when using `PiAgentHarness`.
- Registries are frozen when the harness starts, making cross-thread reads predictable.

## Compatibility

`PiPythonHarness` remains in the package as the older async-first API. It is useful for direct asyncio experiments, but `PiAgentHarness` is the recommended application embedding surface.
