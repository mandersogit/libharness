---
status: Draft
created: '2026-05-17'
---

# v7 event-verification: what pi can emit vs. what `Agent` exposes

Verification of v7's `_EVENT_NAMES` against the actual event set pi can emit to a Python parent in RPC mode. Companion to `dev-notes/2026-05-17-v7-analysis.md` § Discussion item A.

## TL;DR

- **v7's 10 named hooks are all correct.** Every name in v7's `_EVENT_NAMES` exists in `links/pi/packages/agent/src/types.ts` lines 405-418 with matching emit sites. No misnamed events; no events that would never fire.
- **v7 covers `AgentEvent` completely.** The core agent-loop event union has exactly the 10 events v7 exposes — no missing core events.
- **v7 misses 7 *session-layer* events that pi DOES emit to our parent over RPC.** Compaction, auto-retry, queue-state, session-info, and reasoning-level events flow through the same stdout channel but aren't in v7's hook set. In non-strict mode they're silently dropped; in strict mode they trip `UnhandledEventError` on real sessions.
- **v7 also doesn't model 2 RPC-envelope events** (`extension_ui_request`, `extension_error`) that reach `_dispatch_event`. Recommended: filter `extension_ui_request` out of Agent dispatch (the existing `set_extension_ui_handler` mechanism is the right surface); add `extension_error` as an Agent hook.
- **19 extension-hook events stay in-process** in pi (TS-only). Not relevant to v7 directly, but see § Scope correction below — the long-term direction is opt-in participation in *all* pi events.

**Recommendation:** before porting v7 into `libharness.pi`, expand `_EVENT_NAMES` from 10 → 17 to cover the 7 session events. Optionally add `extension_error` as an 18th. Skip `extension_ui_request` (filter it from Agent dispatch). **Separately**, plan a v8 pass to add opt-in participation for the 19 currently-in-process events; see `dev-notes/2026-05-17-v8-request-notes.md`.

## Scope correction (post-author review, 2026-05-17)

An earlier draft of this document framed `Agent` as "notification-only by design," with participation in pi's decision events deferred to a future event-bridge co-design. **That was a scope call I made without consulting the author.** The author's stated direction is different:

> The goal is to participate in **100% of pi's possible events**. Events that are expensive (round-trip required) are opt-in at pi-executable initialization time. By default, when we don't opt in, pi does the inexpensive thing and assumes approval — same as pi's behavior today when no TS extension is registered for an event.

Under this direction:

1. **Notifications continue to "just work."** They already flow over RPC; no opt-in needed.
1. **Decision-event participation becomes opt-in.** For each of the 19 in-process events (e.g., `tool_call`, `before_provider_request`, `session_before_compact`), the Python user can declare a hook with a return-type contract. At pi-executable startup, the TS shim subscribes to those events on pi's side; pi fires them across the bridge to Python; Python returns a result that pi consumes. If the user *doesn't* declare a hook for an event, the shim never subscribes — pi's loop is unaffected, no round-trip cost, no change in behavior.

This reframes the "selectivity principle" below: pi's *three surfaces* are still factually three different things, but the long-term `Agent` API is meant to bridge all three, not just the notification surface. The selectivity in v7 is a property of v7's current implementation (which doesn't yet have the opt-in machinery), not a permanent design choice.

**What this means for the immediate v7 → libharness port:**

- The v7 port still happens with the 10 + 7 + 1 = 18 named notification hooks. No decision-event work in this pass.
- v8 adds the opt-in participation mechanism. The 19 in-process events become a second hook surface (`decide_on_X` / similar — name TBD) with return-type contracts per event. Notes for the v8 request are starting at `dev-notes/2026-05-17-v8-request-notes.md`.

## Sources (pi-mono, current checkout via `links/pi/`)

- Core agent-loop event union: `packages/agent/src/types.ts:403-418`.
- Session-layer events: `packages/coding-agent/src/core/agent-session.ts:124-140` (declarations) + emit sites scattered through that file.
- Extension hook events (in-process): `packages/coding-agent/src/extensions/types.ts:489-881`.
- RPC envelope frames: `packages/coding-agent/src/modes/rpc/rpc-types.ts:113-248`.
- RPC subscription wiring: `packages/coding-agent/src/modes/rpc/rpc-mode.ts:346-348` — the RPC mode subscribes to **all** `AgentSession` events and pipes them to stdout via `output(event)`. So everything `AgentSession` emits to its listeners reaches our parent.

## Full event taxonomy by routing

### Reaches our Python parent via RPC stdout (i.e. arrives in `PiRpcClient._handle_message`)

**Core `AgentEvent` (10 events — v7 covers all):**

| Event                   | Status in v7 | Source         |
| ----------------------- | ------------ | -------------- |
| `agent_start`           | ✓ named hook | `types.ts:405` |
| `agent_end`             | ✓ named hook | `types.ts:406` |
| `turn_start`            | ✓ named hook | `types.ts:408` |
| `turn_end`              | ✓ named hook | `types.ts:409` |
| `message_start`         | ✓ named hook | `types.ts:411` |
| `message_update`        | ✓ named hook | `types.ts:413` |
| `message_end`           | ✓ named hook | `types.ts:414` |
| `tool_execution_start`  | ✓ named hook | `types.ts:416` |
| `tool_execution_update` | ✓ named hook | `types.ts:417` |
| `tool_execution_end`    | ✓ named hook | `types.ts:418` |

**Session-layer events (7 events — v7 missing all):**

| Event                    | Status in v7  | Source                 |
| ------------------------ | ------------- | ---------------------- |
| `queue_update`           | ✗ NOT exposed | `agent-session.ts:124` |
| `compaction_start`       | ✗ NOT exposed | `agent-session.ts:128` |
| `compaction_end`         | ✗ NOT exposed | `agent-session.ts:132` |
| `session_info_changed`   | ✗ NOT exposed | `agent-session.ts:129` |
| `thinking_level_changed` | ✗ NOT exposed | `agent-session.ts:130` |
| `auto_retry_start`       | ✗ NOT exposed | `agent-session.ts:139` |
| `auto_retry_end`         | ✗ NOT exposed | `agent-session.ts:140` |

**RPC envelope events (3 — v7 doesn't model as hooks; 1 never reaches Agent dispatcher):**

| Event                  | Reaches `_dispatch_event`? | Status in v7  | Notes                                          |
| ---------------------- | -------------------------- | ------------- | ---------------------------------------------- |
| `response`             | No — caught earlier        | n/a           | Future-correlated in `_handle_message`         |
| `extension_ui_request` | Yes                        | ✗ NOT exposed | Also handled by `_handle_extension_ui_request` |
| `extension_error`      | Yes                        | ✗ NOT exposed | RPC-mode wrap of TS extension throws           |

### Does NOT reach our Python parent (in-process only)

**Extension hook events (19 events — all stay in pi's TS extension runner):**

`resources_discover`, `session_start`, `session_before_switch`, `session_before_fork`, `session_before_compact`, `session_compact`, `session_shutdown`, `session_before_tree`, `session_tree`, `context`, `before_provider_request`, `after_provider_response`, `before_agent_start`, `model_select`, `thinking_level_select`, `user_bash`, `input`, `tool_call`, `tool_result`

These are dispatched only via `AgentSession._emitExtensionEvent(...)` (`agent-session.ts:635-705`) to TS extensions registered in-process. They never cross stdout to RPC parents.

This is the key reason **v7 didn't expose `tool_call` / `tool_result`** despite my prompt mentioning them as examples: those names refer to TS-extension-side hooks, not RPC-reachable events. v7 correctly chose `tool_execution_*` (the agent-loop-level events that *do* flow over RPC).

## Why the missing events matter

v7's dispatcher silently drops unknown events in non-strict mode:

```python
async def _async_on_event(self, event):
    name = event.type
    if name not in self._EVENT_NAMES:
        if self._raise_on_unhandled_event:
            raise UnhandledEventError(...)
        return  # silent no-op
    ...
```

So in normal use the 7 missing session events arrive, get dropped by the Agent dispatcher, and are still reachable via the lower-level `client.on_event(handler)`. **Nothing breaks.** But:

1. **Strict mode is unusable as designed.** Set `_raise_on_unhandled_event = True` on any real session and the first `queue_update` or `compaction_start` raises. The forward-compat reasoning we used to default strict mode off (in `dev-notes/2026-05-17-v6-as-base-direction.md`) was about future pi events; turns out v7 already has 7 *current* known events outside its declared set. Strict mode is more broken than we realized.
1. **Users who want to react to compaction/retry have to drop to `client.on_event(handler)`.** That's a perfectly fine path for advanced users — but the whole point of the `Agent` class is to provide named hooks for the events users care about. Compaction outcome is a useful event to react to.
1. **The "named hook surface == 'what reaches us via RPC'" mental model is cleaner** than "named hook surface == 'subset of what reaches us, you pick'." Closing the gap aligns the abstraction.

## The three event surfaces (factual taxonomy)

> **Note (added in scope correction):** an earlier draft of this section called this "the selectivity principle" and concluded that `Agent` should only ever model surface (1). The author has since clarified the long-term direction is opt-in participation across all three surfaces — see § Scope correction above. The taxonomy below is still correct as a description of *what pi emits and how*; the conclusion that `Agent` permanently maps to surface (1) only is **not** the current direction.

What follows is the factual breakdown of pi's three event surfaces. v7 implements surface (1) exclusively; v8 (per `dev-notes/2026-05-17-v8-request-notes.md`) is intended to add opt-in surfaces (2) and (3).

### Pi's three event surfaces

**(1) Notifications.** Pi has done a thing and is telling whoever's listening. Pi does not wait for a response. Pi keeps going regardless of whether anyone subscribed, threw, or returned `False`. These flow through `AgentSession.subscribe(listener)` and over RPC stdout. Examples: `agent_start`, `turn_end`, `message_update`, `compaction_start`, `auto_retry_end`.

**(2) Participatory hooks.** Pi is *about to do* a thing and wants in-process TS extensions to optionally change what it does. Handlers return modified payloads, vetos, or `false`-to-block. Pi waits for the return synchronously before proceeding. These flow through `AgentSession._emitExtensionEvent(...)` to extensions registered via `pi.registerEventHandler` in TS. They do **not** cross the RPC boundary. Examples: `before_provider_request` (return a modified payload), `tool_call` (return `false` to block the call), `context` (return filtered messages), `session_before_switch` (return `cancelled: true` to veto).

**(3) Requests.** Pi wants an answer from the parent process — typically a UI dialog. Pi sends a request frame and waits for a matching response frame, correlated by id. These flow through RPC envelope events `extension_ui_request` (out) and `extension_ui_response` (in). Examples: `confirm`, `select`, `input`, `editor`.

`Agent.on_*` / `Agent.async_on_*` is a **notification API**. The hook returns nothing meaningful; pi keeps going whether the hook ran or threw. That semantic only matches surface (1). Stretching it to cover (2) or (3) would be either dishonest (we'd accept a return value pi never sees) or break the contract (pi blocks waiting for a hook that ran asynchronously).

So the design principle is:

> **`Agent` named hooks expose pi's notification surface. Other surfaces use other mechanisms.**

### Which mechanism for each surface (current state)

| Surface       | Mechanism in our harness (v7 today)                     | v8 direction                                                       |
| ------------- | ------------------------------------------------------- | ------------------------------------------------------------------ |
| Notifications | `Agent.on_*` / `Agent.async_on_*` hooks                 | Unchanged                                                          |
| Participatory | (no mechanism — RPC parent currently spectator)         | Opt-in decision hooks; shim subscribes on pi's behalf              |
| Requests      | `PiRpcClient.set_extension_ui_handler(method, handler)` | Possibly folded into the new decision-hook surface for unification |
| RPC envelope  | `PiRpcClient._handle_message` plumbing                  | Unchanged                                                          |

The v7 row "no mechanism for participatory" reflects what was implemented, not a design preference. Per § Scope correction, the long-term direction is opt-in participation. See `dev-notes/2026-05-17-v8-request-notes.md` for the v8 design space.

## Per-event analysis: what we are NOT surfacing, and why

Cataloguing every event pi can emit that v7 does *not* expose as an `Agent` hook, organized by which surface it belongs to.

### Category 1 — Participatory hooks (19 events, never reach our parent)

These are `AgentSession._emitExtensionEvent` events, dispatched only to in-process TS extensions. They don't cross stdout. We *couldn't* surface them as `Agent` hooks even if we wanted to. But because the framing matters, here's why we wouldn't want to even if pi added an RPC bridge for them:

**Pre-flight / veto hooks:**

- *`session_before_switch`* — fires before switching to another session. Handler can return `cancelled: true` to veto. *Not for Agent because:* veto semantics require a synchronous round-trip; we'd be inserting Python latency into every session switch.
- *`session_before_fork`* — fires before forking. Same veto shape.
- *`session_before_compact`* — fires before compaction; handler can return modified prep data or veto.
- *`session_before_tree`* — fires before tree navigation; can veto.
- *`tool_call`* — fires after argument validation, before execution. Handler can return `false` to block the call or modified args to mutate them. *Not for Agent because:* we already own the tool body in Python (it's a `@registry.register` decorator target). The veto-or-mutate semantics belong inside the tool function itself, not in a separate hook.
- *`tool_result`* — fires after execution; can mutate content/details/error. *Not for Agent because:* same reason — the tool body returns what the tool body returns; if Python wants to transform results, it does so in the function.
- *`input`* — fires when user input arrives; can transform or alternately route. *Not for Agent because:* the harness *is* the input source in RPC mode (we call `client.prompt(...)`); we already control what gets sent.
- *`user_bash`* — fires before pi executes a `!` or `!!` user-bash command. Can be intercepted. *Not for Agent because:* in RPC mode pi doesn't process user-bash from the parent — the parent uses `client.bash(...)` for explicit shell calls.

**Provider-interaction hooks:**

- *`context`* — fires before each LLM call (after prompt assembly, before LLM Message conversion). Handler can mutate message list. *Not for Agent because:* same as `before_provider_request` — would require a sync round-trip on the hot path.
- *`before_provider_request`* — fires before pi sends the request to the provider; can replace the payload.
- *`after_provider_response`* — fires after the provider responds, before stream consumption.
- *`before_agent_start`* — fires after user submission, before the agent loop starts; can mutate the system prompt.

*Why none of these belong on Agent:* they're synchronous decision points on pi's hot path. Routing them over RPC for Python to weigh in would add round-trip latency to every LLM call, every tool dispatch, every message ingest. Pi's RPC mode is deliberately built without this; the design recognizes that participatory hooks belong to in-process extensions.

**Session-lifecycle hooks (in-process variants):**

- *`session_start`* — fires when a session is initialized (with reason: `startup` | `reload` | `new` | `resume` | `fork`).
- *`session_shutdown`* — fires when the session runtime tears down.
- *`session_compact`* — fires *after* a compaction entry is created.
- *`session_tree`* — fires *after* tree navigation completes.
- *`resources_discover`* — fires before skills/prompts/themes load.

*Why none of these belong on Agent (even though they're notifications, not vetos):* they're TS-extension-side notifications about the *extension lifecycle*, fired by `_emitExtensionEvent`, not by `_emit` to RPC subscribers. They tell TS extensions "the session you're attached to is starting up" so the extension can set up its own state. Our Python harness doesn't have the equivalent concern — we know when *we* called `harness.start()`. We don't need pi to tell us our own session started.

**Selection-event variants:**

- *`model_select`* — pre-event for model change; can veto. (We DO get the post-event `session_info_changed`.)
- *`thinking_level_select`* — pre-event for reasoning-level change; can veto. (We DO get the post-event `thinking_level_changed`.)

*Why not for Agent:* these have veto semantics. We already get the post-change notification on the surface (1) channel.

### Category 2 — RPC envelope (3 frames)

- *`response`* — every RPC command emits exactly one response frame, correlated by id. Already consumed by `PiRpcClient._handle_message` request-correlation machinery (`rpc.py:358-363`) *before* `_dispatch_event`. Never reaches Agent.

  *Why not surfaced:* protocol plumbing, not application notification. Surfacing it would mean Agent could observe its own RPC calls completing — circular and confusing. The dedicated correlation machinery is the right place.

- *`extension_ui_request`* — pi asks the parent for a UI value (a dialog answer, a selection, etc.). Correlated request/response with `extension_ui_response`. Has dedicated handling via `PiRpcClient.set_extension_ui_handler(method, handler)` plus fallback defaults.

  *Why not surfaced as a named Agent hook:* this is surface (3), a request with required return semantics. The handler must return a dict like `{"confirmed": true}` or `{"cancelled": true}`. `Agent` hooks don't have a return contract. Adding a parallel "hook" surface alongside the existing `set_extension_ui_handler` would force users to choose between two mechanisms that do the same job — one of them with broken semantics (no return) and one with correct semantics (returns a dict).

  Currently `extension_ui_request` *also* gets forwarded to `_dispatch_event` (see `rpc.py:366-369`), which means it reaches Agent's dispatcher and trips strict mode. The fix is to **add `extension_ui_request` to a `_RPC_REQUEST_TYPES` skip set in Agent's dispatcher** so it's silently bypassed there. Users who want to observe these can subscribe via `client.on_event(handler)` at the lower level.

- *`extension_error`* — fires when a TS extension handler throws inside pi (`rpc-mode.ts:341`). This *is* a notification — pi is telling us "your extension is misbehaving." Pi doesn't wait for a response.

  *We should surface this as a named hook.* (Currently not in v7; I'm recommending we add it.)

### Category 3 — Notifications that ARE worth surfacing (the recommendation)

Everything in `AgentSession.subscribe` flows to RPC. v7 already exposes the 10 core agent-loop events. Adding the 7 session-layer events brings the named hook surface in line with what pi can notify our parent about:

(See § Per-missing-event recommendations below for the per-event detail.)

### Summary table — what's NOT a hook, by category

| Category                      | Count | Reachability        | Why not an Agent hook                                     |
| ----------------------------- | ----- | ------------------- | --------------------------------------------------------- |
| Participatory hooks           | 19    | In-process only     | Veto/mutate semantics; no Python return path in RPC       |
| `response` frames             | 1     | Filtered pre-Agent  | RPC envelope; request-correlation plumbing                |
| `extension_ui_request` frames | 1     | Reaches Agent today | Surface (3): request with required return; use UI handler |

Everything else pi emits to our parent is fair game for surface (1) and should be a named hook.

## Per-missing-event recommendations

Each missing event below: should it become a named hook in `Agent`, or should we leave it to the lower-level `on_event` API? Recommendations follow the principle established above: **surface (1) notifications get named hooks; surfaces (2) and (3) don't.** All 7 session-layer events plus `extension_error` are surface (1) notifications.

### Session-layer events (add all 7 as named hooks)

**`queue_update`** — fires when the steering/follow-up message queue state changes (e.g., user submits a steering message while the agent is mid-turn).

- *Recommend:* **add as named hook.** UIs that show "X messages pending while agent is running" need this. Low frequency, low payload size.

**`compaction_start`** — fires when context compaction begins (manual trigger, threshold reached, or overflow).

- *Recommend:* **add as named hook.** Compaction is a major event in long sessions. UIs may want to show a "compacting…" indicator; loggers may want to record it.

**`compaction_end`** — fires after compaction completes. Includes `reason`, `result`, `aborted`, `willRetry`, `errorMessage` per the source.

- *Recommend:* **add as named hook.** Paired with `compaction_start`; users will almost always want both or neither.

**`auto_retry_start`** — fires when pi automatically retries after a recoverable error. Includes `attempt`, `maxAttempts`, `delayMs`, `errorMessage`.

- *Recommend:* **add as named hook.** Operationally important — users want to know "this run is currently retrying" rather than "this run is just slow."

**`auto_retry_end`** — fires when retry succeeds or exhausts. Includes `success`, `attempt`, `finalError`.

- *Recommend:* **add as named hook.** Paired with `auto_retry_start`.

**`session_info_changed`** — fires when session name or related info changes (`agent-session.ts:2663`).

- *Recommend:* **add as named hook.** Cheap; UIs displaying session name need it. Rare frequency.

**`thinking_level_changed`** — fires when reasoning level is set/toggled (`agent-session.ts:1524`).

- *Recommend:* **add as named hook.** Cheap; UIs displaying model config want it. Rare frequency.

### RPC envelope events (handle case-by-case)

**`extension_ui_request`** — already has a dedicated mechanism in `PiRpcClient` (`set_extension_ui_handler(method, handler)` plus a fallback). It also currently reaches `_dispatch_event` (see `rpc.py:366-369`).

- *Recommend:* **filter out from `Agent` dispatch.** Two clean ways:

  - (a) Have `Agent._async_on_event` early-return on `extension_ui_request` (add to an internal "skip" set).
  - (b) Stop having `PiRpcClient._handle_message` forward `extension_ui_request` to `_dispatch_event` at all — let it go to the UI handler only.

  Option (b) is the cleaner fix and matches the design intent (`extension_ui_request` is a *request*, not a *notification* — different abstraction). But it has wider impact (existing `client.on_event` subscribers that watch UI requests would no longer receive them). Option (a) is safer for v1; revisit (b) later. Either way, don't add as a named hook — the UI-handler mechanism is the right surface.

**`extension_error`** — fires when a TS extension handler throws inside pi (`rpc-mode.ts:341`). Reaches our parent as a regular event.

- *Recommend:* **add as named hook.** Lets users observe TS-extension misbehavior. Probably rare in normal operation. Low cost.

## Concrete patch suggested

`Agent._EVENT_NAMES` becomes:

```python
_EVENT_NAMES: ClassVar[frozenset[str]] = frozenset({
    # Core agent-loop events (AgentEvent in packages/agent/src/types.ts)
    "agent_start",
    "agent_end",
    "turn_start",
    "turn_end",
    "message_start",
    "message_update",
    "message_end",
    "tool_execution_start",
    "tool_execution_update",
    "tool_execution_end",
    # Session-layer events (agent-session.ts)
    "queue_update",
    "compaction_start",
    "compaction_end",
    "session_info_changed",
    "thinking_level_changed",
    "auto_retry_start",
    "auto_retry_end",
    # RPC error envelope
    "extension_error",
})
```

That's 18 events × 2 hook attrs = 36 `ClassVar` declarations on the base class. Per the v7 spec (no metaclass codegen for this pass), all 36 are spelled out literally.

Add to the Agent dispatcher a "skip" set for `extension_ui_request`:

```python
_RPC_REQUEST_TYPES: ClassVar[frozenset[str]] = frozenset({"extension_ui_request"})

async def _async_on_event(self, event):
    wrapped = event if isinstance(event, AgentEvent) else AgentEvent.from_mapping(event)
    name = wrapped.type
    if name in self._RPC_REQUEST_TYPES:
        return  # handled elsewhere; never an Agent hook
    if name not in self._EVENT_NAMES:
        if self._raise_on_unhandled_event:
            raise UnhandledEventError(...)
        return
    ...  # async/sync dispatch as before
```

This makes strict mode meaningful: it will fire if pi emits a *truly new* event type (e.g. a future pi release adds `session_renamed_v2`) but not for the known set we're aware of today.

## Tests to add

- Unit test that asserts every name in `_EVENT_NAMES` is also a declared `async_on_X` / `on_X` ClassVar on `Agent` (and vice versa) — catches future drift.
- Unit test that strict mode does NOT raise on `extension_ui_request` (it's in the skip set).
- Unit test for one new hook (e.g., `compaction_start`) wired end-to-end via the fake-pi: enqueue a synthetic `compaction_start` event, assert the hook fires.

Optional: a small CI/test helper that parses `links/pi/packages/agent/src/types.ts` + `agent-session.ts` and asserts the names match `_EVENT_NAMES`. Stretch — would catch pi adding/renaming events on upgrade.

## Open questions for you

1. **Add the 7 session events + `extension_error` to `_EVENT_NAMES`?** Recommendation above is yes; flagging in case you want a narrower or wider set.

1. **`extension_ui_request` handling — option (a) skip in Agent, or (b) stop forwarding from `PiRpcClient._handle_message`?** (a) is safer for v1; (b) is cleaner long term.

1. **Where to land this patch?** Three options:

   - (i) Hand it back to ChatGPT as a small follow-on revision and have them produce v7.1.
   - (ii) Apply the patch ourselves during the port into `libharness.pi`.
   - (iii) Port v7 as-is and follow up with the event-set expansion in a separate commit.

   I lean (ii) — the patch is small, mechanical, and our event verification is the gating insight. ChatGPT doesn't have any new information to bring to it.

1. **`extension_error` priority.** Optional add. Skip for v1?
