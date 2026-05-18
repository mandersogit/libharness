# Agent Hooks: Notification and Decision Surfaces

`Agent` is a subclass of `PiAgentHarness`. It keeps the synchronous harness caller surface while
adding two hook families:

1. **Notification hooks** observe events. Their return values are ignored.
1. **Decision hooks** participate in Pi's in-process extension event surface. Their return values are
   handed back to Pi.

```python
from libharness.pi import Agent, AgentEvent, HookContext

class MyAgent(Agent):
    def on_agent_end(self, event: AgentEvent) -> None:
        print("run ended", event.type)

    def on_tool_call(self, event: AgentEvent) -> None:
        print("Pi is about to call", event["toolName"])

    def decide_tool_call(self, event: AgentEvent, ctx: HookContext) -> dict[str, object] | None:
        if event.get("toolName") == "bash" and "rm -rf" in event.get("input", {}).get("command", ""):
            return {"block": True, "reason": "refusing destructive bash command"}
        return None
```

## Threading model

```text
MainThread
  └─ application code; calls Agent.start(), prompt(), close(), etc.

PiAgentHarness-<id>
  └─ owner-thread-only harness state

PiAsyncioLoop-Thread-2
  ├─ Pi RPC stdout/stdin I/O
  ├─ Python bridge socket I/O
  ├─ async_on_* notification hooks
  └─ async_decide_* decision hooks

pi-hook_0
  ├─ on_* notification hooks
  └─ decide_* decision hooks

pi-tool_*
  └─ Python tools across all harnesses
```

Synchronous hooks run on `HarnessRuntime.hook_executor`, a dedicated
`ThreadPoolExecutor(max_workers=1)`. It is separate from the shared tool executor, so a slow tool
cannot starve event delivery. The single hook worker plus awaited dispatch preserves per-runtime hook
ordering without an additional event queue.

## Notification hooks

Notification hooks use the parallel two-method pattern:

- `async_on_<event>` runs on the runtime asyncio loop thread.
- `on_<event>` runs on the dedicated hook executor.

A subclass may define at most one notification color per event. If a subclass inherits one color and
wants to switch to the other, clear the inherited attribute explicitly:

```python
class A(Agent):
    async def async_on_agent_start(self, event: AgentEvent) -> None:
        ...

class B(A):
    async_on_agent_start = None

    def on_agent_start(self, event: AgentEvent) -> None:
        ...
```

`Agent.__init_subclass__` rejects unsupported hook suffixes and both-defined collisions.

### RPC notification event set

These are the named events delivered over Pi's RPC stdout stream. The ten core loop events come from
`packages/agent/src/types.ts`. The seven session-layer additions are declared in
`packages/coding-agent/src/core/agent-session.ts` lines 124-140. `extension_error` is emitted by
RPC mode when an extension handler throws, in `packages/coding-agent/src/modes/rpc/rpc-mode.ts` line
341\.

| Event                    | When it fires                                               |
| ------------------------ | ----------------------------------------------------------- |
| `agent_start`            | A low-level agent run begins.                               |
| `agent_end`              | The low-level agent run settles.                            |
| `turn_start`             | A provider-request turn starts.                             |
| `turn_end`               | A provider-request turn finishes, including tool results.   |
| `message_start`          | A user, assistant, or tool-result message starts.           |
| `message_update`         | An assistant message receives a streaming provider delta.   |
| `message_end`            | A user, assistant, or tool-result message finalizes.        |
| `tool_execution_start`   | A selected tool call begins execution.                      |
| `tool_execution_update`  | A tool emits a partial/progress result.                     |
| `tool_execution_end`     | A tool call finalizes with a result or error result.        |
| `queue_update`           | Steering/follow-up queues change.                           |
| `compaction_start`       | Context compaction starts.                                  |
| `compaction_end`         | Context compaction completes, aborts, or schedules a retry. |
| `session_info_changed`   | The active session name/info changes.                       |
| `thinking_level_changed` | The active reasoning/thinking level changes.                |
| `auto_retry_start`       | Pi starts an automatic retry after a recoverable error.     |
| `auto_retry_end`         | An automatic retry succeeds or exhausts.                    |
| `extension_error`        | A TypeScript extension handler throws inside Pi.            |

`extension_ui_request` is intentionally excluded. It is an RPC request that requires an
`extension_ui_response`, not a passive agent event. `Agent` silently skips it, even in strict mode;
use `PiRpcClient.set_extension_ui_handler()` for that abstraction.

## Decision hooks

Decision hooks use the symmetric method pattern:

- `async_decide_<event>` runs on the runtime asyncio loop thread.
- `decide_<event>` runs on the dedicated hook executor.

A subclass may define at most one decision color per event. The method's existence is the opt-in
trigger. At extension load, the Python manifest sends `initialOpenGates` to the TypeScript shim; the
shim opens exactly those gates for the lifetime of the run.

Decision hooks receive an `AgentEvent`. They may also accept a second positional `HookContext`:

```python
def decide_session_before_switch(self, event: AgentEvent, ctx: HookContext) -> dict[str, bool] | None:
    if ctx.cancelled:
        return None
    return {"cancel": True}
```

The second argument is optional for compatibility with simpler hooks. Prefer accepting it for hooks
that may take meaningful time.

### Gate model

The TypeScript shim subscribes to every Pi extension event in the decision set with `pi.on(...)`.
For each event fire:

```text
Pi in-process event
  └─ TS shim handler
       ├─ gate closed: notify_event -> Python, return undefined immediately to Pi
       └─ gate open:   event -> Python, wait for Python result, return result to Pi
```

The bridge sends exactly one call per fire.

- **Gate closed:** Python gets visibility through bridge observation hooks, but Pi proceeds as though
  no extension made a decision.
- **Gate open:** Python observes first, then decides. Pi blocks until Python returns, the hook raises,
  the bridge drops, or an opt-in timeout fires.

A subclass may define `on_<event>` / `async_on_<event>` for a decision event as an observation hook
and also define `decide_<event>` / `async_decide_<event>`. When both are present and the gate is open,
observation runs first and decision runs second for the same `AgentEvent` instance.

### Channel separation

Decision events arrive through the TypeScript bridge channel, not through Pi's RPC stdout stream.
Therefore:

- `PiRpcClient.on_event(handler)` subscribers do **not** receive decision events.
- `PiRpcClient.next_event()` does **not** return decision events.
- Only `Agent` bridge hooks (`on_X`, `async_on_X`, `decide_X`, `async_decide_X`) observe them.

This separation is intentional. RPC stdout events are passive notifications; bridge events are
participatory extension events that may block Pi.

### Decision event set and return shapes

The decision event set is the 29 `ExtensionAPI.on(...)` events in
`packages/coding-agent/src/core/extensions/types.ts` lines 1089-1126, minus the ten core agent-loop
notification events. Result type definitions are in the same file around lines 978-1041.

Return `None` to express no opinion. Otherwise return a raw `dict` or JSON-serializable value matching
Pi's expected TypeScript result shape.

| Event                     | Result shape (keys; `?` = optional)            | Example return                                                   |
| ------------------------- | ---------------------------------------------- | ---------------------------------------------------------------- |
| `resources_discover`      | `{skillPaths?, promptPaths?, themePaths?}`     | `{"skillPaths": ["/repo/.pi/skills"]}`                           |
| `session_start`           | No result consumed.                            | `None`                                                           |
| `session_before_switch`   | `{cancel?}`                                    | `{"cancel": True}`                                               |
| `session_before_fork`     | `{cancel?, skipConversationRestore?}`          | `{"cancel": False, "skipConversationRestore": True}`             |
| `session_before_compact`  | `{cancel?, compaction?}`                       | `{"cancel": True}`                                               |
| `session_compact`         | No result consumed.                            | `None`                                                           |
| `session_shutdown`        | No result consumed.                            | `None`                                                           |
| `session_before_tree`     | `{cancel?, summary?, ...}` — see Detail below. | `{"cancel": False, "label": "reviewed"}`                         |
| `session_tree`            | No result consumed.                            | `None`                                                           |
| `context`                 | `{messages?}`                                  | `{"messages": filtered_messages}`                                |
| `before_provider_request` | Replacement provider payload (any JSON value). | `{"messages": messages, "temperature": 0}`                       |
| `after_provider_response` | No result consumed.                            | `None`                                                           |
| `before_agent_start`      | `{message?, systemPrompt?}`                    | `{"systemPrompt": system_prompt + "\nExtra rule"}`               |
| `model_select`            | No result consumed.                            | `None`                                                           |
| `thinking_level_select`   | No result consumed.                            | `None`                                                           |
| `tool_call`               | `{block?, reason?}` — see Detail below.        | `{"block": True, "reason": "blocked by policy"}`                 |
| `tool_result`             | `{content?, details?, isError?}`               | `{"isError": False, "details": {"audited": True}}`               |
| `user_bash`               | `{operations?, result?}`                       | `{"result": {"stdout": "handled", "stderr": "", "exitCode": 0}}` |
| `input`                   | \`{action: continue                            | transform                                                        |

**Detail:**

- *`session_before_tree`:* full shape is `{cancel?, summary?, customInstructions?, replaceInstructions?, label?}`. `summary` is a precomputed text summary; `customInstructions` and `replaceInstructions` interact (the latter wholesale replaces; the former appends). `label` is shown in pi's session tree UI.
- *`tool_call`:* return shape is `{block?, reason?}`. Pi exposes a way to mutate `event.input` on the TypeScript side to modify tool args before dispatch, but the Python decision hook only returns the *result* shape (block/allow). Mutate args via separate code paths (a wrapper tool, or a `before_provider_request` rewrite) if needed.
- *`input`:* result is one of three forms. `{"action": "continue"}` keeps the user's input untouched; `{"action": "transform", "text": new_text, "images"?: [...]}` replaces the input; `{"action": "handled"}` swallows the input and skips the agent loop for it.

## Cancellation

Decision hooks use cooperative cancellation. `HookContext.cancelled` becomes `True` when the bridge
connection closes while Python is still deciding. This occurs when Pi aborts a loop or when a shim-side
timeout destroys the socket. Long-running decision hooks should poll the flag and return promptly.

Notification hooks do not receive a `HookContext`; they are fire-and-forget and do not block Pi.

## Timeout configuration

Default decision timeout is `None`: Pi waits indefinitely for a gate-open Python decision. This is the
safer default for permission/audit gates because a hung hook blocks visibly instead of silently allowing
action.

Subclass authors may opt in globally or per event:

```python
class TimedAgent(Agent):
    _decision_timeout_ms = 5000              # all open gates
    _decision_timeouts_ms = {"tool_call": 750}  # per-event override

    def decide_tool_call(self, event: AgentEvent, ctx: HookContext) -> dict[str, object] | None:
        ...
```

On timeout, the TypeScript shim destroys the bridge socket, logs a warning, returns `undefined` to Pi,
and Pi proceeds with its default behavior for that fire. The Python hook can observe cancellation via
`ctx.cancelled`.

## Exception handling

Every hook invocation is wrapped with `try`/`except` and logged with the event type and stack trace.

- Notification hook exceptions are swallowed after logging; later events continue to dispatch.
- Decision hook exceptions are logged, the bridge request fails, the shim logs and returns `undefined`
  to Pi, and Pi proceeds with its default behavior for that fire.

This prevents one faulty hook from killing the RPC stdout reader or hanging the bridge.

## Strict mode

`Agent._raise_on_unhandled_event` defaults to `False` for forward compatibility.

Set `_raise_on_unhandled_event = True` to raise `UnhandledEventError` when a known named event has no
handler or a bridge decision event is requested without a decision handler. `extension_ui_request` is
still skipped in strict mode because it is a request/response UI abstraction, not a hook event.

## Event payload shape

`AgentEvent` is a frozen wrapper around a shallow immutable copy of Pi's raw event dictionary.

- `event.type` returns the string event name.
- `event.payload` is a `MappingProxyType`; direct top-level mutation is rejected.
- Mapping access works: `event["message"]`, `event.get("toolName")`, iteration, and length.

Nested objects are not deep-copied. If Pi sends a nested list/dict, the wrapper preserves it.
