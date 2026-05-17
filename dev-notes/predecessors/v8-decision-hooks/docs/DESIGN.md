# Synthesized Thread-Owned Design

## Architecture

```text
Python application / MainThread
  ├─ creates HarnessRuntime
  ├─ creates PiAgentHarness or Agent proxies
  └─ remains available for application logic

HarnessRuntime
  ├─ AsyncioLoopThread: PiAsyncioLoop-Thread-2
  │    ├─ Pi RPC subprocess I/O
  │    ├─ Python bridge server I/O
  │    ├─ Agent async notification hooks
  │    └─ Agent async decision hooks
  ├─ shared ThreadPoolExecutor: pi-tool_*
  │    └─ executes tool calls from all harnesses
  └─ dedicated ThreadPoolExecutor: pi-hook_0
       ├─ executes synchronous notification hooks
       └─ executes synchronous decision hooks

PiAgentHarness owner thread, one per normal harness
  ├─ owns _PiAgentHarnessCore state
  ├─ owns that harness's ToolRegistry reference
  ├─ owns PythonToolServer and PiRpcClient references
  └─ submits async work to PiAsyncioLoop-Thread-2

Pi RPC subprocess
  └─ launched with --mode rpc and explicit --extension python_tools_extension.ts

TypeScript bridge extension
  ├─ fetches manifest from that harness's PythonToolServer
  ├─ opens initial decision gates from manifest.initialOpenGates
  ├─ subscribes to every in-process extension event with pi.on(...)
  ├─ registers each Python tool with pi.registerTool()
  ├─ sends notify_event for gate-closed extension events
  └─ sends event and waits for a Python result for gate-open extension events
```

The TypeScript bridge is generic. It contains no user tool logic and no per-agent policy logic.

## Runtime sequence

1. The application creates a `HarnessRuntime`.
2. The runtime starts a dedicated asyncio loop thread lazily, normally before the first harness owner thread.
3. The application creates one or more `PiAgentHarness` or `Agent` instances.
4. In normal mode, each harness creates a dedicated owner thread after the loop thread exists.
5. `harness.start()` is marshalled onto the owner thread.
6. The owner thread freezes the harness's registry.
7. The owner thread starts the local `PythonToolServer` on the runtime loop thread.
8. The owner thread writes the generic TypeScript bridge extension.
9. The owner thread starts Pi in RPC mode on the runtime loop thread.
10. Pi loads the bridge extension.
11. The bridge calls `manifest` on that harness's Python server.
12. The bridge asserts `protocolVersion === 1`.
13. The bridge opens gates listed in `initialOpenGates` and installs timeout settings from `decisionTimeoutsMs`.
14. The bridge subscribes to all 19 in-process extension/decision events.
15. The bridge calls `pi.registerTool()` for each Python tool.
16. Prompts and commands are marshalled through the owner thread into `PiRpcClient`.
17. Tool execution requests enter the relevant harness's bridge server and run in the shared tool executor.
18. RPC stdout events reach lower-level `PiRpcClient` subscribers and, for `Agent`, the additive notification dispatcher.
19. Bridge decision events reach only `Agent` bridge hooks, not lower-level RPC event subscribers.

## Bridge protocol

Transport: loopback TCP, one JSONL request per connection.

Common fields:

```json
{
  "id": "uuid-or-request-id",
  "type": "manifest|execute|notify_event|event",
  "token": "random secret"
}
```

Manifest response:

```json
{
  "id": "m1",
  "type": "response",
  "success": true,
  "data": {
    "protocolVersion": 1,
    "initialOpenGates": ["tool_call"],
    "decisionTimeoutsMs": {"tool_call": 5000},
    "tools": [{"name":"add","description":"...","parameters":{}}]
  }
}
```

Execute requests may emit update frames before the final response:

```json
{"id":"e1","type":"update","data":{"content":[{"type":"text","text":"working"}],"details":{}}}
{"id":"e1","type":"response","success":true,"data":{"content":[{"type":"text","text":"done"}],"details":{}}}
```

Gate-closed bridge notification:

```json
{"id":"n1","type":"notify_event","token":"...","event":"tool_call","data":{"type":"tool_call"}}
```

Gate-open bridge decision:

```json
{"id":"d1","type":"event","token":"...","event":"tool_call","data":{"type":"tool_call"}}
{"id":"d1","type":"response","success":true,"data":{"block":true,"reason":"blocked"}}
```

## Thread-affinity contract

- Public `PiAgentHarness` and `Agent` methods are synchronous and safe to call from the application thread.
- The actual harness core is not touched directly from the application thread in normal mode.
- The core raises if a stateful method is invoked from the wrong thread.
- `PiRpcClient` and `PythonToolServer` async methods run only on the runtime loop thread.
- Python tool functions run in `HarnessRuntime.tool_executor` when using `PiAgentHarness` or `Agent`.
- Synchronous hooks run in `HarnessRuntime.hook_executor`, not in the tool executor.
- Registries are frozen when the harness starts, making cross-thread reads predictable.

## Agent API decisions

`Agent` uses inheritance for v1: `class Agent(PiAgentHarness)`. This keeps the caller surface to one
object and preserves the synchronous harness methods. Composition would be more flexible for one
object driving multiple harnesses, but that is not needed for this pass and would add wiring overhead
for the common case.

Hook payloads use `AgentEvent`, a frozen wrapper around a shallow immutable copy of the raw Pi event
dictionary. I did not define one dataclass per event type because Pi's taxonomy is still fluid and
per-event dataclasses would create a larger compatibility surface.

Decision cancellation uses a second positional `HookContext` argument. This mirrors tool-side
cooperative cancellation without forcing every hook to accept a context. A decision hook may use
`def decide_tool_call(self, event)` for simple cases or `def decide_tool_call(self, event, ctx)` when
it needs cancellation.

Timeout configuration uses both a global `_decision_timeout_ms` and a per-event
`_decision_timeouts_ms` dictionary. The per-event dictionary is the more precise mechanism and wins
when both are supplied. Default remains no decision timeout.

Module organization keeps event machinery in `agent_class.py`. Splitting into a separate `decision.py`
would reduce file size but introduce extra indirection across a small set of tightly coupled classes.

## Pushback / resolved inconsistency

The requested model requires gate-closed decision events to remain observable and also allows
`on_X` plus `decide_X` for the same bridge event. That means `Agent.__init_subclass__` must accept
`on_<decision_event>` and `async_on_<decision_event>` methods. The implementation therefore validates
observation hooks for both the 18 RPC notification events and the 19 bridge decision events, while the
base class still explicitly declares the RPC notification attributes and all decision attributes.
This is the only way to preserve both always-notify semantics and same-event observe-then-decide.

## Compatibility

`PiPythonHarness` remains in the package as the older async-first API. It is useful for direct asyncio
experiments, but `PiAgentHarness`/`Agent` are the recommended application embedding surfaces.

See `docs/AGENT_HOOKS.md` for the complete event tables and return-shape guidance.
