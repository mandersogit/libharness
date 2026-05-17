# Agent Hook Model

## Public shape

`Agent` is a subclass of `PiAgentHarness`:

```python
from pi_python_harness import Agent, AgentEvent

class MyAgent(Agent):
    async def async_on_agent_start(self, event: AgentEvent) -> None:
        ...

    def on_tool_execution_end(self, event: AgentEvent) -> None:
        ...
```

The user-facing object remains one object: subclass it, register tools on its registry, then call
`agent.start()`, `agent.prompt(...)`, `agent.prompt_and_wait(...)`, or the other synchronous harness
methods inherited from `PiAgentHarness`.

## Parallel two-method pattern

For every exposed event, the base class declares two class attributes:

- `async_on_<event_name>` for async hooks.
- `on_<event_name>` for sync hooks.

A subclass may define at most one of the two for a given event. `Agent.__init_subclass__` raises at
class construction if both are effectively defined. If a subclass inherits one color and wants the
other, it must explicitly clear the inherited hook:

```python
class A(Agent):
    async def async_on_agent_start(self, event: AgentEvent) -> None:
        ...

class B(A):
    async_on_agent_start = None

    def on_agent_start(self, event: AgentEvent) -> None:
        ...
```

`__init_subclass__` also rejects `async_on_*` or `on_*` attributes whose suffix is not one of the
exposed event names.

## Dispatch threads

Internal dispatch runs on `HarnessRuntime`'s asyncio loop thread. This preserves the previous
threading shape:

```text
MainThread
  └─ application code

PiAsyncioLoop-Thread-2
  ├─ Pi RPC subprocess I/O
  ├─ Python bridge socket I/O
  └─ Agent async hook dispatch

PiAgentHarness-<id>
  └─ owner-thread harness state

pi-tool_*
  └─ Python tools across all harnesses

pi-hook_0
  └─ synchronous Agent hooks across this HarnessRuntime
```

Async hooks run directly on the runtime loop thread. Sync hooks run through
`HarnessRuntime.hook_executor`, a dedicated `ThreadPoolExecutor(max_workers=1)`. The hook executor is
not the tool executor. A slow tool therefore cannot starve event delivery, and one hook worker plus
awaited per-event dispatch preserves strict per-agent event ordering without additional queues. A
sync hook's return value is discarded.

## Strict mode

`Agent._raise_on_unhandled_event` defaults to `False`. The default is deliberately forward-compatible:
Pi can emit session, RPC, extension-UI, or future agent events that this hook layer does not expose.

Set `_raise_on_unhandled_event = True` on a subclass to raise `UnhandledEventError` for a known named
event without a hook, or for an event type outside the named hook set.

## Event payload shape

The hook payload is `AgentEvent`, a frozen wrapper around Pi's raw event dictionary. This is not a
per-event typed dataclass taxonomy. The taxonomy is still fluid enough that typed dataclasses would
inflate the public surface and create a compatibility burden.

`AgentEvent` provides:

- `event.type` for the event name.
- `event.payload` for the raw dictionary.
- Mapping access such as `event["message"]` and `event.get("message")`.

Lower-level subscribers still receive the raw dictionaries through `PiRpcClient.on_event(handler)` and
`PiRpcClient.next_event()`.

## Exposed event set

The focused named hook set is the core agent-loop event union from Pi's `packages/agent/src/types.ts`
and its event-flow documentation. Session-level events, extension UI requests, and RPC response frames
are intentionally not named hooks in this pass.

| Event | When it fires |
|---|---|
| `agent_start` | The agent begins a prompt or continue run. |
| `agent_end` | The final low-level agent-loop event for the run; no later core loop events are expected for that run. |
| `turn_start` | A new turn begins; a turn is one provider response plus any tool calls/results. |
| `turn_end` | A turn completes with an assistant message and any tool-result messages. |
| `message_start` | A user, assistant, or tool-result message begins. |
| `message_update` | An assistant message receives a streaming provider delta. |
| `message_end` | A user, assistant, or tool-result message completes. |
| `tool_execution_start` | A tool call has been selected and tool execution is starting. |
| `tool_execution_update` | A tool emits a partial/progress result. |
| `tool_execution_end` | A tool call finishes with a final result or error result. |

Events outside this table remain reachable through the lower-level event stream.

## Lifecycle wiring

`Agent.start()` uses this sequence:

1. Call `PiAgentHarness.start()`.
2. The harness owner thread freezes the registry, starts the Python bridge server, writes the TS shim,
   and starts Pi in RPC mode on the runtime loop thread.
3. After the `PiRpcClient` exists and has started, `Agent` installs one additional event subscriber by
   calling `PiRpcClient.on_event(self._async_on_event)` through the harness owner thread.
4. Pi events continue to be queued for `next_event()` and delivered to user-registered lower-level
   `on_event()` subscribers; the `Agent` dispatcher is additive.
5. `Agent.close()` removes the dispatcher before closing the underlying `PiAgentHarness`.
