---
status: Closed (resolved by v8)
created: '2026-05-14'
---

# Event bridge proposal

> **Closed 2026-05-17 — resolved by the v8 port.** This proposal framed the design space for an event bridge between pi extensions and Python handlers. The v8 architecture answers the underlying question with its gated-decide protocol (subscribe-to-all + always-notify + per-event gate for sync round-trip; method existence opens the gate). The implemented protocol is documented at `docs/DESIGN.md` § Architecture and `docs/AGENT_HOOKS.md`; the design rationale is in `dev-notes/2026-05-17-v8-analysis.md`.
>
> Body preserved for audit trail.

**This is a proposal for discussion, not a design. No decisions inside.** The goal is to frame the design space well enough that the author can make decisions about scope, protocol shape, and priorities. Implementation is gated on author sign-off on §"Decision points" at the end.

## What pi gives us

Pi extensions subscribe to ~25 events through one overload-rich method:

```ts
on(event: "tool_call", handler: ExtensionHandler<ToolCallEvent, ToolCallEventResult>): void;
on(event: "agent_start", handler: ExtensionHandler<AgentStartEvent>): void;
// ... ~23 more
```

Full list lives in `links/pi/packages/coding-agent/src/core/extensions/types.ts:1089-1126`. The handler signature is:

```ts
type ExtensionHandler<E, R = undefined> = (event: E, ctx: ExtensionContext) => Promise<R | void> | R | void;
```

Two categories matter for the bridge:

### Notification events (return-type is void)

Pure observation. Handlers can read pi state and call `ctx.ui` / `ctx.triggerCompaction()` / `ctx.abortAgent()`, but the return value doesn't affect pi.

Examples: `agent_start`, `agent_end`, `turn_start`, `turn_end`, `message_start`, `message_update`, `tool_execution_start`, `tool_execution_update`, `tool_execution_end`, `model_select`, `thinking_level_select`, `after_provider_response`, `session_compact`, `session_shutdown`, `session_tree`.

Use cases: logging, metrics, observability, side-channel state tracking.

### Decision events (return-type is a Result interface)

The handler's return value flows back into pi and changes behavior.

| Event                     | Result type                      | What pi does with it           |
| ------------------------- | -------------------------------- | ------------------------------ |
| `tool_call`               | `{block?, reason?}`              | Block tool execution           |
| `tool_result`             | `{content?, details?, isError?}` | Replace tool result content    |
| `message_end`             | `{message?}`                     | Replace finalized message      |
| `before_agent_start`      | `{message?, systemPrompt?}`      | Inject system prompt (chained) |
| `before_provider_request` | `unknown`                        | Reserved; least documented     |
| `context`                 | `{messages?}`                    | Replace messages sent to model |
| `session_before_*`        | `{cancel?}`                      | Veto switch/fork/compact/tree  |
| `user_bash`               | `{operations?}`                  | Override bash executor         |
| `input`                   | `InputEventResult`               | Custom user-input routing      |
| `resources_discover`      | `ResourcesDiscoverResult`        | Modify discovered resources    |

**Detail:**

- *`tool_call`.* If the handler returns `block: true`, pi suppresses tool execution. Tool arguments can also be modified by mutating `event.input` in place (see "Subtleties" below — cross-process mutation is awkward).
- *`session_before_*`.* Four events (`session_before_switch`, `session_before_fork`, `session_before_compact`, `session_before_tree`), each with `{cancel?: boolean}`. Returning `cancel: true` vetoes the action.

Use cases: permission gates ("block bash with rm -rf"), path protection, context injection, system prompt override, custom compaction policy, audit logging that can cancel.

### Subtleties

- **In-place mutation.** `tool_call` is documented as: *"To modify arguments, mutate `event.input` in place instead."* Cross-process JSON serialization can't do in-place mutation; we'd need a returned-diff protocol if we want this capability.
- **Handler chaining.** Multiple extensions can subscribe to the same event. Pi's semantics vary per event (e.g., `before_agent_start` chains `systemPrompt`; `session_before_switch` ORs the `cancel` flags). If multiple Python handlers register, we have to decide whether to mirror pi's per-event semantics or simplify.
- **`ExtensionContext`.** The handler also receives a context object with `ui`, `cwd`, `hasUI`, `triggerCompaction()`, `abortAgent()`, and `sessionManager` (read-only). The UI side already round-trips via existing `extension_ui_request`/`extension_ui_response`; the method calls (`triggerCompaction`, `abortAgent`) would need new bridge frames if Python wants to invoke them.

## What we already have

Pi events are *partially* visible to Python today: `PiRpcClient` exposes events emitted on pi's stdout (per `rpc-mode.ts`). Those are the **RPC-mode emit events** — a narrower set than extension events, and one-way only (no result hooks). The proposal here is about the **extension** event API, which is the full set and includes the decision events.

So the harness already has *observation* of a subset, via RPC. What's missing is (a) the rest of the event surface, and (b) any ability to influence pi.

## Three protocol sketches

All three reuse the existing bridge transport (loopback TCP JSONL, token-protected) and the manifest handshake (`protocolVersion`). Differences are at the protocol-shape level.

### Approach A — notification only (one-way push)

Python declares an event subscription list in the manifest. The TS shim registers `pi.on(name, handler)` for each. On fire, the shim sends a one-way `notify` frame to Python; Python's handler runs; the return value is ignored.

```jsonl
// shim → python (fire-and-forget; no response expected)
{"type":"notify","event":"agent_start","payload":{...}}
```

Python:

```python
@harness.on("agent_start")
def log_start(event: AgentStartEvent) -> None:
    logger.info("agent started", session_id=event.session_id)
```

Pros:

- Simplest protocol; lowest risk.
- Zero latency on pi's hot path (fire-and-forget).
- Covers the entire "observability / logging / metrics" use case.

Cons:

- Doesn't unlock the interesting use cases (blocking, injection, replacement).
- Forwarding high-frequency events (`message_update`) is wasted bandwidth if nothing in Python consumes them — needs subscription filtering even in v1.

### Approach B — full two-way (round-trip per event)

Python handlers can return a result; the shim awaits the Python response and returns it to pi.

```jsonl
// shim → python
{"id":"e42","type":"deliver","event":"tool_call","payload":{...}}
// python → shim (always; required to unblock pi)
{"id":"e42","type":"response","success":true,"data":{"block":true,"reason":"policy: no rm -rf"}}
```

Python:

```python
@harness.on("tool_call")
async def gate_bash(event: ToolCallEvent) -> ToolCallEventResult | None:
    if event.name == "bash" and "rm -rf" in event.input.get("command", ""):
        return {"block": True, "reason": "policy: no rm -rf"}
    return None  # don't influence
```

Pros:

- Realizes the full extension API in Python.
- Symmetric with how tool execution already works.

Cons:

- Every subscribed event blocks pi until Python returns.
- Pi's hot path now includes a network round-trip per event.
- `tool_call`'s in-place input mutation needs special handling (return a diff and have the shim apply it).
- Handler-chaining semantics need per-event design.
- Misbehaving Python (slow / dead handler) stalls pi. We'd need per-event timeouts and a fail-open vs. fail-closed default.

### Approach C — hybrid (manifest declares per-event mode)

The manifest separates notifications from decisions. Each subscribed event is either `"notify"` (fire-and-forget) or `"deliver"` (round-trip).

```json
{
  "protocolVersion": 1,
  "tools": [...],
  "events": {
    "agent_start": "notify",
    "tool_execution_end": "notify",
    "tool_call": "deliver",
    "before_agent_start": "deliver"
  }
}
```

Pros:

- Subscribers pay round-trip cost only where they need it.
- High-frequency telemetry events stay cheap.
- The TS shim can pick the right `pi.on(...)` wiring per event
  (notify uses non-blocking, deliver uses awaited handler).

Cons:

- Two protocols to maintain.
- Subscribers can mis-declare (subscribe to a result-bearing event
  as "notify" and silently lose decision power, or vice versa).
- More documentation surface.

## Common protocol bits (regardless of approach)

- **Event subscription.** Manifest carries the list; bumping `protocolVersion` is mandatory.
- **Wire schema.** Event payloads are typed in TypeScript. We need Python dataclasses that mirror them, or pass dicts and document the shape. Starting with dicts is faster; codegen / hand-written dataclasses are an upgrade path.
- **Errors in Python handlers.** Notify-mode: log and continue. Deliver-mode: configurable per event — default could be "fail open with logged warning" (don't break pi if Python crashes), but certain events (e.g., tool_call gating) might want "fail closed".
- **Timeouts.** Deliver-mode events need a per-event budget. If Python doesn't answer in time: same fail-open vs. fail-closed question.
- **Multiple Python handlers.** Either (a) one handler per event (simple), (b) chain in registration order and merge results per-event (mirrors pi but complex), (c) one "primary" handler and multiple "observer" handlers (compromise).
- **ExtensionContext exposure.** Probably not in v1. Defer `ctx.triggerCompaction()` / `ctx.abortAgent()` / `ctx.ui` to a separate "state bridge" / "UI bridge" project (already on the roadmap in DESIGN.md). v1 sends only the event payload.

## Decision points

The author needs to weigh in on these before any implementation starts:

1. **Scope of v1**: A (notify only), B (full), or C (hybrid)? If C, does v1 implement both halves or stage them (notify in v1, deliver in v2)?
1. **Priority events**: which events do we wire first? My read of the review history (event bridge unlocks "permission gates", "path protection", "context injection") suggests `tool_call`, `before_agent_start`, and `context` are the highest-value decision events. Confirm or override.
1. **Handler-chaining semantics**: mirror pi (per-event chain semantics, expensive to get right) or simplify to one handler per event (cheap, may diverge from pi extensions' expected behavior)?
1. **Failure mode default**: when a Python handler crashes or times out on a decision event, does pi proceed with the un-modified action (fail-open) or block/cancel (fail-closed)? Either is defensible. Likely needs to be configurable per event.
1. **`tool_call.input` mutation**: skip in v1 (block-only), or support a returned-diff protocol from day one?
1. **Event payload typing**: dict-based with documented shapes (fast to ship), or Python dataclasses mirroring pi's TS types (more typing, more maintenance, but better DX and refactor safety)?
1. **ExtensionContext methods** (`triggerCompaction`, `abortAgent`): defer to a separate "state bridge" task, or fold a minimal version into the event bridge?
1. **Subscription model**: static (declared in the manifest at startup, registered once) or dynamic (Python can subscribe/unsubscribe at runtime via new bridge messages)? Static is the obvious starting point; dynamic costs a small protocol expansion.

## Suggested first conversation

If we want a single focused decision before opening any others, **decide #1 and #2 first** — they determine whether this is a one-week or one-month effort and what events even matter for the v1 wire. The rest can fall out of those.

## What this proposal does NOT contain

- A recommended approach. Stating one would prejudge the discussion; the trade-offs are real.
- Wire-format details for individual event payloads. That happens after #2 (priority events) lands.
- An implementation plan. That happens after #1 (scope of v1) lands.
- A schedule. Same.
