---
status: In co-design
created: '2026-05-17'
---

# v8 request — notes in progress

A working doc collecting what we want the next ChatGPT pass (v8) to address. This is **not** the prompt yet — it's the source material we'll distill into a prompt once the design is settled. Companion to `dev-notes/2026-05-17-v7-analysis.md` and `dev-notes/2026-05-17-v7-event-verification.md`.

## Headline ask: opt-in participation in 100% of pi's events

The big substantive piece. Author's direction (clarified 2026-05-17 after the v7 event-verification pass):

> The goal is to participate in **100% of pi's possible events**. Events that are expensive (round-trip required) are opt-in at pi-executable initialization time. By default, when we don't opt in, pi does the inexpensive thing and assumes approval (same as pi's behavior today when no TS extension is registered for an event). (Runtime opt-in via a command was discussed and deferred; see § Deferred below.)

### What this changes

v7's `Agent` class models only the *notification* surface — events that pi forwards over RPC stdout regardless of whether anyone is listening. The 19 in-process *participatory* events (`tool_call`, `tool_result`, `before_provider_request`, `context`, `session_before_*`, `user_bash`, `input`, `resources_discover`, `session_start`, `session_shutdown`, `session_compact`, `session_tree`, `model_select`, `thinking_level_select`, `after_provider_response`, `before_agent_start`) fire only to TS extensions registered in-process — they never cross the RPC boundary in v7.

v8 needs the TS shim to **conditionally subscribe** to these events on pi's behalf, and bridge them across to Python where a user-declared hook returns a result that flows back to pi.

### Why this is shaped as opt-in (not always-on)

1. **Cost.** Each round-trip is a bridge connection, JSON serialize/deserialize, Python dispatch, and pi waiting on the loop. For high-frequency events (`message_update` if it ever became a decision event; `before_provider_request` on a heavy session) the cost is real.
1. **Forward-compat.** Pi keeps adding events. If we always subscribed, every pi upgrade could change cost characteristics silently.
1. **Match pi's own model.** Pi extensions in TS opt into specific events via `pi.on(event, handler)`. Not opting in = no overhead. We're mirroring that.

### Architecture: subscribe-to-all + always-notify + gated-decide (revised twice)

The architecture evolved across two refinement passes:

1. *Original sketch:* shim subscribes only when user has a decision hook; opt-in = register with pi.
1. *First refinement (author):* shim subscribes to every event up front; opt-in flips an in-handler gate that controls whether to round-trip to Python.
1. *Second refinement (author):* gate state controls whether pi *waits* for our response, but Python is **always notified** about every decision-shaped event regardless of gate state. Observation flows always; decision flows only when opted in.

The final shape:

> **The shim subscribes to every decision-shaped event up front. Every fire produces a bridge call to Python. When the gate is closed, the call is fire-and-forget (`notify_event`, pi doesn't wait, shim returns `undefined` to pi). When the gate is open, the call is sync round-trip (`event`, pi waits, Python's `decide_X` hook returns the result pi consumes).**

Why this is the right shape:

1. **Full visibility regardless of opt-in.** Users get logging / monitoring / audit access to every event without paying the sync cost.
1. **Pi-only-blocks-when-asked-to.** Sync round-trip happens only when the user has actually declared a decision hook.
1. **One bridge call per fire.** Notification and decision use the same payload; the difference is whether pi waits.
1. **No runtime opt-in race.** Per the previous refinement — the handler was always subscribed, opt-in just flips a flag.

**Cost shape:**

| Cost                                      | Magnitude  | On pi's hot path? |
| ----------------------------------------- | ---------- | ----------------- |
| Sync round-trip for a decision            | ms–seconds | Yes (pi blocked)  |
| Fire-and-forget notification ship         | tens of μs | No (async)        |
| Event payload serialize (incl. `context`) | μs–low ms  | No                |
| Localhost pipe transfer                   | μs         | No                |
| Python parse + dispatch                   | μs         | No                |

Only the sync round-trip matters in practice. The fire-and-forget notification path is cheap enough to always pay; the visibility benefit is worth it.

**Forward-compat caveat:** the cost analysis holds for current pi event frequencies. Participation events are 1–10 per turn (`tool_call`, `before_provider_request`, `context`, `session_before_*`, `user_bash`, `input`, etc.). If pi ever adds a per-token participation event, the always-notify firehose would start to cost something measurable and we'd revisit. None exists today.

### Opt-in mechanism: init-time, method-existence-driven

v8 ships **one** opt-in mechanism: declaring a `decide_X` or `async_decide_X` method on the Agent subclass opens the gate for event `X` at extension load. The shim's gate set is set once via the manifest handshake and is **immutable** for the lifetime of the harness.

```python
class MyAgent(Agent):
    # Notification hook (existing v7 surface)
    def on_agent_start(self, event):
        log.info("starting")

    # Decision hook (v8 new, sync flavor) — declaring this opens the tool_call gate at startup
    def decide_tool_call(self, event) -> dict | None:
        if event["name"] == "bash" and "rm -rf" in str(event["params"]):
            return {"block": True, "reason": "forbidden command"}
        return None  # let pi proceed normally

    # OR the async flavor — only one of decide_X / async_decide_X per event
    async def async_decide_before_provider_request(self, event) -> dict | None:
        modified = await self._inject_context(event["payload"])
        return {"payload": modified} if modified else None
```

Mechanism:

1. `Agent.__init_subclass__` introspects which `decide_*` / `async_decide_*` methods are defined (non-None).
1. `Agent.start()` passes the list to the shim via the manifest response's `initialOpenGates` field.
1. The shim opens those gates once at extension load. After that, the gate set never changes.

The handler logic per event is:

```ts
pi.on("tool_call", async (event, ctx) => {
  if (openGates.has("tool_call")) {
    // Gate open: sync round-trip. Python runs both observation and decision hooks.
    return await bridgeCallForDecision("event", {event_name: "tool_call", event_data: event});
  } else {
    // Gate closed: fire-and-forget. Python runs observation hooks only; pi doesn't wait.
    fireAndForgetBridgeCall("notify_event", {event_name: "tool_call", event_data: event});
    return undefined;
  }
});
```

One bridge call per fire either way. Pi waits only when the gate is open.

### Deferred: runtime opt-in

An earlier draft also proposed runtime opt-in via `agent.enable_decision("tool_call")` / `agent.disable_decision("tool_call")` calls. **Deferred to a future pass.** Reasons to defer:

- Method-existence init-time opt-in covers the common case ("I want my Agent to gate `tool_call`").
- Runtime opt-in adds bridge protocol surface (`open_gate` / `close_gate` request types), per-call concurrency considerations (what if pi fires the event while a gate is being toggled?), and an additional Python-side API surface.
- We don't have a concrete use case yet for dynamically changing gate state mid-session. When we do (e.g., "enable permission gating only after auth completes"), we'll know what the API needs.
- Easy to add later without breaking the v8 contract: the shim grows two new request types, the manifest's `initialOpenGates` remains the source of truth at startup.

### Sketch of the bridge protocol changes

The current bridge has request types `manifest` and `execute`. v8 adds:

- **Manifest extension.** Manifest response carries `initialOpenGates: ["tool_call", "session_before_switch", ...]` listing events whose gates should be open at extension load.

- **`notify_event` request type** (shim → Python, fire-and-forget). When pi fires a decision-shaped event AND the gate is closed, the shim opens a bridge connection, sends the event, and does not wait for a response:

  ```json
  {"id": "...", "type": "notify_event", "event_name": "tool_call", "event_data": {...}, "token": "..."}
  ```

  Python's bridge server dispatches to `Agent._handle_notification_event(...)`, which routes to any `on_X` hook. No response expected by the shim; pi proceeds with its default behavior. (The shim should still consume the bridge response if one is sent so the connection closes cleanly — but doesn't *wait* for it before returning `undefined` to pi.)

- **`event` request type** (shim → Python, sync round-trip). When pi fires a decision-shaped event AND the gate is open:

  ```json
  {"id": "...", "type": "event", "event_name": "tool_call", "event_data": {...}, "token": "..."}
  ```

  Python's bridge server dispatches `_handle_decision_event(...)`. It runs the `on_X` notification hook first (observation), then the `decide_X` decision hook, and returns the decision result:

  ```json
  {"id": "...", "type": "response", "success": true, "data": {"block": true, "reason": "..."}}
  ```

(No `open_gate` / `close_gate` request types in v8 — runtime opt-in is deferred. See § Deferred above.)

**Protocol version: stays at `1` for v8.** v8 makes backwards-incompatible additions to the bridge (new request types, manifest field). Strictly that warrants a bump. But: there is no shipped binary, no third-party shim distribution, and no scenario where the Python side and TS shim could be out of sync — `write_bridge_shim()` regenerates the TS from `shim.py`'s embedded template on every extension load. Bumping pre-emptively for a forward-compat story that doesn't yet exist is ceremony. Keep the `MANIFEST_PROTOCOL_VERSION = 1` constant and the shim's `SUPPORTED_PROTOCOL_VERSION = 1` assertion as-is; bump when we actually have a release/distribution story to protect.

### Notification firehose: leave as-is (premature optimization)

Aside: pi forwards *all* `AgentSession` events to RPC stdout today (`rpc-mode.ts:346-348`). We could imagine telling pi "only send us notification event types X, Y, Z" to skip serializing and shipping events nobody hooked. **Verdict: premature optimization for now.**

- Volume is small (hundreds to low-thousands of events per session, not millions).
- Pi already pays the event-construction cost in agent-loop logic; the marginal cost of also writing to stdout is small.
- Our `Agent._async_on_event` dispatcher already silently drops unknown events at near-zero Python-side cost.
- Implementing pi-side subset-subscribe would need a new RPC protocol message and pi-side filter logic — non-trivial pi-side change.

When it would stop being premature: measured bottleneck under heavy concurrent-harness load, or a future "pi over real network" mode where bandwidth costs more, or pi adding a high-frequency event we don't care about. None apply today.

The asymmetry that justifies v8's design: **opt-in matters when pi waits (sync round-trip) but barely when it doesn't (async fire-and-forget).** v8 makes pi wait only when the user has explicitly opted in via a `decide_X` hook; otherwise events flow to Python fire-and-forget so observability is preserved without blocking pi. The same logic applies to the existing pi → RPC notification stream: filtering it server-side would save async cost only, which is in the noise.

### Open design questions (for v8 design conversation, before the request)

These all need a call from the author before we send the prompt to ChatGPT. None are obvious enough that I should pick.

1. **Hook naming convention.** *Resolved (author, 2026-05-17): `decide_X` plus `async_decide_X`, symmetric with v7's `on_X` / `async_on_X`.*

   Decision hook names are `decide_X` (sync method, dispatched via the hook executor) and `async_decide_X` (async method, awaited on the runtime loop thread). User defines at most one per event; both-defined is a `__init_subclass__` error, just like notification hooks.

   Both variants block pi (pi is waiting for the return value either way). The choice is style: sync users get the hook-executor thread and can do blocking I/O; async users get the loop thread and can `await` cleanly. Symmetry with notification hooks means users reach for whichever color matches their hook body without juggling two different mental models per event.

1. **Init-time opt-in trigger.** *Resolved (author, 2026-05-17): method existence (`decide_X` or `async_decide_X` non-None on the subclass) opens the gate.*

   `Agent.__init_subclass__` introspects the subclass for any `decide_*` / `async_decide_*` method that is non-None and adds the corresponding event name to the initial gate set. No `_DECIDE_EVENTS` class-level list; the methods *are* the declaration. This matches how `on_X` / `async_on_X` notification hooks declare themselves in v7.

   *Deferred to a future pass:* runtime opt-in via a function call (`agent.enable_decision("tool_call")`). See § Deferred above. Init-time-only is sufficient for v8.

1. **Result shape per event.** *Resolved (author, 2026-05-17): raw dict.*

   User returns whatever shape pi expects (e.g., `{"block": True, "reason": "..."}` for `tool_call`). Library does not validate or define per-event TypedDicts in v8. Matches the v7 `AgentEvent` raw-dict choice. TypedDicts / dataclasses-per-event are a quality-of-life follow-up if real usage hits surprises.

1. ~~**Runtime opt-in API surface.**~~ *Removed: deferred to a future pass per § Deferred. v8 ships init-time opt-in only.*

1. **Timeout / fallback.** *Resolved (author, 2026-05-17): no default timeout; provide an opt-in mechanism for subclass authors to set one.*

   By default, the shim waits indefinitely for the Python decision response. This is "fail closed" — a hung hook blocks pi visibly rather than silently letting a forbidden action through. For most use cases (permission gates, audit, context injection) this is the safer posture.

   Mechanism for subclass authors who want a timeout: a class-level configuration the shim reads via the manifest. Concrete shape TBD — likely either a single `_decision_timeout_ms: ClassVar[int | None] = None` or a per-event dict `_decision_timeouts: ClassVar[dict[str, int]] = {"tool_call": 5000}`. ChatGPT's call on which (or both). On timeout, the shim returns `undefined` to pi (treat as no opt-in for this fire) and logs a warning.

   Pi-side bug-fail-open vs hang-on-bug tradeoff is left to the user. We err on the side of "no surprise behavior changes from us."

1. **Cancellation.** *Resolved (author, 2026-05-17): cooperative `ctx.cancelled` analogue to the existing tool pattern.*

   Decision hooks receive a context object (or have access to `self`-attached state) that exposes a `cancelled` boolean. Set when pi closes the bridge socket mid-decision (e.g., pi's agent loop gets aborted while we're processing). The hook polls `ctx.cancelled` at convenient points; if set, the hook should return promptly (with whatever fallback the user prefers — `None` is the safe default). Mirror the existing v7 server-side `watch_disconnect` pattern that drives `ctx.cancelled` for tool execution.

1. **Notification vs decision overlap.** *Resolved (author, 2026-05-17): both allowed; notification fires first, decision fires next.*

   A subclass may declare BOTH `on_tool_call` (notification) AND `decide_tool_call` (decision) for the same event. They live on different prefixes so `__init_subclass__` doesn't flag a collision; only `on_X` vs `async_on_X` and `decide_X` vs `async_decide_X` are mutually exclusive within each prefix.

   When the gate is open and both hooks exist, Python's dispatcher inside `_handle_decision_event`:

   1. Runs the `on_X` / `async_on_X` notification hook first (observation; return value ignored).
   1. Runs the `decide_X` / `async_decide_X` decision hook next.
   1. Returns the decision result to the shim, which returns it to pi.

   When the gate is closed (no decision hook), only the notification hook runs via the fire-and-forget `notify_event` path.

   **Channel separation:** decision events arrive at Python via the bridge channel (shim → Python), NOT via pi's RPC stdout stream. So `client.on_event(handler)` subscribers do not see decision events — only the Agent class hooks do. This is by design (the bridge is a distinct channel from RPC); decision events are participatory by nature and the lower-level RPC event stream is for notification-only flow.

1. ~~**Symmetric async forms.**~~ *Resolved via Q1 — both `decide_X` and `async_decide_X` ship.*

## Smaller fixes that should fold into v8

Issues surfaced in `dev-notes/2026-05-17-v7-analysis.md` § Discussion items that we *could* handle ourselves during the v7→libharness port, but might be cleaner to bundle into v8:

| Item                                              | Bundle into v8?         | Notes                                            |
| ------------------------------------------------- | ----------------------- | ------------------------------------------------ |
| Add 7 session-layer events to `_EVENT_NAMES`      | Probably yes            | Logical extension; agent already has the pattern |
| Add `extension_error` as a hook                   | Yes if we bundle the 7  | Same change site                                 |
| Filter `extension_ui_request` from Agent dispatch | Probably yes            | One-line skip set                                |
| Named `MANIFEST_PROTOCOL_VERSION` constant        | Either; ours is trivial | We can do during port                            |
| `subscribe_client_events` thread-bounce simplify  | Probably skip in v8     | Internal refactor; can do during port            |
| `AgentEvent.payload` mutability docstring         | Either                  | One-line fix                                     |
| `protocolVersion` mismatch test                   | Probably yes            | Worth having; needs faux-mismatch scaffold       |
| Sync hook exception handling (try/except + log)   | Probably yes            | Affects both notification AND decision dispatch  |

**Detail:**

- *7 session events:* `queue_update`, `compaction_start`, `compaction_end`, `session_info_changed`, `thinking_level_changed`, `auto_retry_start`, `auto_retry_end` — all flow over RPC today. v8 spec for ChatGPT should include them; otherwise we're sending two separate revision requests for closely related work.
- *`extension_error`:* same. Notification surface event.
- *`extension_ui_request`:* `Agent` dispatcher should skip it (handled by `set_extension_ui_handler` instead). Trivial change but worth specifying in v8 so ChatGPT doesn't undo it during the participation-machinery rework.
- *Named constant:* tiny; we can do it ourselves during port. But specifying in v8 prevents drift if ChatGPT regenerates `tools.py`.
- *Thread-bounce simplify:* internal refactor of `subscribe_client_events`; doesn't touch the API. Skip in v8.
- *Payload mutability:* docstring-only; do during port.
- *Protocol-version test:* a fake-Python-server that responds with `protocolVersion: 99` and a test that pi (via shim load) fails. Useful regression. Should be in v8 since we're about to bump to v2.
- *Exception handling:* a raising hook should not kill the reader loop. v7 propagates exceptions from sync hooks via `run_in_executor` futures, which means an exception in `on_X` bubbles out of `_async_on_event`, out of `_dispatch_event`, and up to `_read_stdout_loop`, killing the reader. Wrap with try/except + log. Affects v8 decision-event dispatch too.

## Out of scope for v8

To keep v8 focused, exclude:

- Command bridge, state bridge, UI bridge (separate later passes).
- A from-scratch rewrite of any v7 module structurally.
- Replacing the TS shim with something other than the bridge pattern.
- Hardening / sandboxing.
- Multi-harness work beyond what v6's `HarnessRuntime` already supports.
- Pi reimplementation in Python (still no, just to be safe).

## Working list of artifacts v8 should produce

(Mirroring v7 deliverables; will refine before sending.)

- New file: maybe `src/pi_python_harness/decision.py` for the decision-event machinery, or fold into `agent_class.py`. ChatGPT's call.
- Updated `src/pi_python_harness/shim.py` with the new bridge request types (`event`, `subscribe`, `unsubscribe`) and the dynamic subscription bookkeeping.
- Updated `src/pi_python_harness/server.py` to handle the new request types on the Python side.
- Updated `src/pi_python_harness/agent_class.py` for the `decide_X` hook surface, the 7 added notification hooks, `extension_error` hook, and the `extension_ui_request` skip set.
- Updated `src/pi_python_harness/tools.py` for the named `MANIFEST_PROTOCOL_VERSION = 1` constant (no bump; the existing constant stays at 1).
- New tests: `tests/test_decision_hooks.py`, `tests/test_protocol_version.py`, `tests/test_extension_error_hook.py`.
- Updated docs: `AGENT_HOOKS.md` (decision-hook section), `IMPLEMENTATION_JOURNAL.md` (v8 pass).

## Open questions for the author (before drafting the v8 prompt)

1. Pick a default among the eight design questions above (especially #1 and #2 — naming convention and opt-in trigger).
1. Bundle approach: should v8 include the 7 missing session events + `extension_error`, or do we patch those ourselves during port and keep v8 focused on participation only?
1. Are there decision events the author *doesn't* want exposed even with opt-in? (E.g., `context` / `before_provider_request` would let Python rewrite messages mid-flight — that's powerful and easy to misuse. Worth confirming we want it all.)
1. Is "v8" the right cadence, or should we split — v8 = bundle of bug-fix-y items (the 7 events, the named constant, the test for `protocolVersion`, exception handling, `extension_ui_request` skip), v9 = the participation mechanism? Smaller revisions are usually easier for ChatGPT to do well.

## How this doc evolves

We add to it as we think of things. When we converge on answers to the open questions, the doc gets translated into a prompt-style document (like `dev-notes/2026-05-17-v7-request.md`) that we hand to ChatGPT. Until then, this is the scratchpad.
